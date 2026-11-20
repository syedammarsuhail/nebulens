"""Span rollup: RED numbers read from span_rollup must match a raw scan of spans."""
import secrets, time
import pytest
from conftest import AUTH

MIN = 60_000_000_000


def _insert_spans(srv, svc, offsets_min, durs_ms, errors=(), env=""):
    """Insert root spans directly (as ingest would), returning the rollup rows written alongside."""
    now = time.time_ns()
    rows, spans = [], []
    for i, (off, dur) in enumerate(zip(offsets_min, durs_ms)):
        st = now - int(off * MIN)
        status = 2 if i in errors else 0
        rows.append((secrets.token_hex(16), secrets.token_hex(8), "", svc, "op", 2, st, st + int(dur * 1e6), dur, status,
                     "", "{}", "{}", env))
        spans.append((svc, env, st, status, dur))
    with srv.db() as c:
        c.executemany("INSERT INTO spans(trace_id,span_id,parent_id,service,name,kind,start_ns,end_ns,dur_ms,status,status_msg,attrs,res,env) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return spans


def _one(rows, svc):
    return next(r for r in rows if r["service"] == svc)


def test_ingest_writes_rollup_and_matches_raw(srv, client, ingest):
    svc = "rollup-live-" + secrets.token_hex(3)
    for i in range(30):
        ingest.span(svc, "app", error=(i % 5 == 0))
    rows = srv.q("SELECT SUM(n) n, SUM(err) err FROM span_rollup WHERE service=?", (svc,))[0]
    assert rows["n"] == 30 and rows["err"] == 6
    via_rollup = _one(client.get("/api/services?minutes=60", headers=AUTH).json(), svc)
    assert via_rollup["n"] == 30 and via_rollup["err"] == 6 and via_rollup["err_pct"] == pytest.approx(20)
    assert via_rollup["avg_ms"] == pytest.approx(5, rel=0.05)      # every test span lasts 5ms


def test_child_spans_are_not_counted(srv, client):
    svc = "rollup-child-" + secrets.token_hex(3)
    now = time.time_ns()
    def sp(kind, parent):
        return {"traceId": secrets.token_hex(16), "spanId": secrets.token_hex(8), "parentSpanId": parent, "name": "x", "kind": kind,
                "startTimeUnixNano": str(now - 5_000_000), "endTimeUnixNano": str(now), "status": {"code": 0}}
    body = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": svc}}]},
                               "scopeSpans": [{"spans": [sp(1, "abcd"), sp(3, "abcd"), sp(2, "abcd"), sp(1, "")]}]}]}
    assert client.post("/v1/traces", json=body, headers=AUTH).status_code == 200
    # counted: the server span and the parentless root. Not counted: internal and client spans that have a parent.
    assert srv.q("SELECT SUM(n) n FROM span_rollup WHERE service=?", (svc,))[0]["n"] == 2


def test_percentiles_close_to_raw(srv, monkeypatch):
    svc = "rollup-pct-" + secrets.token_hex(3)
    durs = [float(d) for d in range(1, 201)]                     # 1..200 ms, uniform
    spans = _insert_spans(srv, svc, [10 + (i % 20) for i in range(200)], durs, errors={0, 1, 2})
    with srv.db() as c:
        srv._rollup_write(c, srv._rollup_aggregate(spans))
    fast = _one(srv.services(60), svc)
    monkeypatch.setattr(srv, "_rollup_range", lambda m: None)    # force the raw path
    raw = _one(srv.services(60), svc)
    assert fast["n"] == raw["n"] == 200 and fast["err"] == raw["err"] == 3
    assert fast["avg_ms"] == pytest.approx(raw["avg_ms"], rel=1e-6)
    assert fast["max_ms"] == pytest.approx(raw["max_ms"])
    for k in ("p50", "p95", "p99"):
        assert fast[k] == pytest.approx(raw[k], rel=0.08), k


def test_env_scope_applies_to_rollup(srv, monkeypatch):
    svc = "rollup-env-" + secrets.token_hex(3)
    a = _insert_spans(srv, svc, [5] * 4, [10.0] * 4, env="prod")
    b = _insert_spans(srv, svc, [5] * 6, [10.0] * 6, env="dev")
    with srv.db() as c:
        srv._rollup_write(c, srv._rollup_aggregate(a + b))
    assert _one(srv.services(60, env="prod"), svc)["n"] == 4
    assert _one(srv.services(60, env="dev"), svc)["n"] == 6


