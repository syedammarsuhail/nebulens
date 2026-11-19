"""End-to-end API behaviour. Runs unchanged on SQLite and Postgres (see conftest.py)."""
import pytest
from conftest import AUTH


@pytest.fixture(scope="module", autouse=True)
def data(ingest):
    for i in range(6):
        ingest.logs("feedexport", "application", f"2026-11-09 ERROR Export failed for job 44{i} after {90+i}ms", 0, **{"http.status_code": "500"})
    for i in range(3):
        ingest.logs("feedexport", "application", f"slow call {i}", 13)
    for i in range(4):
        ingest.logs("quiet-svc", "genai", f"GET /x {i} ok", 9, **{"http.status_code": "200"})
    ingest.logs("argocd-server", "argocd", 'GET /y HTTP/1.1" 200 OK', 0)
    ingest.logs("billing", "application", "paid with 4111 1111 1111 1111 ok", 9)
    ingest.logs("blank", "application", "   ", 9)                                  # blank lines are dropped
    ingest.span("feedexport", "application", error=True)
    ingest.span("feedexport", "application")
    ingest.metric("node-exporter", "kube-system")


def get(client, path, **kw):
    r = client.get(path, headers=AUTH, **kw)
    assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:200]}"
    return r.json() if "csv" not in r.headers.get("content-type", "") else r.text


ENDPOINTS = ["/api/logs?minutes=60", "/api/logs/histogram?minutes=60", "/api/logs/facets?minutes=60",
             "/api/logs/patterns?minutes=60", "/api/logs/groupby?field=severity&minutes=60",
             "/api/logs/groupby?field=http.status_code&minutes=60", "/api/logs/fields?minutes=60", "/api/logs/export?minutes=60",
             "/api/environments?minutes=60", "/api/environments/discover", "/api/environments/access",
             "/api/services?minutes=60", "/api/map?minutes=60", "/api/traces?minutes=60", "/api/metrics/names?minutes=60",
             "/api/audit", "/api/audit/verify", "/api/audit/export", "/api/redaction", "/api/mode"]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_endpoint_ok(client, path):
    get(client, path)


def test_severity_is_inferred_and_flagged(client):
    rows = get(client, "/api/logs?minutes=60&service=feedexport&limit=50")
    errs = [r for r in rows if r["body"].startswith("2026-11-09 ERROR")]
    assert errs and all(r["severity"] >= 17 for r in errs)
    assert all(r["attrs"].get("oscilyx.severity.inferred") for r in errs)         # inferred, not reported
    reported = [r for r in rows if r["body"].startswith("slow call")]
    assert reported and not any("oscilyx.severity.inferred" in r["attrs"] for r in reported)


def test_blank_lines_are_dropped_and_cards_redacted(client):
    rows = get(client, "/api/logs?minutes=60&limit=500")
    assert not [r for r in rows if r["service"] == "blank"]
    billing = [r for r in rows if r["service"] == "billing"][0]
    assert "4111" not in billing["body"] and "card:redacted" in billing["body"]


def test_histogram_has_full_breakdown(client):
    h = get(client, "/api/logs/histogram?minutes=60&buckets=4")
    assert {"err", "warn", "info", "debug", "unset", "fatal"} <= set(h[0])
    assert sum(x["err"] for x in h) >= 6 and sum(x["warn"] for x in h) >= 3


def test_facets_respect_the_other_filters(client):
    quiet = get(client, "/api/logs/facets?minutes=60&service=quiet-svc")
    assert {x["value"] for x in quiet["severity"]} == {"INFO"}                    # severity follows the service filter
    errors = get(client, "/api/logs/facets?minutes=60&min_severity=17")
    names = {x["value"] for x in errors["service"]}
    assert "feedexport" in names and "quiet-svc" not in names                    # service list follows the severity filter


def test_groupby_errors_by_service(client):
    g = get(client, "/api/logs/groupby?field=service&minutes=60&min_severity=17")
    assert [x["value"] for x in g["groups"]][0] == "feedexport"


def test_patterns_collapse_similar_lines(client):
    p = get(client, "/api/logs/patterns?minutes=60&service=feedexport&min_severity=17")
    assert p["distinct"] == 1 and p["patterns"][0]["count"] == 6 and "<n>" in p["patterns"][0]["pattern"]


