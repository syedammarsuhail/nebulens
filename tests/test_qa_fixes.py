"""Fixes from the QA latency audit: operations from op_rollup, trace detail, errors-only traces, investigate, SLOs, query limit."""
import secrets
import time

H = {"Authorization": "Bearer tok"}


def test_operations_from_rollup(client, ingest, srv, monkeypatch):
    svc = "ops-" + secrets.token_hex(3)
    for i in range(5):
        ingest.span(svc, "app", error=(i < 2))
    seen = []
    real = srv.qrows
    monkeypatch.setattr(srv, "qrows", lambda sql, args=(): (seen.append(sql), real(sql, args))[1])
    monkeypatch.setattr(srv, "_ops_ready", lambda m: True)
    ops = client.get(f"/api/operations?service={svc}&minutes=60", headers=H).json()
    assert ops[0]["name"] == "op" and ops[0]["n"] == 5 and ops[0]["err"] == 2 and ops[0]["avg_ms"] > 0
    assert any("FROM op_rollup" in s for s in seen) and not any("FROM spans" in s for s in seen)


def test_trace_detail_and_error_traces(client, ingest):
    svc = "trc-" + secrets.token_hex(3)
    ingest.span(svc, "app", error=True)
    t = client.get(f"/api/traces?service={svc}&errors=1&minutes=60", headers=H).json()
    t = t if isinstance(t, list) else t.get("traces", [])
    assert t and t[0]["errors"] >= 1
    d = client.get(f"/api/traces/{t[0]['trace_id']}", headers=H).json()
    assert d["spans"] and "logs" in d


def test_investigate_intents_answer(client, ingest):
    ingest.span("inv-" + secrets.token_hex(3), "app", error=True)
    for qq in ("which services are slow", "what is failing", "any cpu saturation", "show top error logs", "overview"):
        r = client.get("/api/investigate", params={"q_": qq, "minutes": 60}, headers=H)
        assert r.status_code == 200 and "summary" in r.json(), qq


def test_slo_from_rollup(client, ingest, srv, monkeypatch):
    svc = "slo-" + secrets.token_hex(3)
    for i in range(4):
        ingest.span(svc, "app", error=(i == 0))
    assert client.post("/api/slos", json={"name": "slo " + svc, "service": svc, "objective_pct": 99, "latency_ms": 1000,
                                          "window_days": 1}, headers=H).status_code == 200
    monkeypatch.setattr(srv, "_rollup_range", lambda m: (0, 60.0))
    s = [x for x in client.get("/api/slos", headers=H).json() if x["service"] == svc][0]
    assert s["events"] == 4 and abs(s["attainment_pct"] - 75.0) < 1e-6


def test_slow_query_is_stopped_with_503(client, srv, monkeypatch):
    monkeypatch.setattr(srv, "QUERY_TIMEOUT", 0.05)

    def slow_ops(service, minutes=60):
        # a deliberately expensive read (recursive CTE) that the progress handler must interrupt
        srv.qrows("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT 50000000) SELECT SUM(x) FROM c")
        return []
    monkeypatch.setattr(srv, "operations", slow_ops)
    route = [r for r in srv.app.routes if getattr(r, "path", "") == "/api/operations"][0]
    monkeypatch.setattr(route, "endpoint", slow_ops)
    monkeypatch.setattr(route.dependant, "call", slow_ops)
    t = time.time()
    r = client.get("/api/operations?service=x", headers=H)
    assert r.status_code == 503 and r.json()["error"] == "query_timeout" and time.time() - t < 10


def test_trace_text_search(client, ingest, srv, monkeypatch):
    svc = "txt-" + secrets.token_hex(3)
    ingest.span(svc, "app")
    monkeypatch.setattr(srv, "_ops_ready", lambda m: True)
    by_name = client.get(f"/api/traces?service={svc}&q_=op&minutes=60", headers=H).json()
    by_name = by_name if isinstance(by_name, list) else by_name.get("traces", [])
    assert by_name, "operation name search finds the trace"
    tid = by_name[0]["trace_id"]
    by_id = client.get(f"/api/traces?q_={tid[:12]}&minutes=60", headers=H).json()
    by_id = by_id if isinstance(by_id, list) else by_id.get("traces", [])
    assert any(t["trace_id"] == tid for t in by_id)
    none = client.get(f"/api/traces?service={svc}&q_=no-such-operation-xyz&minutes=60", headers=H).json()
    none = none if isinstance(none, list) else none.get("traces", [])
    assert none == []


def test_search_finds_builtin_metrics(client):
    r = client.get("/api/search?q_=error_rate", headers=H).json()
    assert {"name": "nebulens.span.error_rate"} in r["metrics"]


def test_deleting_alert_removes_its_events(client, srv):
    rid = client.post("/api/alerts", json={"name": "qa-del-" + secrets.token_hex(3), "signal": "error_rate", "op": ">",
                                           "threshold": 0, "window_s": 300}, headers=H).json()
    rid = rid.get("id") or srv.q("SELECT MAX(id) id FROM alert_rules")[0]["id"]
    with srv.db() as c:
        c.execute("INSERT INTO alert_events(rule_id, ts, state, value, msg) VALUES(?,?,?,?,?)", (rid, int(time.time()), "firing", 1, "x"))
        c.execute("INSERT INTO alert_events(rule_id, ts, state, value, msg) VALUES(?,?,?,?,?)", (999999, int(time.time()), "firing", 1, "orphan"))
    assert client.delete(f"/api/alerts/{rid}", headers=H).status_code == 200
    left = srv.q("SELECT COUNT(*) n FROM alert_events WHERE rule_id IN (?, 999999)", (rid,))[0]["n"]
    assert left == 0
