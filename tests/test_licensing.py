"""Signed licences (tier, nodes, expiry), no unlock switch, node usage, Enterprise-only features."""
import os
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from conftest import AUTH, _test_license


def test_valid_keys_carry_tier_nodes_and_expiry(srv):
    lic = srv._parse_license(_test_license("pro", nodes=12, days=10))
    assert lic["tier"] == "pro" and lic["nodes"] == 12 and lic["exp"] > 0
    assert srv._parse_license(_test_license("enterprise", nodes=0))["tier"] == "enterprise"


@pytest.mark.parametrize("bad", ["", "anything", "NEBULENS-AAAA-BBBB-CCCC-DDDDDDDD", "NBL1.e30.AAAA"])
def test_garbage_is_not_a_licence(srv, bad):
    assert srv._parse_license(bad) is None


def test_expired_tampered_and_foreign_keys_are_rejected(srv):
    assert srv._parse_license(_test_license("pro", days=-1)) is None                       # expired
    good = _test_license("pro", nodes=5)
    head, body, sig = good.split(".")
    tampered = srv.base64.urlsafe_b64encode(srv.base64.urlsafe_b64decode(body + "==").replace(b'"n": 5', b'"n": 500')).rstrip(b"=").decode()
    assert srv._parse_license(f"{head}.{tampered}.{sig}") is None                           # payload edited
    assert srv._parse_license(_test_license("pro", key=Ed25519PrivateKey.generate())) is None   # signed by someone else


def test_no_environment_switch_unlocks_pro():
    # NEBULENS_TIER=pro used to unlock Pro with no licence; outside the test suite only a signed key counts
    import tempfile
    base = {k: v for k, v in os.environ.items() if not k.startswith(("NEBULENS_LICENSE", "NEBULENS_TEST_LICENSE"))}
    import sqlite3
    db = os.path.join(tempfile.mkdtemp(), "expired.db")      # an install whose 14-day trial ended long ago
    c = sqlite3.connect(db); c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("INSERT INTO meta VALUES('trial_start', '1000')"); c.commit(); c.close()
    base["NEBULENS_DB"] = db
    code = "import server; print(server.TIER, server.IS_PRO, server._LICENSE_VALID)"
    run = lambda extra: subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**base, **extra}, check=True,
                                       cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))).stdout.split()[-3:]
    plain = run({})
    switched = run({"NEBULENS_TIER": "pro", "NEBULENS_LICENSE_KEY": "anything-at-all"})
    assert plain == ["community", "False", "False"]       # trial over and no licence: Community
    assert switched == plain                              # the old switch and an arbitrary key change nothing


def test_license_endpoint_and_over_limit_banner_data(client, ingest, srv, monkeypatch):
    for node in ("node-a", "node-b", "node-c"):
        ingest.span("lic-" + node, "app", **{"k8s.node.name": node})
    srv._res_flush()
    monkeypatch.setattr(srv, "LICENSED_NODES", 2)
    r = client.get("/api/license", headers=AUTH).json()
    assert r["nodes_licensed"] == 2 and r["nodes_used"] >= 3 and r["over_limit"] is True and r["pricing_url"]
    monkeypatch.setattr(srv, "LICENSED_NODES", 0)                                           # unlimited
    assert client.get("/api/license", headers=AUTH).json()["over_limit"] is False


def test_per_environment_access_is_enterprise(client, srv, monkeypatch):
    monkeypatch.setattr(srv, "IS_ENTERPRISE", False)
    monkeypatch.setattr(srv, "IS_PRO", True)
    monkeypatch.setattr(srv, "TIER", "pro")
    r = client.get("/api/environments/access", headers=AUTH)
    assert r.status_code == 403 and r.json()["error"] == "enterprise_required"


def test_community_has_no_service_cap(client, ingest, srv, monkeypatch):
    monkeypatch.setattr(srv, "IS_PRO", False)
    monkeypatch.setattr(srv, "IS_ENTERPRISE", False)
    for i in range(15):                                                                     # was capped at 10
        ingest.span(f"nocap-{i}", "app")
    assert srv.TIER_LIMITS["max_nodes"] == 5 and "max_services" not in srv.TIER_LIMITS


def test_container_host_names_are_not_counted_as_nodes(client, ingest, srv):
    for h in ("3f2a9c1b7d00", "8e1d4b2c9a11"):                 # inside Docker host.name is the container ID
        ingest.span("dockersvc-" + h, "op", **{"host.name": h})
    srv._res_flush()
    nodes = {r[0] for r in srv.qrows("SELECT DISTINCT node FROM resource_seen WHERE node<>''")}
    assert not ({"3f2a9c1b7d00", "8e1d4b2c9a11"} & nodes)


def test_limit_message_uses_readable_names(client, srv, monkeypatch):
    monkeypatch.setattr(srv, "IS_PRO", False)
    monkeypatch.setitem(srv.TIER_LIMITS, "max_slos", 0)
    r = client.post("/api/slos", headers=AUTH, json={"name": "x", "service": "a", "objective_pct": 99.0, "latency_ms": 300})
    assert r.status_code == 403 and "3 slos" not in r.json()["message"] and "SLOs" in r.json()["message"]