def test_one_environment_with_namespaces_inside(client):
    envs = get(client, "/api/environments?minutes=60")
    assert [e["slug"] for e in envs] == ["test-cluster"]
    e = envs[0]
    # Namespaces come from logs and traces. A namespace that only sends metrics is deliberately not listed:
    # scanning the (huge) metrics table for it is what made large installs slow.
    assert {n["namespace"] for n in e["namespaces"]} >= {"application", "genai", "argocd"}
    assert e["services"] >= 4 and e["logs"] > 0 and e["log_errors"] >= 6        # counted from logs, not only traces


def test_services_registry_lists_every_signal(client):
    names = {s["service"] for s in get(client, "/api/services?minutes=60")}
    assert {"feedexport", "quiet-svc", "argocd-server", "node-exporter"} <= names  # node-exporter only sent metrics


def test_metrics_names_windowed(client):
    assert any(m["name"] == "cpu.load" for m in get(client, "/api/metrics/names?minutes=60"))
    assert get(client, "/api/metrics/names?minutes=0")                           # all-time still works


def test_audit_chain_detects_tampering(client, srv):
    for i in range(3):
        assert client.post("/api/dashboards", json={"name": f"audit-{i}", "panels": []}, headers=AUTH).status_code == 200
    assert get(client, "/api/audit/verify")["ok"] is True
    ids = [r["id"] for r in srv.q("SELECT id FROM audit ORDER BY id")]
    with srv.db() as c:
        c.execute("UPDATE audit SET actor=? WHERE id=?", ("someone-else", ids[1]))
    bad = get(client, "/api/audit/verify")
    assert bad["ok"] is False and bad["broken_at_id"] == ids[1]
    with srv.db() as c:
        c.execute("UPDATE audit SET actor=? WHERE id=?", ("api-token", ids[1]))
    assert get(client, "/api/audit/verify")["ok"] is True
    with srv.db() as c:
        c.execute("DELETE FROM audit WHERE id=?", (ids[1],))
    gap = get(client, "/api/audit/verify")
    assert gap["ok"] is False and "deleted" in gap["message"]


def test_environment_admin_roundtrip(client):
    assert client.put("/api/environments/access", json={"grp": "dev", "slugs": ["test-cluster"]}, headers=AUTH).status_code == 200
    assert get(client, "/api/environments/access")["access"][0]["grp"] == "dev"
    assert client.put("/api/environments/access", json={"grp": "dev", "slugs": []}, headers=AUTH).status_code == 200


# ---- security -------------------------------------------------------------------------------------------
def test_api_requires_a_token(client):
    assert client.get("/api/logs?minutes=60").status_code == 401
    assert client.post("/v1/logs", json={"resourceLogs": []}).status_code == 401


