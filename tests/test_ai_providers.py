"""The assistant works with any LLM: Claude through the Anthropic SDK, or any OpenAI-compatible chat-completions endpoint."""
import json, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytest
from conftest import AUTH

AI_VARS = ("NEBULENS_AI_PROVIDER", "NEBULENS_AI_API_KEY", "NEBULENS_AI_BASE_URL", "NEBULENS_AI_MODEL", "NEBULENS_AI_NAME",
           "NEBULENS_AI_KEY_HEADER", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")


@pytest.fixture
def no_ai(monkeypatch):
    for v in AI_VARS:
        monkeypatch.delenv(v, raising=False)
    return monkeypatch


def test_provider_selection(srv, no_ai):
    assert srv._ai_provider() == "" and srv._ai_ready() == (False, "no AI provider configured")
    no_ai.setenv("ANTHROPIC_API_KEY", "sk-test")                      # unchanged behaviour for existing installs
    assert srv._ai_provider() == "anthropic" and srv._ai_ready()[0] and srv._ai_name() == "Claude"
    assert srv._ai_model() == "claude-sonnet-5-5"
    no_ai.delenv("ANTHROPIC_API_KEY")
    no_ai.setenv("NEBULENS_AI_BASE_URL", "http://ollama.local:11434/v1")  # local server: no key needed, but a model is
    assert srv._ai_provider() == "openai" and srv._ai_ready() == (False, "set NEBULENS_AI_MODEL to the model name your endpoint serves")
    no_ai.setenv("NEBULENS_AI_MODEL", "llama3.1")
    assert srv._ai_ready() == (True, "") and srv._ai_name() == "AI model"
    no_ai.setenv("NEBULENS_AI_NAME", "Llama 3.1 on Ollama")
    assert srv._ai_name() == "Llama 3.1 on Ollama"
    no_ai.setenv("NEBULENS_AI_PROVIDER", "off")
    assert srv._ai_provider() == ""


def test_openai_cloud_needs_a_key(srv, no_ai):
    no_ai.setenv("NEBULENS_AI_PROVIDER", "openai")
    no_ai.setenv("NEBULENS_AI_MODEL", "some-model")
    assert srv._ai_ready()[0] is False                               # api.openai.com without a key
    no_ai.setenv("OPENAI_API_KEY", "sk-x")
    assert srv._ai_ready() == (True, "") and srv._ai_name() == "OpenAI"


def test_status_and_disabled_endpoint_are_provider_neutral(client, no_ai):
    st = client.get("/api/assistant/status", headers=AUTH).json()
    assert st["enabled"] is False and st["mode"] == "rules" and st["reason"] == "no AI provider configured"
    r = client.post("/api/assistant", json={"messages": [{"role": "user", "content": "hi"}]}, headers=AUTH)
    assert r.status_code == 503 and "ANTHROPIC" not in r.json()["message"] and r.json()["fallback"] == "/api/investigate"


class _FakeOpenAI(BaseHTTPRequestHandler):
    """Answers like an OpenAI-compatible server: first a tool call, then a final answer that uses the tool result."""
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeOpenAI.seen.append((self.path, dict(self.headers), body))
        tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
        if not tool_msgs:
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "list_services", "arguments": json.dumps({"minutes": 60})}}]}
        else:
            msg = {"role": "assistant", "content": "Services checked: " + str(len(json.loads(tool_msgs[-1]["content"]))) + " with traffic."}
        out = json.dumps({"choices": [{"message": msg, "finish_reason": "tool_calls" if "tool_calls" in msg else "stop"}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(out)))
        self.end_headers(); self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_openai_compatible_tool_loop_end_to_end(client, ingest, no_ai):
    ingest.span("ai-svc", "app")
    httpd = HTTPServer(("127.0.0.1", 0), _FakeOpenAI)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        _FakeOpenAI.seen.clear()
        no_ai.setenv("NEBULENS_AI_PROVIDER", "openai")
        no_ai.setenv("NEBULENS_AI_BASE_URL", f"http://127.0.0.1:{httpd.server_port}/v1")
        no_ai.setenv("NEBULENS_AI_MODEL", "local-model")
        no_ai.setenv("NEBULENS_AI_API_KEY", "secret-key-123")
        st = client.get("/api/assistant/status", headers=AUTH).json()
        assert st["enabled"] and st["mode"] == "llm" and st["provider"] == "openai" and st["model"] == "local-model"
        r = client.post("/api/assistant", json={"messages": [{"role": "user", "content": "which services are up?"}]}, headers=AUTH)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["mode"] == "llm" and j["tools"] == ["list_services"] and j["answer"].startswith("Services checked:")
        path, headers, first = _FakeOpenAI.seen[0]
        assert path == "/v1/chat/completions" and headers["Authorization"] == "Bearer secret-key-123"
        assert first["model"] == "local-model" and first["messages"][0]["role"] == "system"
        assert {t["function"]["name"] for t in first["tools"]} >= {"list_services", "search_logs", "get_trace"}
        second = _FakeOpenAI.seen[1][2]["messages"]
        assert second[-2]["tool_calls"][0]["id"] == "call_1" and second[-1] == {**second[-1], "role": "tool", "tool_call_id": "call_1"}
    finally:
        httpd.shutdown()


def test_azure_style_key_header_and_errors_hide_the_key(srv, no_ai, monkeypatch):
    seen = {}

    class Boom(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["h"] = dict(self.headers)
            self.rfile.read(int(self.headers["Content-Length"]))
            out = b'{"error": "bad key secret-key-xyz"}'
            self.send_response(401); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

        def log_message(self, *a):
            pass
    httpd = HTTPServer(("127.0.0.1", 0), Boom)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        no_ai.setenv("NEBULENS_AI_PROVIDER", "azure")
        no_ai.setenv("NEBULENS_AI_BASE_URL", f"http://127.0.0.1:{httpd.server_port}/openai/v1")
        no_ai.setenv("NEBULENS_AI_MODEL", "my-deployment")
        no_ai.setenv("NEBULENS_AI_API_KEY", "secret-key-xyz")
        no_ai.setenv("NEBULENS_AI_KEY_HEADER", "api-key")
        evs = list(srv._assistant_events([{"role": "user", "content": "hi"}]))
        h = {k.lower(): v for k, v in seen["h"].items()}             # HTTP header names are case-insensitive
        assert h.get("api-key") == "secret-key-xyz" and "authorization" not in h
        err = [e for e in evs if e["type"] == "error"][0]["message"]
        assert "HTTP 401" in err and "secret-key-xyz" not in err     # the key never reaches the UI or the logs
    finally:
        httpd.shutdown()


def _local_model(no_ai, port):
    no_ai.setenv("NEBULENS_AI_PROVIDER", "openai")
    no_ai.setenv("NEBULENS_AI_BASE_URL", f"http://127.0.0.1:{port}/v1")
    no_ai.setenv("NEBULENS_AI_MODEL", "local-model")


def test_community_gets_a_daily_allowance_then_an_upgrade_prompt(srv, client, no_ai):
    httpd = HTTPServer(("127.0.0.1", 0), _FakeOpenAI)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        _local_model(no_ai, httpd.server_port)
        no_ai.setattr(srv, "IS_PRO", False)
        no_ai.setattr(srv, "COMMUNITY_AI_DAILY", 2)
        with srv.db() as c:
            c.execute("DELETE FROM ai_usage")
        st = client.get("/api/assistant/status", headers=AUTH).json()
        assert st["enabled"] and st["pro"] is False and st["quota"] == {"limit": 2, "used": 0, "remaining": 2, "resets": "00:00 UTC"}
        ask = lambda: client.post("/api/assistant", json={"messages": [{"role": "user", "content": "hi"}]}, headers=AUTH)
        assert ask().status_code == 200 and ask().status_code == 200
        r = ask()
        assert r.status_code == 403 and r.json()["error"] == "tier_limit" and r.json()["limit"] == 2   # the UI shows the upgrade dialog
        st = client.get("/api/assistant/status", headers=AUTH).json()
        assert st["enabled"] is False and st["locked"] is True and st["quota"]["remaining"] == 0 and "used up" in st["reason"]
        assert client.get("/api/investigate?q_=errors", headers=AUTH).status_code == 200            # rule-based stays free
    finally:
        httpd.shutdown()


def test_pro_is_unlimited_and_zero_means_pro_only(srv, client, no_ai):
    httpd = HTTPServer(("127.0.0.1", 0), _FakeOpenAI)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        _local_model(no_ai, httpd.server_port)
        st = client.get("/api/assistant/status", headers=AUTH).json()          # tests run as Pro
        assert st["enabled"] and st["pro"] is True and st["quota"] is None
        no_ai.setattr(srv, "IS_PRO", False)
        no_ai.setattr(srv, "COMMUNITY_AI_DAILY", 0)
        st = client.get("/api/assistant/status", headers=AUTH).json()
        assert st["enabled"] is False and st["locked"] and st["reason"] == "AI answers are a Pro feature"
        r = client.post("/api/assistant", json={"messages": [{"role": "user", "content": "hi"}]}, headers=AUTH)
        assert r.status_code == 403 and r.json()["error"] == "pro_required"
    finally:
        httpd.shutdown()
