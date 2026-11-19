"""Shared fixtures. The environment must be configured BEFORE server.py is imported (it reads it at import time).

Run against SQLite (default):   pytest
Run against Postgres:           NEBULENS_TEST_PG_URL=postgresql://user:pw@host/db pytest
The same suite runs on both, which is how SQL that only works on one database gets caught.
"""
import importlib.util, os, pathlib, tempfile
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_TMP = tempfile.mkdtemp(prefix="nebulens-test-")
PG_URL = os.environ.get("NEBULENS_TEST_PG_URL", "")
os.environ.update({
    "NEBULENS_TOKEN": "tok", "NEBULENS_TIER": "pro", "NEBULENS_CACHE_TTL": "0",
    "NEBULENS_CLUSTER_NAME": "test-cluster", "NEBULENS_DB": os.path.join(_TMP, "nebulens.db"),
    "NEBULENS_REDACT": "card,ssn,iban,jwt,apikey", "NEBULENS_AUDIT_RETENTION_DAYS": "0",
})
if PG_URL:
    os.environ["NEBULENS_DATABASE_URL"] = PG_URL
    os.environ["NEBULENS_PG_SCHEMA"] = "nebulens_test"
else:
    os.environ.pop("NEBULENS_DATABASE_URL", None)

AUTH = {"Authorization": "Bearer tok"}


def pytest_collection_modifyitems(config, items):
    if PG_URL:
        skip = pytest.mark.skip(reason="SQLite-only test")
        for it in items:
            if "sqlite_only" in it.keywords:
                it.add_marker(skip)


@pytest.fixture(scope="session")
def srv():
    spec = importlib.util.spec_from_file_location("nebulens_server", ROOT / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def client(srv):
    from fastapi.testclient import TestClient
    with TestClient(srv.app) as c:
        yield c


def attrs(**kw):
    return [{"key": k, "value": {"stringValue": v}} for k, v in kw.items()]


@pytest.fixture(scope="session")
def ingest(client):
    """Helpers that post OTLP/JSON the way a collector does."""
    import secrets, time

    def logs(service, ns, body, sev=0, **extra):
        rec = {"timeUnixNano": str(time.time_ns()), "severityNumber": sev, "body": {"stringValue": body},
               "attributes": attrs(**extra)}
        res = {"resourceLogs": [{"resource": {"attributes": attrs(**{"service.name": service, "k8s.namespace.name": ns})},
                                 "scopeLogs": [{"logRecords": [rec]}]}]}
        r = client.post("/v1/logs", json=res, headers=AUTH)
        assert r.status_code == 200, r.text

    def span(service, ns, error=False, **res_extra):
        now = time.time_ns()
        sp = {"traceId": secrets.token_hex(16), "spanId": secrets.token_hex(8), "name": "op", "kind": 2, "parentSpanId": "",
              "startTimeUnixNano": str(now - 5_000_000), "endTimeUnixNano": str(now), "status": {"code": 2 if error else 0}}
        res = {"resourceSpans": [{"resource": {"attributes": attrs(**{"service.name": service, "k8s.namespace.name": ns}, **res_extra)},
                                  "scopeSpans": [{"spans": [sp]}]}]}
        r = client.post("/v1/traces", json=res, headers=AUTH)
        assert r.status_code == 200, r.text

    def metric(service, ns, name="cpu.load", value=1.5):
        now = time.time_ns()
        res = {"resourceMetrics": [{"resource": {"attributes": attrs(**{"service.name": service, "k8s.namespace.name": ns})},
                                    "scopeMetrics": [{"metrics": [{"name": name, "unit": "1",
                                        "gauge": {"dataPoints": [{"timeUnixNano": str(now), "asDouble": value}]}}]}]}]}
        r = client.post("/v1/metrics", json=res, headers=AUTH)
        assert r.status_code == 200, r.text

    return type("Ingest", (), {"logs": staticmethod(logs), "span": staticmethod(span), "metric": staticmethod(metric)})