def test_short_windows_and_uncovered_windows_use_raw_scan(srv, monkeypatch):
    assert srv._rollup_range(1) is None                            # below the minimum window
    monkeypatch.setattr(srv, "_rollup_from", lambda: int(time.time() // 60) + 5)
    assert srv._rollup_range(60) is None                           # rollup does not cover the window yet


def test_service_timeseries_matches_raw(srv, monkeypatch):
    svc = "rollup-ts-" + secrets.token_hex(3)
    spans = _insert_spans(srv, svc, [3, 3, 20, 20, 20, 40], [10.0] * 6, errors={2})
    with srv.db() as c:
        srv._rollup_write(c, srv._rollup_aggregate(spans))
    fast = srv.service_ts(svc, minutes=60, buckets=20)             # 3-minute buckets
    assert sum(p["rate"] for p in fast) * (60 * 60 / 20) == pytest.approx(6, abs=0.01)
    assert sum(p["err"] for p in fast) * (60 * 60 / 20) == pytest.approx(1, abs=0.01)


def test_backfill_converts_history_and_moves_coverage_back(srv, monkeypatch):
    svc = "rollup-bf-" + secrets.token_hex(3)
    spans = _insert_spans(srv, svc, [20, 21, 22, 35, 36, 50], [4.0, 8.0, 16.0, 32.0, 64.0, 128.0], errors={1})
    assert srv.q("SELECT COUNT(*) n FROM span_rollup WHERE service=?", (svc,))[0]["n"] == 0   # direct insert: no rollup yet
    now_m = int(time.time() // 60)
    srv.q("SELECT 1")
    with srv.db() as c:
        c.execute("UPDATE rollup_state SET value=? WHERE key='from_minute'", (now_m,))
    srv._rollup_backfill(chunk_min=15, pause=0)
    assert srv._rollup_from() <= now_m - 50
    r = srv.q("SELECT SUM(n) n, SUM(err) err FROM span_rollup WHERE service=?", (svc,))[0]
    assert r["n"] == 6 and r["err"] == 1
    srv._rollup_backfill(chunk_min=15, pause=0)                    # a second run must not double count
    assert srv.q("SELECT SUM(n) n FROM span_rollup WHERE service=?", (svc,))[0]["n"] == 6


# ---- edge rollup (service map)
def _edge_spans(srv, a, b, db_name, offsets_min, errors=()):
    """For each offset: a root span in `a`, its child in `b` (child stored BEFORE its parent, like real exporters),
    and a client span in `b` calling database `db_name`."""
    now = time.time_ns()
    rows = []
    for i, off in enumerate(offsets_min):
        st = now - int(off * MIN)
        tid, pid, cid = secrets.token_hex(16), secrets.token_hex(8), secrets.token_hex(8)
        err = 2 if i in errors else 0
        rows.append((tid, cid, pid, b, "child", 2, st + 1_000_000, st + 9_000_000, 8.0, err, "", "{}", "{}", ""))
        rows.append((tid, secrets.token_hex(8), cid, b, "q", 3, st + 2_000_000, st + 4_000_000, 2.0, 0, "",
                     '{"db.system": "%s"}' % db_name, "{}", ""))
        rows.append((tid, pid, "", a, "root", 2, st, st + 12_000_000, 12.0, 0, "", "{}", "{}", ""))
    with srv.db() as c:
        c.executemany("INSERT INTO spans(trace_id,span_id,parent_id,service,name,kind,start_ns,end_ns,dur_ms,status,status_msg,attrs,res,env) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def _rewind_edges(srv, minutes_back=60):
    """Reprocess the last hour from scratch: the background worker may already have moved the cursor past spans a test inserts."""
    with srv._edge_lock:
        srv._edge_rollup_step_locked(time.time_ns(), 0)      # make sure the cursor row exists
        with srv.db() as c:
            c.execute("DELETE FROM edge_rollup")
            c.execute("UPDATE rollup_state SET value=? WHERE key='edge_cursor'",
                      (int((time.time_ns() - minutes_back * MIN) // MIN * MIN),))


def _edge_map(rows, a, b, db_name):
    got = {(e["src"], e["dst"]): e for e in rows}
    return got.get((a, b)), got.get((b, db_name))


def test_edge_rollup_matches_raw_map(srv, monkeypatch):
    a, b, dbn = "edge-a-" + secrets.token_hex(3), "edge-b-" + secrets.token_hex(3), "pg-" + secrets.token_hex(3)
    _edge_spans(srv, a, b, dbn, [10, 11, 12, 30, 31, 45], errors={1})
    monkeypatch.setattr(srv, "_EDGE_LAG_NS", 0)
    _rewind_edges(srv)
    srv._edge_rollup_step(max_slices=500)
    assert srv._edge_ready(60) is not None
    fast = srv.service_map(60)
    monkeypatch.setattr(srv, "_edge_ready", lambda m: None)      # force the raw join
    raw = srv.service_map(60)
    fa, fd = _edge_map(fast, a, b, dbn)
    ra, rd = _edge_map(raw, a, b, dbn)
    assert fa and ra and fd and rd
    assert fa["calls"] == ra["calls"] == 6 and fa["err"] == ra["err"] == 1
    assert fa["avg_ms"] == pytest.approx(ra["avg_ms"], rel=1e-6)
    assert fd["calls"] == rd["calls"] == 6 and fd["avg_ms"] == pytest.approx(2.0)


def test_edge_step_is_idempotent_and_only_counts_each_slice_once(srv, monkeypatch):
    monkeypatch.setattr(srv, "_EDGE_LAG_NS", 0)
    a, b = "edge-c-" + secrets.token_hex(3), "edge-d-" + secrets.token_hex(3)
    _edge_spans(srv, a, b, "mq", [20, 21])
    _rewind_edges(srv)
    srv._edge_rollup_step(max_slices=500)
    n1 = srv.q("SELECT SUM(calls) n FROM edge_rollup WHERE src=? AND dst=?", (a, b))[0]["n"]
    srv._edge_rollup_step(max_slices=500); srv._edge_rollup_step(max_slices=500)
    assert n1 == 2 and srv.q("SELECT SUM(calls) n FROM edge_rollup WHERE src=? AND dst=?", (a, b))[0]["n"] == 2


def test_map_includes_calls_newer_than_the_cursor(srv, monkeypatch):
    monkeypatch.setattr(srv, "_EDGE_LAG_NS", 0)
    monkeypatch.setattr(srv, "_MAP_TAIL", True)                      # opt-in: exact to the second
    srv._edge_rollup_step(max_slices=500)
    a, b = "edge-e-" + secrets.token_hex(3), "edge-f-" + secrets.token_hex(3)
    _edge_spans(srv, a, b, "kafka", [0])                         # stamped now, so always at or after the cursor: only the raw tail can see it
    assert srv._edge_ready(60) is not None
    assert any(e["src"] == a and e["dst"] == b and e["calls"] == 1 for e in srv.service_map(60))


def test_search_uses_registries_and_bounds_traces(client, ingest):
    svc = "searchable-" + secrets.token_hex(3)
    ingest.span(svc, "app")
    ingest.metric(svc, "app", name="search.metric." + svc)
    r = client.get(f"/api/search?q_={svc}", headers=AUTH).json()
    assert {"name": svc} in r["services"] and r["metrics"]
    found = client.get("/api/search?q_=op", headers=AUTH).json()["traces"]          # test spans are named "op"
    assert found and all("op" in t["name"] for t in found)
    tid = found[0]["trace_id"]
    by_id = client.get(f"/api/search?q_={tid[:10]}", headers=AUTH).json()["traces"]   # a trace-id fragment uses the index
    assert any(t["trace_id"] == tid for t in by_id)


def test_session_secret_persists_beside_the_database(srv):
    import os
    assert srv._load_session_secret() == srv._load_session_secret()          # stable across calls (and so across restarts)
    if not os.environ.get("NEBULENS_SESSION_SECRET") and not srv.USE_PG:
        assert os.path.exists(srv.DB_PATH + ".session-secret")


def test_logs_rows_carry_k8s_context_not_the_whole_resource(client, ingest):
    svc = "ctx-" + secrets.token_hex(3)
    ingest.logs(svc, "shop", "boom", sev=17, **{"k8s.pod.name": "pod-" + svc})
    rows = client.get(f"/api/logs?minutes=5&service={svc}", headers=AUTH).json()
    assert rows and rows[0]["k8s"]["k8s.namespace.name"] == "shop" and "res" not in rows[0]
    assert rows[0]["k8s"].get("k8s.pod.name") == "pod-" + svc


# ---- the fast paths must actually be the ones taken (a result that matches the raw scan proves nothing about that)
def _spy(monkeypatch, srv, name):
    calls = []
    orig = getattr(srv, name)
    monkeypatch.setattr(srv, name, lambda *a, **k: (calls.append(1), orig(*a, **k))[1])
    return calls


def test_services_uses_the_rollup_and_never_scans_spans(srv, monkeypatch):
    used = _spy(monkeypatch, srv, "_rollup_services")
    real_qrows = srv.qrows
    scanned = []
    monkeypatch.setattr(srv, "qrows", lambda sql, args=(): (scanned.append(sql), real_qrows(sql, args))[1])
    srv.services(60.0)
    assert used and not any("FROM spans" in s for s in scanned)


def test_home_insights_timeseries_and_map_use_their_rollups(srv, monkeypatch):
    w, ser = _spy(monkeypatch, srv, "_rollup_window"), _spy(monkeypatch, srv, "_rollup_series")
    srv.insights(30.0); srv.home(60.0, 60); srv.service_ts("anything", minutes=60, buckets=20)
    assert w and len(ser) >= 2
    monkeypatch.setattr(srv, "_EDGE_LAG_NS", 0)
    srv._edge_rollup_step(max_slices=500)
    tail = _spy(monkeypatch, srv, "_edge_slice")
    monkeypatch.setattr(srv, "_MAP_TAIL", False)
    srv.service_map(60.0)
    assert not tail                          # "never": no raw join on the request path at all
    monkeypatch.setattr(srv, "_MAP_TAIL", "auto"); monkeypatch.setattr(srv, "_MAP_TAIL_MAX", 0)
    srv.service_map(60.0)
    assert not tail                          # "auto" under heavy traffic: skipped
    monkeypatch.setattr(srv, "_MAP_TAIL_MAX", 10 ** 9)
    srv.service_map(60.0)
    assert len(tail) == 1                    # "auto" with light traffic: the newest minutes are included
    tail.clear()
    monkeypatch.setattr(srv, "_MAP_TAIL", True)
    srv.service_map(60.0)
    assert len(tail) == 1                    # opt-in: only the not-yet-rolled-up tail is joined raw


# ---- concurrency: Postgres aborted one of two simultaneous batches with "deadlock detected" and the 400 made senders drop it
def test_rollup_rows_are_written_in_primary_key_order(srv):
    now = time.time_ns()
    spans = [(s, e, now - m * MIN, 0, d) for m in (3, 1, 2) for s in ("zeta", "alpha", "mid") for e in ("b", "") for d in (2.0, 900.0, 40.0)]
    rows = srv._rollup_aggregate(spans)
    assert len(rows) > 10 and rows == sorted(rows)       # a fixed lock order means two batches can never wait on each other in a cycle


def test_transient_database_errors_are_recognised(srv):
    class DeadlockDetected(Exception):
        pass
    assert srv._db_busy(DeadlockDetected("x")) and srv._db_busy(Exception("ERROR: deadlock detected")) \
        and srv._db_busy(Exception("database is locked"))
    assert not srv._db_busy(ValueError("bad payload")) and not srv._db_busy(KeyError("resourceSpans"))


def _span_body(svc):
    now = time.time_ns()
    return {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": svc}}]},
                               "scopeSpans": [{"spans": [{"traceId": secrets.token_hex(16), "spanId": secrets.token_hex(8), "name": "op", "kind": 2,
                                                          "parentSpanId": "", "startTimeUnixNano": str(now - 5_000_000), "endTimeUnixNano": str(now),
                                                          "status": {"code": 0}}]}]}]}


def test_deadlock_is_retried_once_and_the_batch_is_stored_exactly_once(srv, client, monkeypatch):
    class DeadlockDetected(Exception):
        pass
    svc = "retry-" + secrets.token_hex(3)
    real, calls = srv.ingest_traces, []

    def flaky(d):
        calls.append(1)
        if len(calls) == 1:
            raise DeadlockDetected("deadlock detected")
        return real(d)
    monkeypatch.setattr(srv, "ingest_traces", flaky)
    assert client.post("/v1/traces", json=_span_body(svc), headers=AUTH).status_code == 200
    assert len(calls) == 2 and srv.q("SELECT COUNT(*) n FROM spans WHERE service=?", (svc,))[0]["n"] == 1


def test_persistent_database_trouble_is_503_not_400_and_bad_payloads_stay_400(srv, client, monkeypatch):
    class DeadlockDetected(Exception):
        pass

    def always(d):
        raise DeadlockDetected("deadlock detected")
    monkeypatch.setattr(srv, "ingest_traces", always)
    r = client.post("/v1/traces", json=_span_body("x"), headers=AUTH)
    assert r.status_code == 503                         # OTLP senders retry 503; they drop 400
    monkeypatch.setattr(srv, "ingest_traces", lambda d: (_ for _ in ()).throw(KeyError("resourceSpans")))
    assert client.post("/v1/traces", json=_span_body("x"), headers=AUTH).status_code == 400
