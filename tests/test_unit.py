"""Pure logic: severity inference, PII redaction, environment resolution, batched pruning."""
import time
import pytest

SEVERITY = [
    # explicit level in the text
    ("2026-11-09 15:15:48.123 [http-nio-8080-exec-10] ERROR c.c2m.FeedExport - Connection refused", "ERROR"),
    ("2026-11-09 15:15:48.123 [main] INFO  c.c2m.App - Started in 4.2 seconds", "INFO"),
    ("2026-11-09 15:15:48.123 [main] INFO  c.c2m.App - retry after ERROR rate dropped", "INFO"),   # earliest word wins
    ("2026-11-09 15:15:48 WARN  Slow query took 3200ms", "WARN"),
    ("2026-11-09 15:15:48 [pool-1] SEVERE  Out of memory", "ERROR"),
    ("DEBUG 2026-11-09 verbose detail", "DEBUG"),
    ("ERROR:root:Traceback follows", "ERROR"),
    # .NET console, JSON, logfmt, klog, kafka
    ("fail: Microsoft.AspNetCore.Server.Kestrel[13]", "ERROR"),
    ("info: Microsoft.Hosting.Lifetime[14]", "INFO"),
    ("crit: Program[0] cannot start", "FATAL"),
    ('{"level":"error","msg":"db down","trace":"INFO"}', "ERROR"),
    ('ts=2026-11-09T15:15:48Z level=warn msg="disk 91%"', "WARN"),
    ("E1110 09:02:43.388887 1 memcache.go:265] boom", "ERROR"),
    ("%4|1794349053.533|REQTMOUT|rdkafka#consumer-2| [thrd:10.0.2.117:9092/0]: timed out", "WARN"),
    ("%3|1794349053.533|FAIL|rdkafka#producer-1| broker down", "ERROR"),
    # access logs by HTTP status
    ('10.0.0.1 - - "GET /api/orders HTTP/1.1" 200 1534', "INFO"),
    ('10.0.0.1 - - "GET /missing HTTP/1.1" 404 120', "WARN"),
    ('10.0.0.1 - - "POST /pay HTTP/1.1" 500 88', "ERROR"),
    ("request completed status=503 in 12ms", "ERROR"),
    # stack traces, framework noise, bracketed mixed-case levels
    ('Exception in thread "main" java.lang.reflect.InvocationTargetException', "ERROR"),
    ("Caused by: java.net.ConnectException: Connection refused", "ERROR"),
    ("      at org.eclipse.angus.mail.smtp.SMTPTransport$Authenticator.authenticate(SMTPTransport.java:954)", "ERROR"),
    ("OpenJDK 64-Bit Server VM warning: Sharing is only supported for boot loader classes", "WARN"),
    ("Error: Missing required environment variable: MICROAI_AM_ENVIRONMENT", "ERROR"),
    (":: Spring Boot ::               (v3.5.16)", "INFO"),
    ("  .   ____          _            __ _ _", "INFO"),
    ("( ( )\\___ | '_ | '_| | '_ \\/ _` | \\ \\ \\ \\", "INFO"),
    ("Picked up JAVA_TOOL_OPTIONS: -javaagent:/x", "INFO"),
    ("Starting server on port 8080", "INFO"),                                     # lifecycle chatter
    ("failed to get info from upstream (ERR_CONNECTION_RESET)", "ERROR"),        # "failed to ..." announces a failure
    ("+-> Response:", "ERROR"),
    ("[2026-11-10 22:19:50.843 +00:00][5][Error] - {CentralConfigurationFetcher} Exception", "ERROR"),
    ("This generated password is for development use only.", "WARN"),
    # an explicit level must beat every guess
    ("2026-11-09 WARN upstream returned 500 retrying", "WARN"),
    ("2026-11-09 15:15:48 INFO [Error] handled gracefully", "INFO"),
    ("2026-11-09 INFO could not find optional cache", "INFO"),
    # genuinely no level: must stay unset rather than guess
    ("GET /health 200 in 3ms", ""),
    ("plain message with no level", ""),
    ("order 500 items processed", ""),
    ("e58ce263d42e61050e51551941a86f430453bdf9 10.0.2.93:6380@16380 slave 5fd2ce1b1f068", ""),
    ("}", ""),
]


@pytest.mark.parametrize("body,want", SEVERITY)
def test_severity_inference(srv, body, want):
    assert srv._infer_severity(body)[1] == want


def luhn_number(prefix, total, seed=7):
    """A Luhn-valid digit string that starts with prefix (i.e. one that Luhn alone would wrongly call a card)."""
    import random
    rnd = random.Random(seed)
    body = prefix + "".join(str(rnd.randint(0, 9)) for _ in range(total - len(prefix) - 1))
    return next(body + c for c in "0123456789" if _luhn(body + c))


def _luhn(num):
    tot, alt = 0, False
    for ch in reversed(num):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        tot += d; alt = not alt
    return tot % 10 == 0


@pytest.mark.parametrize("card", ["4111111111111111", "5555555555554444", "378282246310005", "6011111111111117",
                                  "3530111333300000", "30569309025904", "4111 1111 1111 1111", "4111-1111-1111-1111"])
def test_real_cards_are_redacted(srv, card):
    assert "redacted" in srv.redact(f"paid with {card} ok")


def test_timestamps_and_ids_are_not_cards(srv):
    # Luhn alone passes 1 in 10 random digit strings; epoch-nanosecond timestamps start with 1 and must survive
    for n in [luhn_number("1794", 19, s) for s in range(5)] + [luhn_number("9", 16, s) for s in range(3)]:
        assert "redacted" not in srv.redact(f"ts={n} done"), n


