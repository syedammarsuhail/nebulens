"""Removing an environment: from the list only (data kept), or with all its data (admin only, permanent)."""
import secrets, time
from conftest import AUTH


def _send(client, env, n=3):
    now = time.time_ns()
    kv = lambda k, v: {"key": k, "value": {"stringValue": v}}
    res = {"attributes": [kv("service.name", "svc-" + env), kv("deployment.environment", env), kv("k8s.namespace.name", "ns-" + env)]}
    spans = [{"traceId": secrets.token_hex(16), "spanId": secrets.token_hex(8), "parentSpanId": "", "name": "op", "kind": 2,
              "startTimeUnixNano": str(now - 1_000_000), "endTimeUnixNano": str(now), "status": {"code": 0}} for _ in range(n)]
    assert client.post("/v1/traces", json={"resourceSpans": [{"resource": res, "scopeSpans": [{"spans": spans}]}]}, headers=AUTH).status_code == 200
    logs = [{"timeUnixNano": str(now), "severityNumber": 9, "body": {"stringValue": "x"}} for _ in range(n)]
    assert client.post("/v1/logs", json={"resourceLogs": [{"resource": res, "scopeLogs": [{"logRecords": logs}]}]}, headers=AUTH).status_code == 200
    m = {"name": "env.test.metric", "gauge": {"dataPoints": [{"timeUnixNano": str(now), "asDouble": 1.0}]}}
    assert client.post("/v1/metrics", json={"resourceMetrics": [{"resource": res, "scopeMetrics": [{"metrics": [m]}]}]}, headers=AUTH).status_code == 200


def _slug_of(srv, env):
    return srv.q("SELECT DISTINCT env FROM spans WHERE service=?", ("svc-" + env,))[0]["env"]


def _count(srv, slug):
    return {t: srv.q(f"SELECT COUNT(*) n FROM {t} WHERE env=?", (slug,))[0]["n"]
            for t in ("spans", "logs", "metrics", "span_rollup", "env_rollup", "resource_seen", "services_seen")}


def test_remove_from_list_keeps_the_data(srv, client):
    env = "keep-" + secrets.token_hex(3)
    _send(client, env)
    slug = _slug_of(srv, env)
    assert client.delete(f"/api/environments/{slug}", headers=AUTH).status_code == 200
    assert not srv.q("SELECT 1 FROM environments WHERE slug=?", (slug,))
    assert _count(srv, slug)["spans"] == 3                     # data untouched


def test_remove_and_delete_its_data(srv, client):
    env = "purge-" + secrets.token_hex(3)
    _send(client, env)
    other = "stay-" + secrets.token_hex(3)
    _send(client, other)
    slug, other_slug = _slug_of(srv, env), _slug_of(srv, other)
    before = _count(srv, slug)
    assert before["spans"] == 3 and before["logs"] == 3 and before["metrics"] == 1 and before["span_rollup"] >= 1
    r = client.post(f"/api/environments/{slug}/purge", headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] in ("deleting", "already deleting")
    for _ in range(100):
        if not any(_count(srv, slug).values()) and slug not in srv._purging:
            break
        time.sleep(0.05)
    assert not any(_count(srv, slug).values()), _count(srv, slug)
    assert not srv.q("SELECT 1 FROM environments WHERE slug=?", (slug,))
    assert _count(srv, other_slug)["spans"] == 3               # other environments are untouched
    assert client.post(f"/api/environments/{slug}/purge", headers=AUTH).status_code == 404


def test_deleting_data_needs_admin(srv):
    assert srv._required_role("POST", "/api/environments/prod/purge") == "admin"
    assert srv._required_role("DELETE", "/api/environments/prod") == "editor"     # removing from the list only
