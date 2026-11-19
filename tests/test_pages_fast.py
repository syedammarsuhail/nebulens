"""Kubernetes, Environments and Traces pages: they must use their small registries / rollups, and give exact numbers."""
import json, secrets, time
from conftest import AUTH

MIN = 60_000_000_000


def _no_raw_scans(monkeypatch, srv):
    """Record every SQL statement; the fast paths must not touch the spans / logs / metrics tables."""
    seen = []
    rq, rr = srv.q, srv.qrows
    monkeypatch.setattr(srv, "q", lambda sql, args=(): (seen.append(sql), rq(sql, args))[1])
    monkeypatch.setattr(srv, "qrows", lambda sql, args=(): (seen.append(sql), rr(sql, args))[1])
    return lambda: [x for x in seen if any(f"FROM {t}" in x for t in ("spans ", "spans\n", "logs ", "metrics "))]


def _post_res(client, sig, res, n=1, error=False):
    now = time.time_ns()
    attrs = [{"key": k, "value": {"stringValue": v}} for k, v in res.items()]
    if sig == "spans":
        recs = [{"traceId": secrets.token_hex(16), "spanId": secrets.token_hex(8), "parentSpanId": "", "name": "op", "kind": 2,
                 "startTimeUnixNano": str(now - 2_000_000), "endTimeUnixNano": str(now), "status": {"code": 2 if error else 0}}
                for _ in range(n)]
        body, path = {"resourceSpans": [{"resource": {"attributes": attrs}, "scopeSpans": [{"spans": recs}]}]}, "/v1/traces"
    else:
        recs = [{"timeUnixNano": str(now), "severityNumber": 17 if error else 9, "body": {"stringValue": "x"}} for _ in range(n)]
        body, path = {"resourceLogs": [{"resource": {"attributes": attrs}, "scopeLogs": [{"logRecords": recs}]}]}, "/v1/logs"
    assert client.post(path, json=body, headers=AUTH).status_code == 200


def test_kubernetes_page_reads_the_resource_registry_not_raw_rows(srv, client, monkeypatch):
    tag = secrets.token_hex(3)
    res = {"service.name": "k8s-" + tag, "k8s.namespace.name": "ns-" + tag, "k8s.pod.name": "pod-" + tag, "k8s.node.name": "node-x",
           "telemetry.sdk.language": "java", "deployment.environment": "env-" + tag}
    _post_res(client, "spans", res)
    _post_res(client, "logs", res)
    monkeypatch.setattr(srv, "_res_ready", lambda m: True)
    raw = _no_raw_scans(monkeypatch, srv)
    ns = next(n for n in srv.k8s_namespaces(60.0, "") if n["namespace"] == "ns-" + tag)
    wl = next(w for w in srv.k8s_workloads(60.0, "ns-" + tag, "") if w["service"] == "k8s-" + tag)
    assert ns["services"] == 1 and ns["instrumented"] == 1 and set(ns["signals"]) == {"spans", "logs"}
    assert wl["language"] == "java" and wl["pods"] == ["pod-" + tag] and wl["node"] == "node-x" and wl["instrumented"]
    assert srv.k8s_overview(60.0, "")["nodes"] >= 1
    assert not raw()


