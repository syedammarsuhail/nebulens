"""Integration detection: fast (registries, not raw-table scans) and correct (the right language, k8s from resource attributes)."""
import integrations as ints


def _f(langs=(), k8s=False, names=()):
    return {"metric_names": set(names), "langs": set(langs), "k8s": k8s, "minutes": 60, "attr_keys": set()}


def test_language_integrations_need_their_own_language(srv):
    py_only = _f(langs=["python"])
    assert srv._detect_integration(ints.by_id("python"), facts=py_only)[0] == "active"
    # any SDK language used to light up Java and Node.js as well
    assert srv._detect_integration(ints.by_id("java"), facts=py_only)[0] == "no_data"
    assert srv._detect_integration(ints.by_id("nodejs"), facts=py_only)[0] == "no_data"
    assert srv._detect_integration(ints.by_id("browser-rum"), facts=_f(langs=["webjs"]))[0] == "active"


def test_kubernetes_detected_from_pods_or_nodes(srv):
    assert srv._detect_integration(ints.by_id("kubernetes"), facts=_f(k8s=True))[0] == "active"
    assert srv._detect_integration(ints.by_id("kubernetes"), facts=_f())[0] == "no_data"
    # a Kubernetes metric alone is enough too
    assert srv._detect_integration(ints.by_id("kubernetes"), facts=_f(names=["k8s.pod.cpu.usage"]))[0] == "active"


def test_live_detection_from_ingest(client, ingest, srv):
    ingest.span("detect-py", "detect-ns", **{"telemetry.sdk.language": "python", "k8s.pod.name": "detect-py-0"})
    d = client.get("/api/integrations/python", headers={"Authorization": "Bearer tok"}).json()
    assert d["state"] == "active" and "telemetry.sdk.language=python" in d["detected"]["attributes"]
    k = client.get("/api/integrations/kubernetes", headers={"Authorization": "Bearer tok"}).json()
    assert k["state"] == "active"


def test_list_reads_registries_once_not_raw_tables_per_integration(client, srv, monkeypatch):
    seen = []
    real = srv.q

    def spy(sql, args=()):
        seen.append(sql)
        return real(sql, args)
    monkeypatch.setattr(srv, "q", spy)
    r = client.get("/api/integrations", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200 and len(r.json()) >= 9
    raw = [s for s in seen if any(f"FROM {t} " in s for t in ("spans", "logs", "metrics"))]
    # at most one small sample per raw table (only for integrations detected by arbitrary attributes)
    assert len(raw) <= 3, raw


def test_map_nodes_from_registry_not_span_scan(client, ingest, srv, monkeypatch):
    ingest.span("map-meta-svc", "map-ns", **{"telemetry.sdk.language": "java", "service.version": "3.1.4"})
    seen = []
    real = srv.qrows

    def spy(sql, args=()):
        seen.append(sql)
        return real(sql, args)
    monkeypatch.setattr(srv, "qrows", spy)
    rows = client.get("/api/map/nodes?minutes=60", headers={"Authorization": "Bearer tok"}).json()
    m = {r["service"]: r for r in rows}["map-meta-svc"]
    assert m["language"] == "java" and m["namespace"] == "map-ns" and m["version"] == "3.1.4"
    assert not [s for s in seen if "FROM spans" in s]      # the whole-window span scan took over a minute at scale
