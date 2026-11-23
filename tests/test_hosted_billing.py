"""Hosted-mode (NEBULENS_MODE=hosted) self-serve signup, login and Stripe billing.

Imports server.py fresh under its own env and its own module name, isolated from the main self-hosted
`srv`/`client` session fixtures every other test file shares -- hosted mode is a different deployment mode
entirely (multi-tenant orgs/projects/API keys), not something the self-hosted fixtures can exercise. Env
vars are saved and restored around the whole module so a later test file (e.g. test_licensing.py, which
builds a subprocess env from os.environ) never inherits NEBULENS_MODE=hosted left over from here.
"""
import hashlib
import hmac
import importlib.util
import json
import os
import pathlib
import tempfile
import time

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEBHOOK_SECRET = "whsec_test_123"
PRICE_TIERS = {"price_test_pro": "pro", "price_test_enterprise": "enterprise"}

_ENV_KEYS = ("NEBULENS_MODE", "NEBULENS_ADMIN_TOKEN", "NEBULENS_DB", "NEBULENS_DATA_DIR",
             "NEBULENS_STRIPE_SECRET_KEY", "NEBULENS_STRIPE_WEBHOOK_SECRET", "NEBULENS_STRIPE_PRICE_TIERS",
             "NEBULENS_DATABASE_URL")


