"""Logs: a window dragged out on the chart, and per-service error counts in the sidebar."""
import time
from conftest import AUTH


def _post(client, svc, body, ts_ns, sev):
    d = {"resourceLogs": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": svc}},
                                                      {"key": "k8s.namespace.name", "value": {"stringValue": "win"}}]},
                           "scopeLogs": [{"logRecords": [{"timeUnixNano": str(ts_ns), "severityNumber": sev, "body": {"stringValue": body}}]}]}]}
    assert client.post("/v1/logs", json=d, headers=AUTH).status_code == 200


def test_window_selects_only_that_period_everywhere(client):
    now = time.time_ns()
    mins = lambda m: now - int(m * 60e9)
    _post(client, "win-old", "old-window-line", mins(30), 17)
    _post(client, "win-new", "new-window-line", mins(3), 17)
    both = {r["body"] for r in client.get("/api/logs?minutes=60&q_=window-line", headers=AUTH).json()}
    assert both == {"old-window-line", "new-window-line"}
    start, end = (now - int(35 * 60e9)) / 1e6, (now - int(25 * 60e9)) / 1e6
    win = f"start_ms={start}&end_ms={end}"
    only = {r["body"] for r in client.get(f"/api/logs?minutes=60&q_=window-line&{win}", headers=AUTH).json()}
    assert only == {"old-window-line"}
    # every other view honours the same window
    assert client.get(f"/api/logs/groupby?field=service&q_=window-line&{win}", headers=AUTH).json()["scanned"] == 1
    pat = client.get(f"/api/logs/patterns?q_=window-line&{win}", headers=AUTH).json()
    assert pat["scanned"] == 1
    assert "old-window-line" in client.get(f"/api/logs/export?q_=window-line&{win}", headers=AUTH).text
    assert "new-window-line" not in client.get(f"/api/logs/export?q_=window-line&{win}", headers=AUTH).text
    fac = client.get(f"/api/logs/facets?q_=window-line&{win}", headers=AUTH).json()
    assert [x["value"] for x in fac["service"]] == ["win-old"]
    hist = client.get(f"/api/logs/histogram?buckets=10&{win}", headers=AUTH).json()
    assert len(hist) == 10 and abs(hist[0]["t"] - start) < 1 and sum(b["n"] for b in hist) >= 1
    assert abs((hist[1]["t"] - hist[0]["t"]) - (end - start) / 10) < 1          # buckets span the window, not "last N minutes"


def test_window_is_ignored_when_incomplete_or_reversed(client):
    r = client.get("/api/logs?minutes=60&start_ms=5&end_ms=1", headers=AUTH)
    assert r.status_code == 200
    assert client.get("/api/logs?minutes=60&start_ms=123456", headers=AUTH).status_code == 200


def test_service_facet_ranks_by_errors_and_reports_them(client):
    now = time.time_ns()
    for i in range(12):
        _post(client, "chatty-ok", f"fine {i}", now - 1000 * i, 9)
    for i in range(3):
        _post(client, "quiet-broken", f"boom {i}", now - 1000 * i, 17)
    by_count = client.get("/api/logs/facets?q_=&minutes=5", headers=AUTH).json()["service"]
    by_err = client.get("/api/logs/facets?minutes=5&sort=errors", headers=AUTH).json()["service"]
    row = lambda rows, n: next(x for x in rows if x["value"] == n)
    assert row(by_err, "quiet-broken")["err"] == 3 and row(by_err, "chatty-ok")["err"] == 0
    names = [x["value"] for x in by_err]
    assert names.index("quiet-broken") < names.index("chatty-ok")           # errors first, whatever the volume
    names_c = [x["value"] for x in by_count]
    assert names_c.index("chatty-ok") < names_c.index("quiet-broken")       # default order is still by volume


def test_metric_group_by_sees_resource_attributes(client):
    """kubeletstats puts k8s.pod.name / k8s.node.name on the resource, not on the data point."""
    now = time.time_ns()
    kv = lambda k, v: {"key": k, "value": {"stringValue": v}}
    def rm(pod, node, val, dp_attr=None):
        pt = {"timeUnixNano": str(now - 60_000_000_000), "asDouble": val}
        if dp_attr:
            pt["attributes"] = [kv(*dp_attr)]
        return {"resource": {"attributes": [kv("k8s.pod.name", pod), kv("k8s.node.name", node), kv("k8s.namespace.name", "grp")]},
                "scopeMetrics": [{"metrics": [{"name": "grp.test.mem", "gauge": {"dataPoints": [pt]}}]}]}
    body = {"resourceMetrics": [rm("pod-a", "n1", 1.0), rm("pod-b", "n2", 2.0), rm("pod-c", "n1", 3.0, ("state", "used"))]}
    assert client.post("/v1/metrics", json=body, headers=AUTH).status_code == 200
    g = lambda by: client.get(f"/api/metrics/query?name=grp.test.mem&minutes=10&buckets=5&group_by={by}", headers=AUTH).json()["series"]
    assert set(g("k8s.pod.name")) == {"pod-a", "pod-b", "pod-c"}          # resource attribute
    assert set(g("k8s.node.name")) == {"n1", "n2"}                        # two pods share n1
    assert set(g("state")) == {"None", "used"} or set(g("state")) == {"used", "None"}   # data point attribute still works
    assert set(client.get("/api/metrics/query?name=grp.test.mem&minutes=10&buckets=5", headers=AUTH).json()["series"]) == {"unknown"}  # default grouping unchanged
