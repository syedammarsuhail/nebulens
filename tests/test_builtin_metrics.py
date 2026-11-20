"""Built-in metrics derived from spans and logs: listed, queryable like any metric, usable in alerts."""
H = {"Authorization": "Bearer tok"}


def test_builtin_metrics_listed(client):
    names = {m["name"]: m for m in client.get("/api/metrics/names?minutes=60", headers=H).json()}
    for n in ("nebulens.span.requests", "nebulens.span.error_rate", "nebulens.span.duration.p95", "nebulens.log.errors"):
        assert names[n]["mtype"] == "built-in" and names[n]["description"]


def test_builtin_span_and_log_metrics_from_ingest(client, ingest):
    for i in range(4):
        ingest.span("bm-svc", "bm-ns", error=(i == 0))
    ingest.logs("bm-svc", "bm-ns", "boom", sev=17)
    ingest.logs("bm-svc", "bm-ns", "fine", sev=9)

    def total(name, agg="avg"):
        d = client.get(f"/api/metrics/query?name={name}&service=bm-svc&minutes=10&buckets=1", headers=H).json()
        vals = [v for v in d["series"].get("bm-svc", []) if v is not None]
        assert vals, (name, d)
        return vals[0]
    secs = 600.0
    assert abs(total("nebulens.span.requests") * secs - 4) < 1e-6
    assert abs(total("nebulens.span.errors") * secs - 1) < 1e-6
    assert abs(total("nebulens.span.error_rate") - 25.0) < 1e-6
    assert total("nebulens.span.duration.avg") > 0 and total("nebulens.span.duration.p95") > 0
    assert abs(total("nebulens.log.records") * secs - 2) < 1e-6
    assert abs(total("nebulens.log.errors") * secs - 1) < 1e-6


def test_builtin_metric_drives_alerts(srv, ingest):
    for _ in range(3):
        ingest.span("bm-alert", "bm-ns", error=True)
    v = srv.eval_signal("metric_avg", "nebulens.span.error_rate", "bm-alert", 300)
    assert v == 100.0
    assert srv.eval_signal("metric_max", "nebulens.span.requests", "bm-alert", 300) > 0