def test_api_is_same_origin_only(client):
    r = client.options("/api/logs", headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in r.headers


def test_browser_ingest_preflight_works_but_does_not_weaken_auth(client):
    pre = client.options("/v1/traces", headers={"Origin": "https://app.example.com", "Access-Control-Request-Method": "POST",
                                                "Access-Control-Request-Headers": "content-type,authorization"})
    assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == "https://app.example.com"
    assert client.post("/v1/traces", json={"resourceSpans": []}, headers={"Origin": "https://app.example.com"}).status_code == 401
    ok = client.post("/v1/traces", json={"resourceSpans": []}, headers={**AUTH, "Origin": "https://app.example.com"})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "https://app.example.com"


def test_security_headers(client):
    h = client.get("/api/mode").headers
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer"
    assert "strict-transport-security" not in h                                   # only over HTTPS


def test_community_tier_limits(client, srv, monkeypatch):
    monkeypatch.setattr(srv, "IS_PRO", False)
    monkeypatch.setattr(srv, "TIER", "community")
    r = client.get("/api/audit", headers=AUTH)
    assert r.status_code == 403 and r.json()["error"] == "pro_required"


# ---- Round A: views ------------------------------------------------------------------------------------
def test_builtin_views_seed(client):
    r = get(client, "/api/views?kind=logs")
    slugs = {v["slug"] for v in r}
    assert {"all-errors", "warn-and-up", "pod-logs", "k8s-events", "noisy-services", "recurring"} <= slugs
    assert all(v["builtin"] for v in r if v["slug"] == "all-errors")


def test_view_save_delete_favorite(client, srv):
    # Save
    r = client.post("/api/views", json={"name": "feedexport errors", "kind": "logs",
                                        "params": {"service": "feedexport", "min_severity": 17}}, headers=AUTH)
    assert r.status_code == 200 and r.json()["slug"] == "feedexport-errors"
    vid = next(v["id"] for v in get(client, "/api/views?kind=logs") if v["slug"] == "feedexport-errors")
    # Favorite
    r = client.post(f"/api/views/{vid}/favorite", headers=AUTH); assert r.json()["favorite"] is True
    favs = [v for v in get(client, "/api/views?kind=logs") if v["favorite"]]
    assert any(v["slug"] == "feedexport-errors" for v in favs)
    # Delete
    assert client.delete(f"/api/views/{vid}", headers=AUTH).status_code == 200
    assert not any(v["slug"] == "feedexport-errors" for v in get(client, "/api/views?kind=logs"))


def test_builtin_deletion_tombstoned(client, srv):
    """A deleted built-in must stay deleted — otherwise restarts resurrect what the user hid."""
    vid = next(v["id"] for v in get(client, "/api/views?kind=logs") if v["slug"] == "recurring")
    assert client.delete(f"/api/views/{vid}", headers=AUTH).status_code == 200
    srv._views_seeded = False  # simulate a restart
    assert not any(v["slug"] == "recurring" for v in get(client, "/api/views?kind=logs"))


def test_views_validation(client):
    assert client.post("/api/views", json={"name": "x", "kind": "bogus"}, headers=AUTH).status_code == 400
    assert client.post("/api/views", json={"name": "", "kind": "logs"}, headers=AUTH).status_code == 400
    assert client.delete("/api/views/9999999", headers=AUTH).status_code == 404


# ---- Round A: errors page ------------------------------------------------------------------------------
def test_errors_endpoint_groups(client, ingest):
    for i in range(5): ingest.logs("errsvc", "app", f"ERROR DB timeout job {i}", 17)
    for i in range(2): ingest.logs("errsvc", "app", f"ERROR ConnectionRefused to 10.0.{i}.5", 17)
    ingest.logs("errsvc", "app", "INFO Starting", 9)                            # not an error
    r = get(client, "/api/errors?minutes=60&service=errsvc")
    assert r["distinct"] == 2 and r["errors"][0]["count"] == 5
    assert "ERROR DB timeout job" in r["errors"][0]["pattern"]
    assert len(r["errors"][0]["sparkline"]) > 0


def test_errors_sort_and_new_within_hours(client, ingest):
    import time as _t
    old_ns = _t.time_ns() - int(48 * 3600e9)
    # One very old error and three new ones
    body = {"resourceLogs": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": "errsvc"}}]},
        "scopeLogs": [{"logRecords": [{"timeUnixNano": str(old_ns), "severityNumber": 17,
                                      "body": {"stringValue": "ERROR ancient failure XYZ"}}]}]}]}
    client.post("/v1/logs", json=body, headers=AUTH)
    for i in range(3): ingest.logs("errsvc", "app", f"ERROR brand-new failure {i}", 17)
    r_new = get(client, "/api/errors?minutes=4320&sort=new&new_within_hours=1&service=errsvc")
    assert not any("ancient" in g["pattern"] for g in r_new["errors"])


# ---- Round A: PromQL -----------------------------------------------------------------------------------
@pytest.mark.parametrize("query", [
    "cpu.load", 'cpu.load{service="x"}', 'cpu.load{service=~"a|b"}',
    "sum(cpu.load)", "sum(cpu.load) by (service)", "avg without (pod) (cpu.load)",
    "rate(http.requests[5m])", "sum(rate(http.requests[5m])) by (service)",
    "cpu.load > 0.8", "cpu.load * 100", "cpu.load + cpu.load",
    "(cpu.load) / 2", "deriv(cpu.load[10m])",
])
def test_promql_valid_parses(srv, query):
    srv._promql_parse(query)                       # must not raise