@pytest.mark.parametrize("text,kind", [("ssn=123-45-6789 on file", "ssn"), ("to GB82WEST12345698765432 ok", "iban"),
                                       ('api_key="sk-live-9f8e7d6c5b4a"', "apikey"),
                                       ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijk", "jwt")])
def test_other_secrets_redacted(srv, text, kind):
    assert f"{kind}:redacted" in srv.redact(text)


def test_ordinary_text_untouched(srv):
    for t in ("GET /orders/8832 200 in 14ms", "2026-11-09 15:15:48.123 ERROR pool exhausted", "latency 1234567890123 ns"):
        assert srv.redact(t) == t


def test_environment_resolution(srv):
    slug, meta = srv._env_of({"k8s.namespace.name": "application"})
    assert slug == "test-cluster" and meta["namespace"] == "application"          # namespace is NOT the environment
    assert srv._env_of({"deployment.environment": "production", "k8s.namespace.name": "x"})[0] == "production"
    assert srv._env_of({"k8s.cluster.name": "cluster-b"})[0] == "cluster-b"


@pytest.mark.sqlite_only
def test_prune_deletes_exactly_the_stale_rows(srv):
    now, H = time.time_ns(), 3600e9
    with srv.db() as c:
        c.executemany("INSERT INTO metrics(ts_ns,service,name,unit,mtype,value,attrs,res,env) VALUES(?,?,?,?,?,?,?,?,?)",
                      [(int(now - h * H), "prune-svc", "n", "1", "g", 1.0, "{}", "{}", "e") for h in range(1, 101) for _ in range(50)])
    cut = now - int(24 * H)
    old = srv.q("SELECT COUNT(*) n FROM metrics WHERE service='prune-svc' AND ts_ns<?", (cut,))[0]["n"]
    srv._prune_table("metrics", "ts_ns", cut, batch=1000)
    assert srv.q("SELECT COUNT(*) n FROM metrics WHERE service='prune-svc' AND ts_ns<?", (cut,))[0]["n"] == 0
    assert srv.q("SELECT COUNT(*) n FROM metrics WHERE service='prune-svc'")[0]["n"] == 5000 - old


def test_dockerfile_copies_every_top_level_module():
    """Regression: v9/v10 shipped an image missing integrations.py (ModuleNotFoundError at startup).
    Tests run from the source tree so they can't see this; check the Dockerfile explicitly."""
    import glob, os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    df = open(os.path.join(root, "Dockerfile")).read()
    for f in glob.glob(os.path.join(root, "*.py")):
        assert os.path.basename(f) in df, f"{os.path.basename(f)} is not COPY'd in the Dockerfile"


def test_integration_pages_are_scrollable():
    """Regression: .page is height:100% unless the route sets scroll:true, which clipped the long
    Kubernetes Setup tab with no way to scroll. Both integration routes must opt in."""
    import os, re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = open(os.path.join(root, "static", "views2.js"), encoding="utf-8").read()
    for route in ('"/integrations"', '"/integrations/:iid"'):
        m = re.search(r"P\.route\(" + re.escape(route) + r", \{(.*?)async render", js, re.S)
        assert m, f"route {route} not found"
        assert "scroll: true" in m.group(1), f"{route} must set scroll: true"


def _otlp_logs(*bodies_and_sev):
    recs = [{"timeUnixNano": "1700000000000000000", "severityNumber": sev, "body": {"stringValue": body}}
            for body, sev in bodies_and_sev]
    return {"resourceLogs": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "min-sev-svc"}}]},
                              "scopeLogs": [{"logRecords": recs}]}]}


def _stored(srv, svc="min-sev-svc"):
    with srv.db() as c:
        return sorted(r[0] for r in c.execute("SELECT severity FROM logs WHERE service=?", (svc,)).fetchall())


def test_min_log_severity_guard(srv, monkeypatch):
    monkeypatch.setattr(srv, "MIN_LOG_SEVERITY", 17)
    monkeypatch.setattr(srv, "LOG_UNKNOWN_SEVERITY", "drop")
    before = srv._below_min_dropped[0]
    n = srv.ingest_logs(_otlp_logs(
        ("DEBUG heartbeat", 5),                       # explicit level below the minimum -> dropped
        ("plain text with no level at all", 0),       # unknown level, "drop" -> dropped
        ("2026-11-17 INFO Started in 3s", 0),         # unlabeled, level read from the text -> INFO -> dropped
        ("2026-11-17 ERROR boom", 0),                 # unlabeled, text says ERROR -> kept
        ("fatal thing", 21),                          # explicit FATAL -> kept
    ))
    assert n == 2
    assert _stored(srv) == [17, 21]
    assert srv._below_min_dropped[0] - before == 3


def test_min_log_severity_keep_unknown(srv, monkeypatch):
    monkeypatch.setattr(srv, "MIN_LOG_SEVERITY", 17)
    monkeypatch.setattr(srv, "LOG_UNKNOWN_SEVERITY", "keep")
    n = srv.ingest_logs(_otlp_logs(("no level here either", 0), ("INFO x", 9)))
    assert n == 1


def test_min_log_severity_off_by_default(srv, monkeypatch):
    monkeypatch.setattr(srv, "MIN_LOG_SEVERITY", 0)
    assert srv.ingest_logs(_otlp_logs(("DEBUG a", 5), ("no level", 0))) == 2