@pytest.fixture(scope="module")
def hosted(srv):
    # Depending on the main suite's (session-scoped) `srv` fixture forces the self-hosted server.py module to
    # finish importing, with the real environment still clean, before this fixture mutates NEBULENS_MODE below.
    # Otherwise, if this module's tests happened to run before anything else requested `srv`/`client`, the
    # self-hosted module would get created while NEBULENS_MODE=hosted was (temporarily) set process-wide, and
    # the "self-hosted" fixture wouldn't be self-hosted at all.
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    tmp = tempfile.mkdtemp(prefix="nebulens-hosted-test-")
    os.environ.update({
        "NEBULENS_MODE": "hosted", "NEBULENS_ADMIN_TOKEN": "test-admin-token",
        "NEBULENS_DB": os.path.join(tmp, "nebulens.db"), "NEBULENS_DATA_DIR": os.path.join(tmp, "data"),
        "NEBULENS_STRIPE_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "NEBULENS_STRIPE_PRICE_TIERS": json.dumps(PRICE_TIERS),
    })
    os.environ.pop("NEBULENS_STRIPE_SECRET_KEY", None)   # left unset: exercises the "billing not configured" path
    os.environ.pop("NEBULENS_DATABASE_URL", None)        # SQLite platform db for this test, regardless of the suite's PG run
    try:
        spec = importlib.util.spec_from_file_location("nebulens_server_hosted", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture(scope="module")
def hclient(hosted):
    with TestClient(hosted.app) as c:
        yield c


def _stripe_sig(payload: bytes, secret: str, t: int = None):
    t = t or int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.{payload.decode()}".encode(), hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def _signup(hclient, email="founder@example.com", password="correct horse battery", org="Acme Org"):
    r = hclient.post("/signup", json={"email": email, "password": password, "org_name": org})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- signup / login / session
def test_signup_creates_org_account_and_admin_key(hclient):
    out = _signup(hclient)
    assert out["org_name"] == "Acme Org" and out["email"] == "founder@example.com"
    assert out["admin_key"]["key"].startswith("p0a_")
    assert "nl_org_session" in hclient.cookies


def test_duplicate_email_is_rejected(hclient):
    _signup(hclient, email="dup@example.com", org="Dup Org 1")
    r = hclient.post("/signup", json={"email": "dup@example.com", "password": "whatever123", "org_name": "Dup Org 2"})
    assert r.status_code == 409 and r.json()["error"] == "email_taken"


def test_duplicate_org_name_is_rejected(hclient):
    _signup(hclient, email="a@example.com", org="Shared Name Inc")
    r = hclient.post("/signup", json={"email": "b@example.com", "password": "whatever123", "org_name": "Shared Name Inc"})
    assert r.status_code == 409 and r.json()["error"] == "org_name_taken"


def test_login_wrong_password_rejected_right_password_accepted(hclient):
    _signup(hclient, email="login@example.com", password="the-real-password", org="Login Co")
    hclient.cookies.clear()
    bad = hclient.post("/login", json={"email": "login@example.com", "password": "nope"})
    assert bad.status_code == 401 and bad.json()["error"] == "invalid_credentials"
    assert "nl_org_session" not in hclient.cookies
    good = hclient.post("/login", json={"email": "login@example.com", "password": "the-real-password"})
    assert good.status_code == 200 and good.json()["org_name"] == "Login Co"
    assert "nl_org_session" in hclient.cookies


def test_account_me_requires_session_and_reports_free_tier(hclient):
    hclient.cookies.clear()
    assert hclient.get("/account/me").status_code == 401
    _signup(hclient, email="me@example.com", org="Me Co")
    r = hclient.get("/account/me")
    assert r.status_code == 200
    j = r.json()
    assert j["email"] == "me@example.com" and j["tier"] == "free" and j["billing_active"] is False


def test_logout_clears_the_session(hclient):
    _signup(hclient, email="logout@example.com", org="Logout Co")
    assert hclient.get("/account/me").status_code == 200
    assert hclient.post("/logout").status_code == 200
    assert hclient.get("/account/me").status_code == 401


# ---------------------------------------------------------------- billing: not configured, checkout, portal
def test_checkout_without_a_stripe_key_is_reported_not_configured(hclient):
    _signup(hclient, email="nostripe@example.com", org="No Stripe Co")
    r = hclient.post("/billing/checkout", json={"price_id": "price_test_pro", "success_url": "https://x/ok", "cancel_url": "https://x/no"})
    assert r.status_code == 503 and r.json()["error"] == "billing_not_configured"


def test_checkout_builds_the_right_stripe_request_and_maps_price_to_tier(hclient, hosted, monkeypatch):
    out = _signup(hclient, email="checkout@example.com", org="Checkout Co")
    seen = {}

    def fake_stripe_request(path, form=None, method="POST"):
        seen["path"], seen["form"] = path, form
        return {"id": "cs_test_1", "url": "https://checkout.stripe.com/fake"}

    monkeypatch.setattr(hosted, "_stripe_request", fake_stripe_request)
    r = hclient.post("/billing/checkout", json={"price_id": "price_test_pro", "success_url": "https://x/ok", "cancel_url": "https://x/no"})
    assert r.status_code == 200 and r.json()["url"] == "https://checkout.stripe.com/fake"
    assert seen["path"] == "checkout/sessions"
    assert seen["form"]["client_reference_id"] == out["org_id"]
    assert seen["form"]["metadata[tier]"] == "pro"
    assert seen["form"]["line_items[0][price]"] == "price_test_pro"


def test_portal_requires_an_existing_stripe_customer(hclient, monkeypatch):
    _signup(hclient, email="portal@example.com", org="Portal Co")
    r = hclient.post("/billing/portal")
    assert r.status_code == 400 and r.json()["error"] == "no_subscription"


# ---------------------------------------------------------------- webhook
def test_webhook_bad_signature_is_rejected(hclient):
    body = json.dumps({"type": "checkout.session.completed", "data": {"object": {}}}).encode()
    r = hclient.post("/billing/webhook", content=body, headers={"stripe-signature": "t=1,v1=deadbeef"})
    assert r.status_code == 400 and r.json()["error"] == "bad_signature"


def test_webhook_checkout_completed_upgrades_the_org_to_pro(hclient):
    out = _signup(hclient, email="webhook@example.com", org="Webhook Co")
    event = {"type": "checkout.session.completed", "data": {"object": {
        "client_reference_id": out["org_id"], "customer": "cus_test_1", "subscription": "sub_test_1",
        "metadata": {"org_id": out["org_id"], "tier": "pro"}}}}
    body = json.dumps(event).encode()
    r = hclient.post("/billing/webhook", content=body, headers={"stripe-signature": _stripe_sig(body, WEBHOOK_SECRET)})
    assert r.status_code == 200 and r.json()["received"] is True

    me = hclient.get("/account/me")
    assert me.json()["tier"] == "pro" and me.json()["billing_active"] is True
    assert me.json()["org"]["daily_cap"] == 0                                 # "pro" tier in HOSTED_TIER_QUOTAS is unlimited


def test_webhook_subscription_deleted_reverts_the_org_to_free(hclient):
    out = _signup(hclient, email="revert@example.com", org="Revert Co")
    completed = {"type": "checkout.session.completed", "data": {"object": {
        "client_reference_id": out["org_id"], "customer": "cus_test_2", "subscription": "sub_test_2",
        "metadata": {"org_id": out["org_id"], "tier": "enterprise"}}}}
    b1 = json.dumps(completed).encode()
    hclient.post("/billing/webhook", content=b1, headers={"stripe-signature": _stripe_sig(b1, WEBHOOK_SECRET)})
    before = hclient.get("/account/me").json()
    assert before["tier"] == "enterprise" and before["billing_active"] is True

    deleted = {"type": "customer.subscription.deleted", "data": {"object": {"customer": "cus_test_2"}}}
    b2 = json.dumps(deleted).encode()
    r = hclient.post("/billing/webhook", content=b2, headers={"stripe-signature": _stripe_sig(b2, WEBHOOK_SECRET)})
    assert r.status_code == 200
    after = hclient.get("/account/me").json()
    # Found live (deployed container, real signed webhook, not just this test): billing_active stayed true
    # forever after cancellation, because the handler updated tier but never cleared stripe_subscription_id.
    assert after["tier"] == "free" and after["billing_active"] is False


# ---------------------------------------------------------------- hardening found by the QA/security agent pass
def test_login_pays_the_same_hashing_cost_whether_or_not_the_email_exists(hclient, hosted, monkeypatch):
    """A nonexistent email used to skip _verify_password (and its scrypt call) entirely, answering measurably
    faster than a wrong password for a real account -- a ~30ms timing side-channel, confirmed exploitable for
    email enumeration. Fix: always call _verify_password, against a fixed dummy hash when there's no account.
    This checks the mechanism (the call always happens) rather than timing itself, which would be flaky."""
    calls = []
    real = hosted._verify_password
    monkeypatch.setattr(hosted, "_verify_password", lambda pw, stored: (calls.append(stored), real(pw, stored))[1])

    hclient.post("/login", json={"email": "definitely-not-registered@example.com", "password": "whatever123"})
    assert len(calls) == 1 and calls[0] == hosted._DUMMY_PASSWORD_HASH

    _signup(hclient, email="timing@example.com", password="the-real-password", org="Timing Co")
    calls.clear()
    hclient.post("/login", json={"email": "timing@example.com", "password": "wrong-one"})
    assert len(calls) == 1 and calls[0] != hosted._DUMMY_PASSWORD_HASH


def test_concurrent_signup_with_the_same_email_leaves_no_orphaned_org(hclient):
    """Found live: two requests racing the same email could both pass the pre-check before either committed;
    the loser's account insert then raised an unhandled 500, and -- because the org insert just before it was
    a separate, already-committed statement -- left a permanently unusable, orphaned org name behind. Fixed by
    making both inserts one real transaction, so the loser's org insert rolls back too."""
    import concurrent.futures
    email = "racer@example.com"
    org_names = [f"Race Org {i}" for i in range(8)]

    def attempt(name):
        return hclient.post("/signup", json={"email": email, "password": "whatever12345", "org_name": name})

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = dict(zip(org_names, ex.map(attempt, org_names)))

    statuses = sorted(r.status_code for r in results.values())
    assert 500 not in statuses, "a racing signup crashed instead of cleanly losing the race"
    assert statuses.count(200) == 1
    assert statuses.count(409) == len(org_names) - 1
    losing_names = [name for name, r in results.items() if r.status_code == 409]

    # The one winning org name is legitimately taken (it has a real account attached) and must stay taken;
    # every *losing* attempt's org name must NOT be orphaned -- each must still be free to use.
    for i, name in enumerate(losing_names):
        r = hclient.post("/signup", json={"email": f"racer-followup-{i}@example.com", "password": "whatever12345", "org_name": name})
        assert r.status_code == 200, f"org name '{name}' was left behind orphaned by the race: {r.text}"


def test_webhook_rejects_malformed_or_non_object_json_bodies_cleanly(hclient):
    for body in (b"not even json{{{", b"", b"[1, 2, 3]", b'"just a string"', b"42"):
        r = hclient.post("/billing/webhook", content=body, headers={"stripe-signature": _stripe_sig(body, WEBHOOK_SECRET)})
        assert r.status_code == 400 and r.json()["error"] == "bad_payload", (body, r.status_code, r.text)


def test_checkout_rejects_an_unmapped_price_id_instead_of_defaulting_to_pro(hclient):
    """price_id -> tier used to default to "pro" for anything not in NEBULENS_STRIPE_PRICE_TIERS -- silently
    wrong if an operator adds a new price in Stripe and forgets to map it here. Must fail closed instead."""
    _signup(hclient, email="unmapped@example.com", org="Unmapped Co")
    r = hclient.post("/billing/checkout", json={"price_id": "price_never_configured",
                                                "success_url": "https://x/ok", "cancel_url": "https://x/no"})
    assert r.status_code == 400 and r.json()["error"] == "unknown_price"


def test_signup_with_a_non_json_content_type_fails_cleanly_not_with_a_500(hclient):
    """The shared validation-error handler crashed trying to JSON-serialize the raw bytes body FastAPI
    attaches to a validation error when the Content-Type isn't JSON -- reachable by anyone, since /signup and
    /login are unauthenticated and public, unlike most of this handler's other (API-key-gated) callers."""
    hclient.cookies.clear()
    r = hclient.post("/signup", content=b'{"email":"x@example.com","password":"whatever1","org_name":"X Org"}',
                     headers={"Content-Type": "text/plain"})
    assert r.status_code < 500, r.text


# ---------------------------------------------------------------- self-hosted mode must not expose any of this
def test_self_hosted_server_does_not_expose_signup_or_billing(client):
    """`client` here is the main suite's self-hosted TestClient (conftest.py), not the hosted one above --
    confirms none of this new surface leaks into a plain self-hosted install. Bodies must pass SignupIn's own
    field validation (e.g. org_name needs >=2 chars) to reach the handler's _hosted_only() 404 at all --
    otherwise FastAPI's validation-error handler (400, not 404) answers first."""
    assert client.post("/signup", json={"email": "x@example.com", "password": "whatever1", "org_name": "Some Org"}).status_code == 404
    assert client.post("/login", json={"email": "x@example.com", "password": "whatever1"}).status_code == 404
    assert client.get("/account/me").status_code == 404
    assert client.post("/billing/checkout", json={"price_id": "p", "success_url": "https://x", "cancel_url": "https://x"}).status_code == 404
    assert client.post("/billing/webhook", content=b"{}").status_code == 404