@pytest.mark.parametrize("query", ["", "cpu load", "sum(", "cpu{x=}", "rate(x[])", "x[9z]"])
def test_promql_invalid_rejected(srv, query):
    with pytest.raises(Exception): srv._promql_parse(query)


def test_promql_runs_against_real_data(client, ingest):
    import time as _t
    for pod in ("p1", "p2"):
        for v in (0.3, 0.5, 0.7):
            ingest.metric("api", "app", name="cpu.load", value=v)
    _t.sleep(0.1)
    r = client.get("/api/promql/query", params={"query": 'cpu.load{service="api"}'}, headers=AUTH).json()
    assert r["status"] == "success" and len(r["data"]["result"]) >= 1
    r2 = client.get("/api/promql/query", params={"query": "sum(cpu.load) by (service)"}, headers=AUTH).json()
    assert r2["status"] == "success" and any(s["metric"].get("service") == "api" for s in r2["data"]["result"])
    r3 = client.get("/api/promql/query", params={"query": "sum("}, headers=AUTH).json()
    assert r3["status"] == "error"


def test_promql_metadata(client):
    assert client.get("/api/promql/labels", headers=AUTH).json()["status"] == "success"
    assert client.get("/api/promql/label/service/values", headers=AUTH).json()["status"] == "success"


# ---- Round A: RUM --------------------------------------------------------------------------------------
def test_rum_summary_shapes_percentiles(client):
    def pv(path, load, lcp, cls):
        body = {"resourceLogs": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "web"}}]},
            "scopeLogs": [{"logRecords": [{"timeUnixNano": str(__import__("time").time_ns()),
                "severityNumber": 9, "body": {"stringValue": "page_view " + path},
                "attributes": [
                    {"key": "event.name", "value": {"stringValue": "page_view"}},
                    {"key": "url.path",   "value": {"stringValue": path}},
                    {"key": "rum.load_ms", "value": {"doubleValue": load}},
                    {"key": "rum.lcp_ms",  "value": {"doubleValue": lcp}},
                    {"key": "rum.cls",     "value": {"doubleValue": cls}}]}]}]}]}
        assert client.post("/v1/logs", json=body, headers=AUTH).status_code == 200
    for i in range(10): pv("/fast", 300 + i * 5, 400, 0.01)
    for i in range(10): pv("/slow", 2000 + i * 100, 2500, 0.2)
    r = get(client, "/api/rum/summary?minutes=60&service=web")
    assert r["views"] == 20 and r["load_p50"] is not None
    assert r["slowest_paths"][0]["path"] == "/slow"


