"""Security regressions: outbound calls never reach the cloud metadata service, the AI endpoint must be http(s), and the
Helm chart refuses the placeholder tokens that ship in its values files."""
import shutil, subprocess, urllib.request
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data/", "http://[fe80::1]/", "http://0.0.0.0:8080/",
                                 "file:///etc/passwd", "ftp://example.com/x", "gopher://127.0.0.1:6379/"])
def test_metadata_link_local_and_odd_schemes_are_always_blocked(srv, url):
    assert srv._blocked_dest(url)


@pytest.mark.parametrize("url", ["http://10.0.0.5:8080/health", "http://127.0.0.1:4318/healthz", "https://example.com/"])
def test_self_hosted_still_checks_the_private_network(srv, url):
    assert srv._blocked_dest(url) is None          # checking internal services is what synthetic checks are for


def test_redirects_into_the_metadata_service_are_not_followed(srv):
    h = srv._CheckedRedirect()
    req = urllib.request.Request("http://example.com/")
    assert h.redirect_request(req, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data/") is None
    assert h.redirect_request(req, None, 302, "Found", {}, "https://example.com/next") is not None


def test_self_hosted_auth_failures_are_rate_limited(client, srv):
    """FAIL_LIMIT/_auth_fail already existed for hosted mode's /org/* key checks but, found in a security
    review, were never wired into the self-hosted /v1 and /api token/session check -- the path almost every
    real install actually uses -- so wrong-token attempts never got throttled or even counted. A bad guess just
    silently 401'd at full network speed with no operator-visible signal."""
    try:
        seen_429 = False
        for i in range(srv.FAIL_LIMIT + 5):
            r = client.get("/api/services", headers={"Authorization": f"Bearer wrong-token-{i}"})
            if r.status_code == 429:
                seen_429 = True
                assert r.json()["error"] == "too_many_attempts" and "Retry-After" in r.headers
                break
            assert r.status_code == 401
        assert seen_429, "wrong tokens were never throttled after FAIL_LIMIT attempts"
    finally:
        srv._FAILS.clear()          # don't leak this test's rate-limit state into whatever test runs next


def test_synthetic_check_to_metadata_records_a_block_not_a_request(srv, monkeypatch):
    called = []
    monkeypatch.setattr(srv, "_urlopen", lambda *a, **k: called.append(1))
    with srv.db() as c:
        c.execute("INSERT INTO synthetics(name,url,interval_s,expect_status,max_ms,enabled) VALUES(?,?,?,?,?,1)",
                  ("meta", "http://169.254.169.254/latest/meta-data/", 60, 200, 2000))
        cid = c.execute("SELECT MAX(id) FROM synthetics").fetchone()[0]
    srv.run_check(srv.q("SELECT * FROM synthetics WHERE id=?", (cid,))[0])
    run = srv.q("SELECT ok, err FROM synthetic_runs WHERE check_id=? ORDER BY ts DESC LIMIT 1", (cid,))[0]
    with srv.db() as c:          # a failed check lowers every service's health score: don't leave it for other tests
        c.execute("DELETE FROM synthetic_runs WHERE check_id=?", (cid,))
        c.execute("DELETE FROM synthetics WHERE id=?", (cid,))
    assert not called and run["ok"] == 0 and "metadata" in run["err"]


def test_ai_endpoint_must_be_http(srv, monkeypatch):
    for v in ("NEBULENS_AI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("NEBULENS_AI_PROVIDER", "openai")
    monkeypatch.setenv("NEBULENS_AI_MODEL", "m")
    monkeypatch.setenv("NEBULENS_AI_BASE_URL", "file:///etc")
    assert srv._ai_ready() == (False, "NEBULENS_AI_BASE_URL must be an http(s) URL")


@pytest.mark.skipif(not shutil.which("helm"), reason="helm not installed")
@pytest.mark.parametrize("overlay", ["values-baremetal.yaml", "values-eks.yaml", "values-openshift.yaml"])
def test_chart_refuses_placeholder_tokens(overlay):
    chart = ROOT / "helm" / "nebulens"
    bad = subprocess.run(["helm", "template", "p", str(chart), "-f", str(chart / overlay)], capture_output=True, text=True)
    assert bad.returncode != 0 and "placeholder" in bad.stderr
    ok = subprocess.run(["helm", "template", "p", str(chart), "-f", str(chart / overlay), "--set", "auth.token=" + "a" * 48],
                        capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
