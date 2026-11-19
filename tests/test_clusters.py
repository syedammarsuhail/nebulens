"""Multi-cluster console: per-cluster ingest tokens, server-enforced cluster identity, admin-only management."""
import json, time
import pytest
from conftest import AUTH


def _otlp_log(body, ns="application", svc="orders", **res):
    attrs = {"service.name": svc, "k8s.namespace.name": ns, **res}
    return {"resourceLogs": [{"resource": {"attributes": [{"key": k, "value": {"stringValue": v}} for k, v in attrs.items()]},
                              "scopeLogs": [{"logRecords": [{"timeUnixNano": str(time.time_ns()), "severityNumber": 17,
                                                             "severityText": "ERROR", "body": {"stringValue": body}}]}]}]}


def _bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def _add(client, name, note=""):
    r = client.post("/api/clusters", json={"name": name, "note": note}, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def test_create_list_and_token_shown_once(client):
    c = _add(client, "Prod East", "primary")
    assert c["slug"] == "prod-east" and c["token"].startswith("p0c_") and len(c["token"]) > 30
    lst = client.get("/api/clusters", headers=AUTH).json()
    row = next(x for x in lst["clusters"] if x["slug"] == "prod-east")
    assert row["status"] == "waiting" and row["note"] == "primary" and row["name"] == "Prod East"
    assert row["token_prefix"] == c["token"][:8]
    assert c["token"] not in json.dumps(lst)          # the full token is never returned again
    assert lst["ingest_auth_required"] is True        # conftest sets NEBULENS_TOKEN


def test_duplicate_and_invalid_names(client):
    _add(client, "dup-cluster")
    assert client.post("/api/clusters", json={"name": "Dup Cluster"}, headers=AUTH).status_code == 409
    assert client.post("/api/clusters", json={"name": "!!!"}, headers=AUTH).status_code == 400


def test_ingest_is_bound_to_the_cluster_whatever_the_sender_claims(client, srv):
    c = _add(client, "bound-east")
    body = "bound-east-marker-1"
    r = client.post("/v1/logs", json=_otlp_log(body, **{"k8s.cluster.name": "someone-elses-cluster",
                                                         "deployment.environment": "production"}), headers=_bearer(c["token"]))
    assert r.status_code == 200, r.text
    row = srv.q("SELECT env, res FROM logs WHERE body=?", (body,))[0]
    assert row["env"] == "bound-east"                                  # not "production", not another cluster's slug
    res = json.loads(row["res"])
    assert res["k8s.cluster.name"] == "bound-east"                     # overwritten with the registered name
    lst = client.get("/api/clusters", headers=AUTH).json()["clusters"]
    me = next(x for x in lst if x["slug"] == "bound-east")
    assert me["status"] == "live" and me["logs_1h"] >= 1 and me["last_seen"] > 0


def test_global_token_still_works_and_unknown_cluster_token_is_refused(client):
    assert client.post("/v1/logs", json={"resourceLogs": []}, headers=AUTH).status_code == 200
    assert client.post("/v1/logs", json={"resourceLogs": []}, headers=_bearer("p0c_not-a-real-token")).status_code == 401
    assert client.post("/v1/logs", json={"resourceLogs": []}).status_code == 401


def test_disable_enable_rotate_delete(client):
    c = _add(client, "lifecycle")
    h = _bearer(c["token"])
    assert client.post("/v1/logs", json={"resourceLogs": []}, headers=h).status_code == 200
    assert client.patch("/api/clusters/lifecycle", json={"disabled": True}, headers=AUTH).status_code == 200
    r = client.post("/v1/logs", json={"resourceLogs": []}, headers=h)
    assert r.status_code == 403 and r.json()["error"] == "cluster_disabled"
    assert next(x for x in client.get("/api/clusters", headers=AUTH).json()["clusters"] if x["slug"] == "lifecycle")["status"] == "disabled"
    assert client.patch("/api/clusters/lifecycle", json={"disabled": False}, headers=AUTH).status_code == 200
    assert client.post("/v1/logs", json={"resourceLogs": []}, headers=h).status_code == 200
    new = client.post("/api/clusters/lifecycle/rotate", headers=AUTH).json()
    assert new["token"] != c["token"] and new["token"].startswith("p0c_")
    assert client.post("/v1/logs", json={"resourceLogs": []}, headers=h).status_code == 401          # old token dead at once
    assert client.post("/v1/logs", json={"resourceLogs": []}, headers=_bearer(new["token"])).status_code == 200
    assert client.delete("/api/clusters/lifecycle", headers=AUTH).status_code == 200
    assert client.post("/v1/logs", json={"resourceLogs": []}, headers=_bearer(new["token"])).status_code == 401
    assert client.post("/api/clusters/missing/rotate", headers=AUTH).status_code == 404
    assert client.patch("/api/clusters/missing", json={"note": "x"}, headers=AUTH).status_code == 404


def test_rename_keeps_slug_and_restamps(client, srv):
    c = _add(client, "rename-me")
    assert client.patch("/api/clusters/rename-me", json={"name": "Renamed Cluster"}, headers=AUTH).status_code == 200
    client.post("/v1/logs", json=_otlp_log("rename-marker"), headers=_bearer(c["token"]))
    row = srv.q("SELECT env, res FROM logs WHERE body=?", ("rename-marker",))[0]
    assert row["env"] == "rename-me" and json.loads(row["res"])["k8s.cluster.name"] == "Renamed Cluster"


def test_cluster_appears_as_an_environment(client):
    _add(client, "env-visible")
    envs = client.get("/api/environments?minutes=60", headers=AUTH).json()
    assert any(e["slug"] == "env-visible" and e["name"] == "env-visible" for e in envs)


def test_kubernetes_pages_can_filter_by_cluster(client):
    a, b = _add(client, "k8s-a"), _add(client, "k8s-b")
    client.post("/v1/logs", json=_otlp_log("k8s-a-line", ns="shop", svc="cart"), headers=_bearer(a["token"]))
    client.post("/v1/logs", json=_otlp_log("k8s-b-line", ns="billing", svc="invoice"), headers=_bearer(b["token"]))
    na = {r["namespace"] for r in client.get("/api/kubernetes/namespaces?minutes=60&cluster=k8s-a", headers=AUTH).json()}
    nb = {r["namespace"] for r in client.get("/api/kubernetes/namespaces?minutes=60&cluster=k8s-b", headers=AUTH).json()}
    assert na == {"shop"} and nb == {"billing"}
    assert client.get("/api/kubernetes/overview?minutes=60&cluster=k8s-a", headers=AUTH).json()["clusters"] == ["k8s-a"]
    wl = client.get("/api/kubernetes/workloads?minutes=60&cluster=k8s-b", headers=AUTH).json()
    assert [w["service"] for w in wl] == ["invoice"]


def test_cluster_management_is_admin_only(client, srv):
    assert client.get("/api/clusters").status_code == 401                     # no credentials
    for method, path in (("GET", "/api/clusters"), ("POST", "/api/clusters"), ("DELETE", "/api/clusters/x"),
                         ("POST", "/api/clusters/x/rotate"), ("PATCH", "/api/clusters/x")):
        assert srv._required_role(method, path) == "admin"


def test_tokens_are_stored_hashed(client, srv):
    c = _add(client, "hash-check")
    row = srv.q("SELECT token_hash, token_prefix FROM clusters WHERE slug=?", ("hash-check",))[0]
    assert row["token_hash"] == srv._hash_token(c["token"]) and c["token"] not in json.dumps(dict(row))