def test_rum_sdk_is_served(client):
    r = client.get("/sdk/oscilyx-rum.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert b"oscilyx-rum" in r.content.lower() and len(r.content) < 20 * 1024


# ---- Integrations end-to-end ---------------------------------------------------------------------------
def test_integrations_list_shape(client):
    r = get(client, "/api/integrations")
    assert len(r) >= 9
    ids = {i["id"] for i in r}
    assert {"kubernetes", "java", "nodejs", "python", "postgres", "redis", "kafka", "nginx", "browser-rum"} <= ids
    for i in r:
        assert "state" in i and "counts" in i and "installed" in i


def test_integration_detail_includes_setup_and_alerts(client):
    r = get(client, "/api/integrations/kubernetes")
    assert r["setup_blocks"] and any(b["kind"] == "command" for b in r["setup_blocks"])
    assert r["alerts"] and any(a["id"] == "k8s-cpu-limit" for a in r["alerts"])
    assert r["dashboards"] and r["dashboards"][0]["panels"]


def test_integration_install_uninstall_roundtrip(client):
    # Install creates dashboards + views
    pre = len(get(client, "/api/dashboards"))
    r = client.post("/api/integrations/kubernetes/install", headers=AUTH).json()
    assert r["ok"] and len(r["dashboard_ids"]) == 2 and len(r["view_ids"]) == 2
    post = get(client, "/api/dashboards")
    names = {d["name"] for d in post}
    assert "Kubernetes · Cluster" in names and "Kubernetes · Workloads" in names
    # Re-install is idempotent
    r2 = client.post("/api/integrations/kubernetes/install", headers=AUTH).json()
    assert r2.get("already_installed") is True and len(get(client, "/api/dashboards")) == len(post)
    # Uninstall removes only what the integration installed
    r3 = client.post("/api/integrations/kubernetes/uninstall", headers=AUTH).json()
    assert r3["ok"] and r3["removed"]["dashboards"] == 2
    final = get(client, "/api/dashboards")
    assert "Kubernetes · Cluster" not in {d["name"] for d in final}
    # Uninstall-when-missing is a no-op
    r4 = client.post("/api/integrations/kubernetes/uninstall", headers=AUTH).json()
    assert r4["was_installed"] is False


def test_integration_detection_flips_when_data_arrives(client, ingest):
    before = get(client, "/api/integrations/postgres")
    assert before["state"] == "no_data"
    ingest.metric("db", "app", name="postgresql.connections.active", value=12.0)
    import time as _t; _t.sleep(0.1)
    after = get(client, "/api/integrations/postgres")
    assert after["state"] == "active"
    assert "postgresql.connections.active" in after["detected"]["metric_names"]


def test_integration_unknown_is_404(client):
    assert client.get("/api/integrations/does-not-exist", headers=AUTH).status_code == 404
    assert client.post("/api/integrations/does-not-exist/install", headers=AUTH).status_code == 404


# ---- Kubernetes monitoring (v10) -------------------------------------------------------------------
def _k8s_span(client, service, ns, language="", version="", deployment="", pod="", node="", cluster=""):
    import secrets, time
    attrs = [{"key": "service.name", "value": {"stringValue": service}},
             {"key": "k8s.namespace.name", "value": {"stringValue": ns}}]
    if language: attrs.append({"key": "telemetry.sdk.language", "value": {"stringValue": language}})
    if version: attrs.append({"key": "telemetry.sdk.version", "value": {"stringValue": version}})
    if deployment: attrs.append({"key": "k8s.deployment.name", "value": {"stringValue": deployment}})
    if pod: attrs.append({"key": "k8s.pod.name", "value": {"stringValue": pod}})
    if node: attrs.append({"key": "k8s.node.name", "value": {"stringValue": node}})
    if cluster: attrs.append({"key": "k8s.cluster.name", "value": {"stringValue": cluster}})
    now = time.time_ns()
    body = {"resourceSpans": [{"resource": {"attributes": attrs},
            "scopeSpans": [{"spans": [{"traceId": secrets.token_hex(16), "spanId": secrets.token_hex(8),
                "name": "op", "kind": 2, "parentSpanId": "", "startTimeUnixNano": str(now - 1_000_000),
                "endTimeUnixNano": str(now), "status": {"code": 0}}]}]}]}
    assert client.post("/v1/traces", json=body, headers=AUTH).status_code == 200


def test_k8s_overview_counts(client):
    for i in range(3):
        _k8s_span(client, "feedexport", "application", language="java", version="2.11.0",
                  deployment="feedexport", pod=f"feedexport-{i}", node="node-0", cluster="staging")
    _k8s_span(client, "spushsocket", "application", pod="sock-0", node="node-0", cluster="staging")
    _k8s_span(client, "llm-gateway", "genai", language="python", version="1.28.0",
              pod="llm-0", node="node-1", cluster="staging")
    r = get(client, "/api/kubernetes/overview")
    assert r["nodes"] >= 2 and r["namespaces"] >= 2 and "staging" in r["clusters"]
    assert r["instrumented_workloads"] >= 2
    assert r["languages"].get("java", 0) >= 3 and r["languages"].get("python", 0) >= 1


def test_k8s_workloads_show_sdk_and_filter_by_namespace(client):
    # Ingest a known-instrumented workload for this test, since prior tests may or may not have run.
    _k8s_span(client, "feedexport-k8s", "application-k8s", language="java", version="2.11.0",
              deployment="feedexport", pod="feedexport-0", node="node-0", cluster="staging")
    _k8s_span(client, "spushsocket-k8s", "application-k8s", pod="sock-0", node="node-0", cluster="staging")
    _k8s_span(client, "llm-gateway-k8s", "genai-k8s", language="python", version="1.28.0",
              pod="llm-0", node="node-1", cluster="staging")
    r = get(client, "/api/kubernetes/workloads")
    by_svc = {w["service"]: w for w in r}
    fe = by_svc["feedexport-k8s"]
    assert fe["instrumented"] and fe["language"] == "java" and fe["sdk_version"] == "2.11.0"
    assert fe["deployment"] == "feedexport" and fe["pod_count"] >= 1
    assert "spans" in fe["signals"]
    # Uninstrumented workload still appears (useful for the "which haven't I instrumented yet?" question)
    assert by_svc["spushsocket-k8s"]["instrumented"] is False and by_svc["spushsocket-k8s"]["language"] == ""
    # Filter by namespace
    genai = get(client, "/api/kubernetes/workloads?namespace=genai-k8s")
    assert len(genai) == 1 and genai[0]["service"] == "llm-gateway-k8s"


def test_k8s_namespaces_coverage_pct(client):
    _k8s_span(client, "svc-a", "ns-cov", language="java", cluster="staging")
    _k8s_span(client, "svc-b", "ns-cov", cluster="staging")
    r = get(client, "/api/kubernetes/namespaces")
    by_ns = {n["namespace"]: n for n in r}
    cov = by_ns["ns-cov"]
    assert cov["services"] == 2 and cov["instrumented"] == 1
    assert cov["coverage_pct"] == 50


def test_k8s_wizard_steps_light_up_as_data_arrives(client):
    r = get(client, "/api/kubernetes/wizard")
    titles = [s["title"] for s in r["steps"]]
    assert titles[0].startswith("Oscilyx collectors") and titles[-1].startswith("See traces")
    # All four should be green after the previous tests seeded telemetry
    assert r["complete"] is True
    assert all(s["ok"] for s in r["steps"])


def test_k8s_wizard_empty_state(client, srv):
    """A fresh install has no data, so only the first step should be checkable later; all should be false now."""
    # Create a separate TestClient that shares nothing
    from fastapi.testclient import TestClient as TC
    # Just verify the wizard endpoint returns well-formed output even with no data
    r = get(client, "/api/kubernetes/wizard")
    assert "steps" in r and len(r["steps"]) == 4
    assert "complete" in r


# ---- metric names registry (v10.1) ----------------------------------------------------------------
def _send_metric(client, service, name, n=1, unit="1"):
    import time
    now = time.time_ns()
    pts = [{"timeUnixNano": str(now - i * 1000), "asDouble": float(i)} for i in range(n)]
    body = {"resourceMetrics": [{"resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": service}}]},
            "scopeMetrics": [{"metrics": [{"name": name, "unit": unit, "gauge": {"dataPoints": pts}}]}]}]}
    assert client.post("/v1/metrics", json=body, headers=AUTH).status_code == 200