def test_resource_registry_backfill_and_fallback(srv):
    tag = secrets.token_hex(3)
    now = time.time_ns()
    res = json.dumps({"service.name": "bf-" + tag, "k8s.namespace.name": "bfns-" + tag, "k8s.pod.name": "bfpod-" + tag})
    with srv.db() as c:
        c.execute("INSERT INTO spans(trace_id,span_id,parent_id,service,name,kind,start_ns,end_ns,dur_ms,status,status_msg,attrs,res,env) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (secrets.token_hex(16), "s1", "", "bf-" + tag, "op", 2, now - MIN, now - MIN + 1000, 0.001, 0, "", "{}", res, ""))
        c.execute("UPDATE rollup_state SET value=? WHERE key='res_from'", (int(time.time() // 60) + 10,))
    assert not srv._res_ready(60)                    # not covered yet: the page falls back to the sampled scan
    srv._res_backfill()
    assert srv._res_ready(60)
    assert srv.q("SELECT pod FROM resource_seen WHERE service=?", ("bf-" + tag,))[0]["pod"] == "bfpod-" + tag


def test_environments_page_reads_rollups_and_counts_exactly(srv, client, monkeypatch):
    tag = secrets.token_hex(3)
    res = {"service.name": "envsvc-" + tag, "k8s.namespace.name": "envns-" + tag, "deployment.environment": "envtest-" + tag}
    _post_res(client, "spans", res, n=7)
    _post_res(client, "spans", res, n=3, error=True)
    _post_res(client, "logs", res, n=5)
    _post_res(client, "logs", res, n=2, error=True)
    monkeypatch.setattr(srv, "_env_ready", lambda m: True)
    monkeypatch.setattr(srv, "_res_ready", lambda m: True)
    raw = _no_raw_scans(monkeypatch, srv)
    envs = srv.environments(60.0)
    assert not raw()
    e = next(x for x in envs if any(n["namespace"] == "envns-" + tag for n in x["namespaces"]))
    assert (e["spans"], e["span_errors"], e["logs"], e["log_errors"]) == (10, 3, 7, 2) and e["services"] >= 1
    monkeypatch.setattr(srv, "_env_ready", lambda m: False)
    monkeypatch.setattr(srv, "_res_ready", lambda m: False)
    slow = next(x for x in srv.environments(60.0) if x["slug"] == e["slug"])
    assert (slow["spans"], slow["span_errors"], slow["logs"], slow["log_errors"]) == (10, 3, 7, 2)   # same answer the slow way


def test_environment_rollup_backfill_counts_history_once(srv):
    tag = secrets.token_hex(3)
    now = time.time_ns()
    with srv.db() as c:
        for i in range(6):
            c.execute("INSERT INTO spans(trace_id,span_id,parent_id,service,name,kind,start_ns,end_ns,dur_ms,status,status_msg,attrs,res,env) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (secrets.token_hex(16), "s", "p" if i % 2 else "", "eb-" + tag, "op", 1, now - (20 + i) * MIN,
                       now - (20 + i) * MIN + 1, 0.0, 2 if i == 0 else 0, "", "{}", "{}", "eb-env-" + tag))
        c.execute("UPDATE rollup_state SET value=? WHERE key='env_from'", (int(time.time() // 60),))
    srv._env_backfill(chunk_min=7, pause=0)
    srv._env_backfill(chunk_min=7, pause=0)          # a second run is a no-op
    r = srv.q("SELECT SUM(spans) n, SUM(span_err) e FROM env_rollup WHERE service=?", ("eb-" + tag,))[0]
    assert (int(r["n"]), int(r["e"])) == (6, 1)      # child spans count too: the page shows all spans
    assert srv._marker("env_from") == 0


def test_traces_list_aggregates_correctly(client):
    tag = secrets.token_hex(3)
    now = time.time_ns()
    tid = secrets.token_hex(16)

    def sp(sid, parent, st_ms, dur_ms, err=False):
        base = now - 100_000_000
        return {"traceId": tid, "spanId": sid, "parentSpanId": parent, "name": "n-" + sid, "kind": 2,
                "startTimeUnixNano": str(base + st_ms * 1_000_000), "endTimeUnixNano": str(base + (st_ms + dur_ms) * 1_000_000),
                "status": {"code": 2 if err else 0}}
    for svc, spans in (("tr-a-" + tag, [sp("aaaa000000000001", "", 0, 80)]),
                       ("tr-b-" + tag, [sp("bbbb000000000002", "aaaa000000000001", 10, 30, err=True),
                                        sp("bbbb000000000003", "bbbb000000000002", 15, 5)])):
        body = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": svc}}]},
                                   "scopeSpans": [{"spans": spans}]}]}
        assert client.post("/v1/traces", json=body, headers=AUTH).status_code == 200
    ids = lambda qs: {t["trace_id"] for t in client.get("/api/traces?minutes=5&" + qs, headers=AUTH).json()}
    got = {t["trace_id"]: t for t in client.get("/api/traces?minutes=5&limit=50", headers=AUTH).json()}[tid]
    assert (got["spans"], got["errors"], got["services"], got["root"], got["root_service"]) == (3, 1, 2, "n-aaaa000000000001", "tr-a-" + tag)
    assert abs(got["dur_ms"] - 80) < 0.01
    assert tid in ids("errors=true") and tid in ids("min_ms=79") and tid not in ids("min_ms=81")
    assert tid in ids("service=tr-b-" + tag)


def test_environments_discover_uses_the_rollup_and_matches(srv, client, monkeypatch):
    tag = secrets.token_hex(3)
    res = {"service.name": "disc-" + tag, "deployment.environment": "disc-" + tag}
    _post_res(client, "spans", res, n=4)
    _post_res(client, "logs", res, n=3)
    slow = srv.environments_discover(1440)                   # rollup not covering a day in the test DB: the raw scan
    monkeypatch.setattr(srv, "_env_ready", lambda m: True)
    raw = _no_raw_scans(monkeypatch, srv)
    fast = srv.environments_discover(1440)
    assert not raw()
    pick = lambda d: {e["slug"]: e["records"] for e in d["environments"]}
    slug = next(k for k in pick(fast) if tag in k)
    assert pick(fast)[slug] == pick(slow)[slug] == 7
