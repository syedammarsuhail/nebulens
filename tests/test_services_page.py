"""Services page: one batch call for every service's health and trend, served from the rollups, same scores as before."""
import secrets, time
from conftest import AUTH


def _ready(monkeypatch, srv):
    monkeypatch.setattr(srv, "_rollup_range", lambda m: (srv.since_ns(m) // 60_000_000_000, m * 60))
    monkeypatch.setattr(srv, "_env_ready", lambda m: True)


def _spans(client, svc, n, errors=0, dur_ms=5):
    now = time.time_ns()
    recs = [{"traceId": secrets.token_hex(16), "spanId": secrets.token_hex(8), "parentSpanId": "", "name": "op", "kind": 2,
             "startTimeUnixNano": str(now - dur_ms * 1_000_000), "endTimeUnixNano": str(now), "status": {"code": 2 if i < errors else 0}}
            for i in range(n)]
    body = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": svc}}]},
                               "scopeSpans": [{"spans": recs}]}]}
    assert client.post("/v1/traces", json=body, headers=AUTH).status_code == 200


def test_summary_gives_the_same_health_as_the_old_per_service_call(srv, client, monkeypatch):
    svc = "health-" + secrets.token_hex(3)
    _spans(client, svc, 40, errors=4)                       # 10% errors: score 100 - min(40, 10*4) = 60, unhealthy
    slow = srv.service_health(svc, 60.0)                    # rollups not covering the test window: the raw-scan path
    _ready(monkeypatch, srv)
    fast = srv.services_summary(60.0, 30)[svc]
    assert slow["score"] == fast["health"]["score"] == 60.0 and fast["health"]["state"] == slow["state"] == "unhealthy"
    assert fast["health"]["signals"]["error_pct"] == slow["signals"]["error_pct"] == 10.0
    assert len(fast["ts"]) == 30 and abs(sum(p["rate"] for p in fast["ts"]) * (60 * 60 / 30) - 40) < 0.01
    assert srv.service_health(svc, 60.0)["score"] == 60.0   # the single-service endpoint uses the rollup too now


def test_summary_is_one_request_and_never_scans_spans(srv, client, monkeypatch):
    _spans(client, "summary-" + secrets.token_hex(3), 5)
    _ready(monkeypatch, srv)
    seen = []
    rq, rr = srv.q, srv.qrows
    monkeypatch.setattr(srv, "q", lambda sql, args=(): (seen.append(sql), rq(sql, args))[1])
    monkeypatch.setattr(srv, "qrows", lambda sql, args=(): (seen.append(sql), rr(sql, args))[1])
    r = client.get("/api/services-summary?minutes=60&buckets=30", headers=AUTH)
    assert r.status_code == 200 and r.json()
    assert not [s for s in seen if "FROM spans" in s or "FROM logs" in s]


def test_summary_is_empty_when_rollups_do_not_cover_the_window(srv, client, monkeypatch):
    monkeypatch.setattr(srv, "_rollup_range", lambda m: None)
    assert client.get("/api/services-summary?minutes=60", headers=AUTH).json() == {}    # the page then falls back per service