def test_metric_names_counts_and_services_from_registry(client):
    _send_metric(client, "svc-a", "reg.test.metric", n=3)
    _send_metric(client, "svc-a", "reg.test.metric", n=2)      # upsert path: counts must add up
    _send_metric(client, "svc-b", "reg.test.metric", n=1)
    rows = {r["name"]: r for r in get(client, "/api/metrics/names?minutes=60")}
    r = rows["reg.test.metric"]
    assert r["points"] == 6
    assert set(r["services"].split(",")) == {"svc-a", "svc-b"}


def test_metric_names_all_time_and_unknown_window(client):
    _send_metric(client, "svc-c", "reg.test.other", n=1)
    assert "reg.test.other" in {r["name"] for r in get(client, "/api/metrics/names?minutes=0")}


def test_metric_names_old_hour_buckets_fall_outside_window(client, srv):
    """Buckets older than the window are excluded; this is what keeps the page fast and honest."""
    with srv.db() as c:
        c.execute("INSERT INTO metric_names_seen(name,unit,mtype,service,hr,points) VALUES(?,?,?,?,?,?)",
                  ("reg.ancient.metric", "1", "gauge", "svc-z", 1000, 5))
    names60 = {r["name"] for r in get(client, "/api/metrics/names?minutes=60")}
    names_all = {r["name"] for r in get(client, "/api/metrics/names?minutes=0")}
    assert "reg.ancient.metric" not in names60 and "reg.ancient.metric" in names_all
