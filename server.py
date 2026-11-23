"""NebuLens - a personal, OpenTelemetry-native observability tool.

Ingests OTLP (HTTP, JSON or protobuf) for traces, logs and metrics, stores them in
SQLite, and serves a resource-centric UI: services (RED metrics), service map,
trace waterfall, log search, metric explorer, alert rules and synthetic checks.
"""
import asyncio, random, json, math, os, sqlite3, sys, threading, time, ssl, socket, urllib.request, urllib.error, urllib.parse, base64
import collections, contextvars, hashlib, hmac, ipaddress, re, secrets
from contextlib import contextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request, Response, HTTPException, Depends, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator, ConfigDict
from typing import Optional, Literal
import uuid

# NebuLens was called Pulse0 (and, briefly in v11.0, Oscilyx). NEBULENS_* settings are the ones to use; the old PULSE0_* and
# OSCILYX_* names keep working, in both directions so modules that still read an old name see a value set under the new
# one. When several are set, NEBULENS_* wins, then OSCILYX_*, then PULSE0_*.
_OLD_PREFIXES = ("OSCILYX_", "PULSE0_")
for _p in _OLD_PREFIXES:
    for _k in list(os.environ):
        if _k.startswith(_p):
            os.environ.setdefault("NEBULENS_" + _k[len(_p):], os.environ[_k])
for _k in list(os.environ):
    if _k.startswith("NEBULENS_"):
        for _p in _OLD_PREFIXES:
            os.environ[_p + _k[len("NEBULENS_"):]] = os.environ[_k]


def _legacy_db(path):
    """An upgraded install keeps its data in oscilyx.db or pulse0.db: use that file while no nebulens.db exists beside it."""
    if os.path.basename(path) == "nebulens.db" and not os.path.exists(path):
        for old_name in ("oscilyx.db", "pulse0.db"):
            old = os.path.join(os.path.dirname(path), old_name)
            if os.path.exists(old):
                return old
    return path


DB_PATH = _legacy_db(os.environ.get("NEBULENS_DB", os.path.join(os.path.dirname(__file__), "nebulens.db")))
DATABASE_URL = os.environ.get("NEBULENS_DATABASE_URL", "")
USE_PG = bool(DATABASE_URL)
PG_SCHEMA = os.environ.get("NEBULENS_PG_SCHEMA", "")

if USE_PG:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.numeric import FloatLoader
    from psycopg_pool import ConnectionPool
    _IntegrityError = (sqlite3.IntegrityError, psycopg.errors.UniqueViolation)
    # SQLite's SUM()/AVG() return int/float; Postgres returns Decimal for NUMERIC. Code throughout this file
    # mixes those results into plain float arithmetic, so decode NUMERIC as float everywhere to match SQLite.
    psycopg.adapters.register_loader("numeric", FloatLoader)
else:
    _IntegrityError = sqlite3.IntegrityError
STATIC = os.path.join(os.path.dirname(__file__), "static")
RETENTION_HOURS = float(os.environ.get("NEBULENS_RETENTION_HOURS", "72"))
# Metrics are by far the largest table (every scrape of every node writes hundreds of rows), so they can be
# kept for less time than logs and traces. 0 = same as NEBULENS_RETENTION_HOURS.
METRICS_RETENTION_HOURS = float(os.environ.get("NEBULENS_METRICS_RETENTION_HOURS", "0") or 0)
# Lowest log severity that is stored (OTel severity_number: 5 DEBUG, 9 INFO, 13 WARN, 17 ERROR, 21 FATAL).
# 0 = store everything. 17 = errors only. Applied at ingest AFTER the level has been read from the log text, so
# it also covers container logs that arrive with no severity at all (the collector cannot filter those).
MIN_LOG_SEVERITY = int(os.environ.get("NEBULENS_MIN_LOG_SEVERITY", "0") or 0)
# Only used when MIN_LOG_SEVERITY > 0: what to do with a record whose level could not be determined even from
# its text. "drop" (default) keeps the store errors-only; "keep" stores them and relies on the text parser.
LOG_UNKNOWN_SEVERITY = (os.environ.get("NEBULENS_LOG_UNKNOWN_SEVERITY", "drop") or "drop").strip().lower()
AUTH_TOKEN = os.environ.get("NEBULENS_TOKEN", "")  # optional bearer token for ingest + API

# ---------------------------------------------------------------- Tier / licensing
# NEBULENS_TIER: "community" (free, limited) or "pro" (paid, unlimited).
# A valid NEBULENS_LICENSE_KEY (validated by the enterprise module) activates Pro.
# On first install, a 14-day Pro trial is activated automatically.
#
# Licence keys (NBL1.<payload>.<signature>) are signed by NebuLens with an Ed25519 private key that never ships. Only the
# public key is here, so a key cannot be created or altered even with this source code. The payload carries the tier
# ("pro" or "enterprise"), the licensed node count (0 = unlimited) and the expiry. Checked offline: no phone-home.
# There is no setting that unlocks a tier without a key (the old NEBULENS_TIER=pro switch did).
_LICENSE_PUBKEYS = ("VDMn9Yv+M/GgwrkXCy3stTCEKY9AdYw7lWUrI2S2+v4=",)
_LICENSE_KEY = (os.environ.get("NEBULENS_LICENSE_KEY", "") or "").strip()
COMMUNITY_NODES = 5        # the free tier: one cluster or VM fleet of up to 5 nodes
PRICING_URL = os.environ.get("NEBULENS_PRICING_URL", "https://nebulens.dev/pricing.html")
_LIMIT_NAMES = {"dashboards": "dashboards", "alerts": "alert rules", "slos": "SLOs", "synthetics": "synthetic checks",
                 "clusters": "registered cluster"}


def _license_pubkeys():
    keys = list(_LICENSE_PUBKEYS)
    if "pytest" in sys.modules and os.environ.get("NEBULENS_TEST_LICENSE_PUBKEY"):   # the test suite signs its own keys
        keys.append(os.environ["NEBULENS_TEST_LICENSE_PUBKEY"])
    return keys


def _parse_license(key):
    """{"tier", "nodes", "exp", "customer"} for a correctly signed, unexpired key; None otherwise."""
    key = (key or "").strip()
    if not key.startswith("NBL1."):
        return None
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        _, body, sig = key.split(".")
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        signature = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        for pk in _license_pubkeys():
            try:
                Ed25519PublicKey.from_public_bytes(base64.b64decode(pk)).verify(signature, payload)
                break
            except Exception:
                continue
        else:
            return None
        d = json.loads(payload)
        if d.get("t") not in ("pro", "enterprise") or int(d.get("exp", 0)) < time.time():
            return None
        return {"tier": d["t"], "nodes": max(0, int(d.get("n", 0))), "exp": int(d["exp"]), "customer": str(d.get("c", ""))[:120]}
    except Exception:
        return None


_LICENSE = _parse_license(_LICENSE_KEY)
_ENTERPRISE = False
if not _LICENSE and _LICENSE_KEY:
    try:   # keys issued before signed licences (NEBULENS-XXXX-...): Pro, unlimited nodes, only with the paid module installed
        from enterprise.license import validate_license
        _ENTERPRISE = True
        if validate_license(_LICENSE_KEY):
            _LICENSE = {"tier": "pro", "nodes": 0, "exp": 0, "customer": ""}
    except ImportError:
        pass
if _LICENSE_KEY and not _LICENSE:
    print("WARNING: NEBULENS_LICENSE_KEY is not a valid NebuLens licence (wrong, altered or expired); running as Community.",
          flush=True)
_LICENSE_VALID = bool(_LICENSE)

TRIAL_DAYS = 14
_TRIAL_START = 0
_TRIAL_ACTIVE = False
_TRIAL_DAYS_LEFT = 0

def _init_trial(db_path):
    """Check/initialize trial state from the meta table. Called after DB init."""
    global _TRIAL_START, _TRIAL_ACTIVE, _TRIAL_DAYS_LEFT, TIER, IS_PRO, RETENTION_HOURS, LICENSED_NODES
    if _LICENSE_VALID:
        return
    try:
        now = int(time.time())
        if USE_PG:
            with _pg_pool.connection() as pc:
                pc.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
                pc.execute("INSERT INTO meta(key,value) VALUES('trial_start',%s) ON CONFLICT (key) DO NOTHING", (str(now),))
                _TRIAL_START = int(pc.execute("SELECT value FROM meta WHERE key='trial_start'").fetchone()[0])
        else:
            c = sqlite3.connect(db_path, check_same_thread=False)
            c.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
            row = c.execute("SELECT value FROM meta WHERE key='trial_start'").fetchone()
            if row:
                _TRIAL_START = int(row[0])
            else:
                _TRIAL_START = now
                c.execute("INSERT INTO meta(key,value) VALUES('trial_start',?)", (str(now),))
                c.commit()
            c.close()
        elapsed = now - _TRIAL_START
        _TRIAL_DAYS_LEFT = max(0, TRIAL_DAYS - int(elapsed / 86400))
        _TRIAL_ACTIVE = _TRIAL_DAYS_LEFT > 0
        if _TRIAL_ACTIVE:
            TIER = "pro"
            IS_PRO = True
            LICENSED_NODES = 0          # the trial is not node-limited
            RETENTION_HOURS = float(os.environ.get("NEBULENS_RETENTION_HOURS", "72"))
    except Exception:
        pass

TIER = _LICENSE["tier"] if _LICENSE else "community"
IS_PRO = TIER in ("pro", "enterprise")          # Pro features: Pro and Enterprise
IS_ENTERPRISE = TIER == "enterprise"
LICENSED_NODES = _LICENSE["nodes"] if _LICENSE else COMMUNITY_NODES     # 0 = unlimited
def _print_tier():
    if _LICENSE_VALID:
        nodes = f"{LICENSED_NODES} nodes" if LICENSED_NODES else "unlimited nodes"
        until = time.strftime("%Y-%m-%d", time.gmtime(_LICENSE["exp"])) if _LICENSE.get("exp") else "no expiry"
        print(f"NebuLens {TIER.title()} licence: {nodes}, valid until {until}"
              + (f" ({_LICENSE['customer']})" if _LICENSE.get("customer") else ""))
    elif IS_PRO and _TRIAL_ACTIVE:
        print(f"NebuLens Pro trial ({_TRIAL_DAYS_LEFT} days remaining)")
    else:
        if _TRIAL_START and not _TRIAL_ACTIVE:
            print("NebuLens Community tier (Pro trial expired, set NEBULENS_LICENSE_KEY to upgrade)")
        else:
            print("NebuLens Community tier (set NEBULENS_LICENSE_KEY to activate Pro)")

# Community-tier limits (Pro and Enterprise are unlimited). The footprint limit is in nodes, the same unit Pro is priced in;
# there is no service cap: eBPF finds every workload on day one, and a service cap made the free tier look broken.
# Clusters are capped separately (pricing.html promises "1 cluster, up to 5 nodes"): the node cap alone doesn't stop
# someone registering several small clusters, which is the thing Pro's "unlimited clusters" is meant to be worth.
TIER_LIMITS = {
    "max_nodes": COMMUNITY_NODES,
    "max_clusters": 1,
    "max_dashboards": 10,
    "max_alerts": 15,
    "max_slos": 3,
    "max_synthetics": 3,
    "retention_days": 7,
}
if not IS_PRO:
    RETENTION_HOURS = min(RETENTION_HOURS, TIER_LIMITS["retention_days"] * 24)


def _tier_check(resource: str, table: str):
    """Raise 403 if the community tier limit is reached for a resource."""
    if IS_PRO:
        return
    limit = TIER_LIMITS.get(f"max_{resource}")
    if limit is None:
        return
    count = q(f"SELECT COUNT(*) n FROM {table}")[0]["n"]
    if count >= limit:
        raise HTTPException(
            403,
            {"error": "tier_limit",
             "message": f"The Community plan allows up to {limit} {_LIMIT_NAMES.get(resource, resource)}. Upgrade to Pro for unlimited.",
             "limit": limit, "current": count, "tier": "community",
             "upgrade_url": PRICING_URL})


def _tier_gate(feature: str, need: str = "pro"):
    """Block a feature the current tier does not include. need: "pro" (Pro and Enterprise) or "enterprise"."""
    if IS_ENTERPRISE or (need == "pro" and IS_PRO):
        return
    raise HTTPException(
        403,
        {"error": "pro_required" if need == "pro" else "enterprise_required",
         "message": f"{feature} is available on the {need.title()} tier. Upgrade to unlock.",
         "feature": feature, "tier": TIER, "required_tier": need,
         "upgrade_url": PRICING_URL})


def _nodes_in_use():
    """Distinct Kubernetes nodes that sent data in the last 24 h; at least 1 once anything arrived. VM hosts are not counted:
    host.name is a container ID inside Docker, so counting it would report every container as a node."""
    try:
        n = qrows("SELECT COUNT(DISTINCT node) FROM resource_seen WHERE node<>'' AND last_seen>=?", (int(time.time()) - 86400,))[0][0]
        if n:
            return int(n)
        return 1 if qrows("SELECT 1 FROM resource_seen LIMIT 1") else 0
    except Exception:
        return 0


def _license_usage():
    """Licensed footprint vs what reports in; over the limit shows a banner, never stops ingest."""
    used = _nodes_in_use()
    return {"nodes_used": used, "nodes_licensed": LICENSED_NODES or None,
            "over_limit": bool(LICENSED_NODES) and used > LICENSED_NODES}

# Deployment mode. "selfhosted" (default) is the classic single-tenant NebuLens: one SQLite file, optional
# NEBULENS_TOKEN. "hosted" is Pulse-hosted multi-tenant: organizations -> projects (one SQLite silo each),
# hashed API keys, per-org quotas. See the "hosted mode" section below.
MODE = os.environ.get("NEBULENS_MODE", "selfhosted").strip().lower()
if MODE not in ("selfhosted", "hosted"):
    raise RuntimeError("NEBULENS_MODE must be 'selfhosted' or 'hosted'")
HOSTED = MODE == "hosted"
ADMIN_TOKEN = os.environ.get("NEBULENS_ADMIN_TOKEN", "")  # bootstrap platform admin (hosted mode only)
DATA_DIR = os.environ.get("NEBULENS_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "tenants"))
if HOSTED and not ADMIN_TOKEN:
    raise RuntimeError("NEBULENS_ADMIN_TOKEN is required when NEBULENS_MODE=hosted")

def _jload(v):
    """json.loads that passes through dicts (Postgres JSONB returns dicts directly)."""
    return v if isinstance(v, (dict, list)) else json.loads(v or "{}")


# ---------------------------------------------------------------- Postgres SQL translation
_PG_TABLES = ("spans", "logs", "metrics", "alert_rules", "alert_events",
              "synthetics", "synthetic_runs", "dashboards", "audit", "slos", "span_rollup", "rollup_state", "edge_rollup", "resource_seen", "env_rollup", "ai_usage", "op_rollup")

def _as_dict(val, default=None):
    """Return a dict from a JSON column. Postgres JSONB arrives as dict already; SQLite TEXT is a string.
    None and empty-string both yield the default (an empty dict unless overridden)."""
    if default is None:
        default = {}
    if val is None or val == "":
        return dict(default)
    if isinstance(val, dict):
        return val
    try:
        out = json.loads(val)
        return out if isinstance(out, dict) else dict(default)
    except Exception:
        return dict(default)


def _as_list(val, default=None):
    """Same as _as_dict but for JSON arrays (panels, etc.)."""
    if default is None:
        default = []
    if val is None or val == "":
        return list(default)
    if isinstance(val, list):
        return val
    try:
        out = json.loads(val)
        return out if isinstance(out, list) else list(default)
    except Exception:
        return list(default)


def _pg_sql(sql):
    """Translate SQLite-flavoured SQL to Postgres dialect."""
    if not USE_PG:
        return sql
    # Parameter placeholders: ? -> %s (but not inside strings). psycopg parses the whole query for %-style
    # placeholders, so a literal '%' (e.g. inside a LIKE pattern like '%cpu%') must be doubled to '%%' first,
    # or psycopg misreads fragments like '%c' as an invalid placeholder.
    out, i, in_str = [], 0, False
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            in_str = not in_str
            out.append(ch)
        elif ch == '?' and not in_str:
            out.append('%s')
        elif ch == '%':
            out.append('%%')
        else:
            out.append(ch)
        i += 1
    sql = ''.join(out)

    # Schema-qualify table names
    for t in _PG_TABLES:
        sql = re.sub(r'\b(FROM|JOIN|INTO|UPDATE|TABLE)\s+' + t + r'\b',
                      lambda m: m.group(1) + ' ' + PG_SCHEMA + '.' + t, sql, flags=re.IGNORECASE)
        sql = re.sub(r'\bINSERT\s+INTO\s+' + PG_SCHEMA + r'\.' + t + r'\s+VALUES',
                      'INSERT INTO ' + PG_SCHEMA + '.' + t + ' VALUES', sql, flags=re.IGNORECASE)
        # ON ... table(col) references in indexes
        sql = re.sub(r'\bON\s+' + t + r'\(', 'ON ' + PG_SCHEMA + '.' + t + '(', sql, flags=re.IGNORECASE)

    # json_extract(col, '$."key"') -> col->>'key'  and  json_extract(col, '$.key') -> col->>'key'
    def _jx(m):
        col, key = m.group(1), m.group(2)
        key = key.strip('"')
        return f"{col}->>'{key}'"
    sql = re.sub(r'''json_extract\(\s*(\w+)\s*,\s*'\$\.["']?([^'"]+)["']?'\s*\)''', _jx, sql)

    # json_extract(col, %s) with a bound '$."key"' path -> jsonpath lookup returning text
    sql = re.sub(r"json_extract\(\s*(\w+)\s*,\s*%s\s*\)", r"(jsonb_path_query_first(\1, %s::jsonpath) #>> '{}')", sql)

    # CAST(col->>'key' AS TEXT) -> col->>'key'  (already text in Postgres)
    sql = re.sub(r"CAST\(\s*([\w.]+->>'[^']+')\s+AS\s+TEXT\)", r'\1', sql, flags=re.IGNORECASE)

    # SUM(expr=val) -> SUM((expr=val)::int)  — Postgres boolean not summable
    sql = re.sub(r"SUM\(([^()=]+=[^()]+)\)", r'SUM((\1)::int)', sql)
    # SUM((ts_ns>=%s)::int) is fine but SUM(ts_ns>=%s) needs the same treatment
    sql = re.sub(r"SUM\(((?:[^()>]|>(?!=))+>=[^()]+)\)", r'SUM((\1)::int)', sql)
    # Fix double-casting: SUM(((expr)::int)::int) -> SUM((expr)::int)
    sql = re.sub(r"\(\(([^()]+)\)::int\)::int", r'(\1)::int', sql)

    # (parent_id<>'') needs no translation, it works in both

    # ORDER BY (parent_id<>'') -> ORDER BY (parent_id<>'')::int  (Postgres can't sort boolean directly
    # in the same way SQLite sorts 0/1, but actually Postgres CAN order by boolean: false < true)
    # So this is fine.

    return sql



_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS spans(
  trace_id TEXT, span_id TEXT, parent_id TEXT, service TEXT, name TEXT, kind INTEGER,
  start_ns INTEGER, end_ns INTEGER, dur_ms REAL, status INTEGER, status_msg TEXT,
  attrs TEXT, res TEXT, env TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_spans_env ON spans(env, start_ns);
CREATE INDEX IF NOT EXISTS ix_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS ix_spans_time ON spans(start_ns);
CREATE INDEX IF NOT EXISTS ix_spans_svc ON spans(service, start_ns);
CREATE TABLE IF NOT EXISTS logs(
  ts_ns INTEGER, service TEXT, severity INTEGER, sev_text TEXT, body TEXT,
  trace_id TEXT, span_id TEXT, attrs TEXT, res TEXT, env TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_logs_env ON logs(env, ts_ns);
CREATE INDEX IF NOT EXISTS ix_logs_time ON logs(ts_ns);
CREATE INDEX IF NOT EXISTS ix_logs_err ON logs(ts_ns, service) WHERE severity>=17;
CREATE TABLE IF NOT EXISTS metrics(
  ts_ns INTEGER, service TEXT, name TEXT, unit TEXT, mtype TEXT, value REAL, attrs TEXT, res TEXT, env TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_metrics_env ON metrics(env, ts_ns);
-- Environments: one row per connected namespace / cluster / deployment environment.
-- Rows appear automatically as telemetry arrives (source='auto'); an operator can rename,
-- recolor or pre-register them (source='manual').
-- One row per (service, environment) that has sent anything. Listing services used to run
-- SELECT DISTINCT service over the whole logs/metrics tables, which takes a minute on a busy cluster.
CREATE TABLE IF NOT EXISTS services_seen(
  service TEXT, env TEXT DEFAULT '', last_seen INTEGER, PRIMARY KEY (service, env));
-- Hourly point counts per (metric, service). The Metrics page used to GROUP BY name over every metric
-- row in the window (millions of rows on a busy cluster, minutes over NFS); this is a few thousand rows.
CREATE TABLE IF NOT EXISTS metric_names_seen(
  name TEXT, unit TEXT DEFAULT '', mtype TEXT DEFAULT '', service TEXT DEFAULT '', hr INTEGER, points INTEGER DEFAULT 0,
  PRIMARY KEY (name, unit, mtype, service, hr));
CREATE INDEX IF NOT EXISTS ix_metric_names_seen_hr ON metric_names_seen(hr);
-- Per-minute rollup of root/server spans, written at ingest: one row per (minute, service, env, duration bucket).
-- RED metrics and latency percentiles read this instead of scanning every span in the window.
CREATE TABLE IF NOT EXISTS span_rollup(
  minute INTEGER, service TEXT, env TEXT DEFAULT '', b INTEGER, n INTEGER, err INTEGER, dur_sum REAL, dur_max REAL,
  PRIMARY KEY (minute, service, env, b));
CREATE INDEX IF NOT EXISTS ix_span_rollup_svc ON span_rollup(service, minute);
-- 'from_minute': the rollup is complete for every minute >= this value; older windows fall back to scanning spans.
CREATE TABLE IF NOT EXISTS rollup_state(key TEXT PRIMARY KEY, value INTEGER);
-- Per-minute service-to-service calls (ext=0: parent span in another service) and calls to uninstrumented backends
-- (ext=1: databases, queues, named peers). Filled by a worker job over spans that are a couple of minutes old, so
-- parents that finish after their children are already stored. 'edge_cursor' (ns) is how far it has got.
-- One row per distinct resource (service + cluster/namespace/node/pod/deployment + SDK) per signal, written at ingest. The Kubernetes
-- page used to re-read 20,000 rows per table, with their full resource JSON, on every call (7 s, and an arbitrary sample at that).
CREATE TABLE IF NOT EXISTS resource_seen(
  sig TEXT, service TEXT, cluster TEXT DEFAULT '', ns TEXT DEFAULT '', node TEXT DEFAULT '', pod TEXT DEFAULT '',
  deployment TEXT DEFAULT '', lang TEXT DEFAULT '', sdk_version TEXT DEFAULT '', svc_version TEXT DEFAULT '', env TEXT DEFAULT '',
  last_seen INTEGER,
  PRIMARY KEY (sig, service, cluster, ns, node, pod, deployment, lang, sdk_version, svc_version, env));
CREATE INDEX IF NOT EXISTS ix_resource_seen_ts ON resource_seen(last_seen);
-- Per-minute span and log counts per (environment, service), ALL spans and logs (not just roots). The Environments page used to
-- scan every span and log in the window four times, once parsing each row's resource JSON for its namespace.
CREATE TABLE IF NOT EXISTS env_rollup(
  minute INTEGER, env TEXT DEFAULT '', service TEXT, spans INTEGER DEFAULT 0, span_err INTEGER DEFAULT 0,
  logs INTEGER DEFAULT 0, log_err INTEGER DEFAULT 0, PRIMARY KEY (minute, env, service));
-- AI answers used per UTC day (Community has a daily allowance; Pro is unlimited).
CREATE TABLE IF NOT EXISTS ai_usage(day TEXT PRIMARY KEY, n INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS edge_rollup(
  minute INTEGER, src TEXT, dst TEXT, ext INTEGER, calls INTEGER, err INTEGER, dur_sum REAL,
  PRIMARY KEY (minute, src, dst, ext));
CREATE TABLE IF NOT EXISTS environments(
  slug TEXT PRIMARY KEY, name TEXT, cluster TEXT DEFAULT '', namespace TEXT DEFAULT '',
  env_attr TEXT DEFAULT '', color TEXT DEFAULT '', kind TEXT DEFAULT 'namespace',
  source TEXT DEFAULT 'auto', first_seen INTEGER, last_seen INTEGER);
-- Pro: which environments an SSO group may see. No row for a group = every environment.
CREATE TABLE IF NOT EXISTS env_access(
  grp TEXT, slug TEXT, PRIMARY KEY (grp, slug));
-- Multi-cluster console: one row per remote cluster allowed to send telemetry to this NebuLens. Each cluster has its
-- own ingest token, stored only as a SHA-256 hash; the plain token is shown once, when it is created or rotated.
-- A cluster's telemetry is always stored under the environment whose slug equals the cluster slug.
CREATE TABLE IF NOT EXISTS clusters(
  slug TEXT PRIMARY KEY, name TEXT, token_hash TEXT, token_prefix TEXT DEFAULT '', note TEXT DEFAULT '',
  disabled INTEGER DEFAULT 0, created INTEGER, last_seen INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_clusters_token ON clusters(token_hash);
CREATE INDEX IF NOT EXISTS ix_metrics ON metrics(name, ts_ns);
CREATE TABLE IF NOT EXISTS alert_rules(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, signal TEXT, expr TEXT, op TEXT,
  threshold REAL, window_s INTEGER, service TEXT, webhook TEXT, enabled INTEGER DEFAULT 1,
  state TEXT DEFAULT 'ok', last_value REAL, last_eval INTEGER, since INTEGER);
CREATE TABLE IF NOT EXISTS alert_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER, ts INTEGER, state TEXT, value REAL, msg TEXT);
CREATE TABLE IF NOT EXISTS synthetics(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, url TEXT, interval_s INTEGER DEFAULT 60,
  expect_status INTEGER DEFAULT 200, max_ms REAL DEFAULT 2000, enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS synthetic_runs(
  check_id INTEGER, ts INTEGER, ok INTEGER, status INTEGER, ms REAL, ssl_days REAL, err TEXT);
CREATE INDEX IF NOT EXISTS ix_synth ON synthetic_runs(check_id, ts);
CREATE TABLE IF NOT EXISTS dashboards(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, panels TEXT, updated INTEGER);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, actor TEXT, role TEXT, via TEXT, method TEXT,
  path TEXT, status INTEGER, detail TEXT, ip TEXT DEFAULT '', req_id TEXT DEFAULT '',
  envs TEXT DEFAULT '', prev_hash TEXT DEFAULT '', hash TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_audit ON audit(ts);
CREATE TABLE IF NOT EXISTS view_tombstones(slug TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS integration_installs(
  integration_id TEXT PRIMARY KEY, installed_at INTEGER,
  dashboard_ids TEXT DEFAULT '[]', view_ids TEXT DEFAULT '[]', alert_ids TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS views(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
  params TEXT NOT NULL, description TEXT DEFAULT '', builtin INTEGER DEFAULT 0, favorite INTEGER DEFAULT 0,
  updated INTEGER);
CREATE INDEX IF NOT EXISTS ix_views_kind ON views(kind);
-- Per-minute calls per (service, operation) for server/consumer/root spans, written at ingest. The Operations tab and
-- "Ask NebuLens" used to GROUP BY name over every span of the service in the window (20-60 s at a few million spans).
CREATE TABLE IF NOT EXISTS op_rollup(
  minute INTEGER, service TEXT, env TEXT DEFAULT '', name TEXT, n INTEGER DEFAULT 0, err INTEGER DEFAULT 0,
  dur_sum REAL DEFAULT 0, dur_max REAL DEFAULT 0, PRIMARY KEY (minute, service, env, name));
CREATE INDEX IF NOT EXISTS ix_op_rollup_svc ON op_rollup(service, minute);
CREATE TABLE IF NOT EXISTS slos(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, service TEXT,
  objective_pct REAL, latency_ms REAL, window_days INTEGER DEFAULT 30);
"""


_ENV_COLUMN_TABLES = ("spans", "logs", "metrics")
# Columns added to the audit trail after 0.1.0. Old rows keep empty values and verify as
# "unchained" rather than as tampered with.
_AUDIT_NEW_COLUMNS = (("ip", "TEXT DEFAULT ''"), ("req_id", "TEXT DEFAULT ''"), ("envs", "TEXT DEFAULT ''"),
                      ("prev_hash", "TEXT DEFAULT ''"), ("hash", "TEXT DEFAULT ''"))


def _migrate_env_column(conn):
    """Add columns introduced after a database was first created. Old rows keep empty values."""
    for t in _ENV_COLUMN_TABLES:
        try:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
            if cols and "env" not in cols:
                conn.execute(f"ALTER TABLE {t} ADD COLUMN env TEXT DEFAULT ''")
        except Exception as e:
            print(f"env migration ({t}):", e)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(audit)")}
        for name, decl in _AUDIT_NEW_COLUMNS:
            if cols and name not in cols:
                conn.execute(f"ALTER TABLE audit ADD COLUMN {name} {decl}")
    except Exception as e:
        print("audit migration:", e)


def _open_db(path):
    c = sqlite3.connect(path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    # Migrate BEFORE the schema script: it creates indexes on the env column, which would fail on a
    # database written by an older NebuLens and abort the whole script.
    _migrate_env_column(c)
    c.executescript(_SCHEMA)
    c.execute("INSERT OR IGNORE INTO rollup_state(key, value) VALUES('from_minute', ?)", (int(time.time() // 60),))
    c.execute("INSERT OR IGNORE INTO rollup_state(key, value) VALUES('res_from', ?)", (int(time.time() // 60),))
    c.execute("INSERT OR IGNORE INTO rollup_state(key, value) VALUES('env_from', ?)", (int(time.time() // 60),))
    c.execute("INSERT OR IGNORE INTO rollup_state(key, value) VALUES('op_from', ?)", (int(time.time() // 60),))
    c.commit()
    return c


# ---- Postgres pool (when USE_PG) ----
_pg_pool = None


def _default_pg_schema():
    """'nebulens', or the schema an older version filled ('oscilyx', then 'pulse0') while no nebulens schema exists."""
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=10) as c:
            have = {r[0] for r in c.execute("SELECT schema_name FROM information_schema.schemata "
                                            "WHERE schema_name IN ('nebulens', 'oscilyx', 'pulse0')").fetchall()}
        if "nebulens" not in have:
            for old in ("oscilyx", "pulse0"):
                if old in have:
                    return old
        return "nebulens"
    except Exception:
        return "nebulens"


if USE_PG:
    PG_SCHEMA = PG_SCHEMA or _default_pg_schema()
    _pg_pool = ConnectionPool(DATABASE_URL, min_size=2, max_size=10, open=True,
                              kwargs={"options": f"-c search_path={PG_SCHEMA},public"})


def _pg_ddl():
    """The SQLite schema above, translated to Postgres types."""
    ddl = "\n".join(ln for ln in _SCHEMA.splitlines() if not ln.startswith("PRAGMA"))
    ddl = ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    ddl = re.sub(r"\bINTEGER\b", "BIGINT", ddl)
    ddl = re.sub(r"\bREAL\b", "DOUBLE PRECISION", ddl)
    return re.sub(r"\b(attrs|res) TEXT", r"\1 JSONB", ddl)


if USE_PG:
    if not PG_SCHEMA.isidentifier():
        raise RuntimeError("NEBULENS_PG_SCHEMA must be a plain identifier")
    with _pg_pool.connection() as _c:
        _c.execute(f'CREATE SCHEMA IF NOT EXISTS "{PG_SCHEMA}"')
        _c.execute(_pg_ddl())
        _c.execute(f"INSERT INTO {PG_SCHEMA}.rollup_state(key, value) VALUES('from_minute', %s) ON CONFLICT (key) DO NOTHING",
                   (int(time.time() // 60),))
        for _k in ("res_from", "env_from", "op_from"):
            _c.execute(f"INSERT INTO {PG_SCHEMA}.rollup_state(key, value) VALUES(%s, %s) ON CONFLICT (key) DO NOTHING",
                       (_k, int(time.time() // 60)))
        for _t in ("spans", "logs", "metrics"):  # pre-environments databases
            _c.execute(f'ALTER TABLE {PG_SCHEMA}.{_t} ADD COLUMN IF NOT EXISTS env TEXT DEFAULT \'\'')

# ---- SQLite single-tenant (when not USE_PG) ----
_lock = threading.Lock()
_conn = None if USE_PG else _open_db(DB_PATH)
if USE_PG:
    _init_trial(None)
elif _conn:
    _init_trial(DB_PATH)
_print_tier()

# Which SQLite silo the current request / worker iteration reads and writes. None = the default
# (self-hosted) database. The hosted-mode auth middleware sets this from the API key's project, so
# every query helper below (db/q) is tenant-scoped without touching any query code.
_cur_db = contextvars.ContextVar("nebulens_db", default=None)
_tenants = collections.OrderedDict()  # path -> (conn, lock); small LRU of open project databases
_tenants_guard = threading.Lock()
MAX_OPEN_TENANTS = int(os.environ.get("NEBULENS_MAX_OPEN_PROJECTS", "64"))


def _handle():
    path = _cur_db.get()
    if path is None:
        return _conn, _lock
    with _tenants_guard:
        h = _tenants.get(path)
        if h is None:
            h = _tenants[path] = (_open_db(path), threading.Lock())
        _tenants.move_to_end(path)
        while len(_tenants) > MAX_OPEN_TENANTS:
            _tenants.popitem(last=False)  # dropped refs are closed by GC once no thread holds them
        return h


class _PgCursor:
    """Thin wrapper so pg cursors quack like sqlite3 cursors for our helpers."""
    def __init__(self, conn):
        self._conn = conn
    def execute(self, sql, args=()):
        return self._conn.execute(_pg_sql(sql), args)
    def executemany(self, sql, rows):
        cur = self._conn.cursor()
        cur.executemany(_pg_sql(sql), rows)
        return cur


@contextmanager
def db():
    if USE_PG:
        with _pg_pool.connection() as conn:
            yield _PgCursor(conn)
            conn.commit()
    else:
        conn, lock = _handle()
        with lock:
            yield conn
            conn.commit()


_readers = threading.local()
# A page read that runs longer than this is stopped: the browser gives up after 20 s anyway, and an abandoned scan kept running
# and slowed every later request. Only API requests are limited (rollup backfills and the cache warmer are not).
QUERY_TIMEOUT = float(os.environ.get("NEBULENS_QUERY_TIMEOUT", "25") or 0)
_q_limited = contextvars.ContextVar("q_limited", default=False)


def _progress_check():
    d = getattr(_readers, "deadline", 0)
    return 1 if d and time.monotonic() > d else 0


def _rd(c, sql, args):
    """Run a read on this thread's connection, stopped after QUERY_TIMEOUT seconds when serving an API request."""
    lim = QUERY_TIMEOUT > 0 and _q_limited.get() and not getattr(_warming, "on", False)
    _readers.deadline = time.monotonic() + QUERY_TIMEOUT if lim else 0
    try:
        return c.execute(sql, args).fetchall()
    finally:
        _readers.deadline = 0


def _reader():
    """Per-thread read connection to the default DB. WAL lets readers run alongside the single writer,
    so slow dashboard queries no longer queue behind (or block) ingest on the global lock."""
    c = getattr(_readers, "c", None)
    if c is None or getattr(_readers, "path", None) != DB_PATH:
        c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5)
        c.row_factory = sqlite3.Row
        c.set_progress_handler(_progress_check, 20000)
        _readers.c, _readers.path = c, DB_PATH
    return c


def _pg_limit(conn):
    if QUERY_TIMEOUT > 0 and _q_limited.get() and not getattr(_warming, "on", False):
        conn.execute(f"SET LOCAL statement_timeout = {int(QUERY_TIMEOUT * 1000)}")


def qrows(sql, args=()):
    """Raw row tuples (no dict conversion) for large scans aggregated by the caller."""
    if USE_PG:
        with _pg_pool.connection() as conn:
            _pg_limit(conn)
            return conn.execute(_pg_sql(sql), args).fetchall()
    if _cur_db.get() is None:
        return _rd(_reader(), sql, args)
    conn, lock = _handle()
    with lock:
        return conn.execute(sql, args).fetchall()


def qcol(sql, args=()):
    """First column of every row as a plain list (no per-row dicts: much cheaper for big scans)."""
    if USE_PG:
        with _pg_pool.connection() as conn:
            return [r[0] for r in conn.execute(_pg_sql(sql), args)]
    if _cur_db.get() is None:
        return [r[0] for r in _reader().execute(sql, args)]
    conn, lock = _handle()
    with lock:
        return [r[0] for r in conn.execute(sql, args)]


# ---------------------------------------------------------------- heavy-read cache
# Aggregates over the whole time window (services, map, insights, home) cost seconds at ~1M spans. Concurrent callers share one
# computation (single-flight) and results live for CACHE_TTL seconds; a background warmer refreshes them while the UI is in use.
CACHE_TTL = float(os.environ.get("NEBULENS_CACHE_TTL", "20"))
_last_use = [0.0]
_warming = threading.local()


def _plain_args(sig, a, kw):
    """Bind a call to its signature with defaults applied, replacing FastAPI Query(...) defaults by their real value:
    outside a request FastAPI never resolves them, so a direct call (the cache warmer) would pass a Query object."""
    from fastapi.params import Param
    b = sig.bind(*a, **kw); b.apply_defaults()
    for k, v in list(b.arguments.items()):
        if isinstance(v, Param):
            b.arguments[k] = v.default
    return b


def ttl_cached(fn):
    import functools, inspect
    sig = inspect.signature(fn)
    cache, locks = {}, {}

    def key_of(a, kw):
        b = _plain_args(sig, a, kw)
        # The caller's environment scope is part of the key: without it a restricted user could be
        # served another user's cached, wider result (or vice versa).
        envs = _cur_envs.get()
        return (_cur_db.get(), None if envs is None else tuple(sorted(envs)), tuple(sorted(b.arguments.items())))

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        bound = _plain_args(sig, a, kw)       # real values even for a direct call that left a Query(...) default
        a, kw = bound.args, bound.kwargs
        if CACHE_TTL <= 0:
            return fn(*a, **kw)
        if not getattr(_warming, "on", False):
            _last_use[0] = time.time()
        k = key_of(a, kw)
        hit = cache.get(k)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        with locks.setdefault(k, threading.Lock()):
            hit = cache.get(k)
            if hit and time.time() - hit[0] < CACHE_TTL:
                return hit[1]
            v = fn(*a, **kw)
            if len(cache) > 256:
                cache.pop(min(cache, key=lambda x: cache[x][0]), None)
            cache[k] = (time.time(), v)
            return v

    def refresh(*a, **kw):
        k = key_of(a, kw)
        b = _plain_args(sig, a, kw)
        with locks.setdefault(k, threading.Lock()):
            cache[k] = (time.time(), fn(*b.args, **b.kwargs))

    wrapper.refresh = refresh
    return wrapper


def q(sql, args=()):
    if USE_PG:
        with _pg_pool.connection() as conn:
            if sql.lstrip().upper().startswith(("SELECT", "WITH")):
                _pg_limit(conn)
            with conn.cursor(row_factory=dict_row) as cur:
                return cur.execute(_pg_sql(sql), args).fetchall()
    if _cur_db.get() is None and sql.lstrip().upper().startswith(("SELECT", "WITH")):
        return [dict(r) for r in _rd(_reader(), sql, args)]
    conn, lock = _handle()
    with lock:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ---------------------------------------------------------------- PII redaction
# Regulated deployments must not persist card numbers, national IDs or credentials, which routinely
# leak into log lines and span attributes. Redaction runs at ingest, BEFORE anything is written, so
# the raw value never reaches the database or a backup of it.
#   NEBULENS_REDACT=card,ssn,iban,email,jwt,apikey,phone   (default: card,ssn,iban,jwt,apikey)
#   NEBULENS_REDACT=off                                    disable entirely
#   NEBULENS_REDACT_CUSTOM='name=<regex>;name2=<regex>'    extra patterns, e.g. acct=\bACC-\d{8}\b
# Each match becomes <name:redacted>, so the shape of the log stays readable and searchable.
_LUHN_RE = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
_PII_PATTERNS = {
    "ssn":    re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
    "iban":   re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    "email":  re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    "jwt":    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    "apikey": re.compile(r"(?i)\b(?:api[_-]?key|secret|password|passwd|token|authorization)\b\s*[:=]\s*[\"']?([^\s\"',;}]{6,})"),
    "phone":  re.compile(r"(?<![\d-])\+?\d{1,3}[ -]?\(?\d{3}\)?[ -]?\d{3}[ -]?\d{4}(?![\d-])"),
}
_REDACT_DEFAULT = "card,ssn,iban,jwt,apikey"
_redact_names = [x.strip().lower() for x in os.environ.get("NEBULENS_REDACT", _REDACT_DEFAULT).split(",") if x.strip()]
REDACT_OFF = "off" in _redact_names or "none" in _redact_names
_REDACT_RULES = []
if not REDACT_OFF:
    for _n in _redact_names:
        if _n == "card":
            _REDACT_RULES.append(("card", _LUHN_RE, True))
        elif _n in _PII_PATTERNS:
            _REDACT_RULES.append((_n, _PII_PATTERNS[_n], False))
    for _spec in os.environ.get("NEBULENS_REDACT_CUSTOM", "").split(";"):
        if "=" in _spec:
            _nm, _, _rx = _spec.partition("=")
            try:
                _REDACT_RULES.append((_nm.strip()[:32] or "custom", re.compile(_rx), False))
            except re.error as _e:
                print(f"invalid NEBULENS_REDACT_CUSTOM pattern for {_nm!r}: {_e}")

_redact_counts = collections.Counter()   # what was redacted, for the compliance report
_REDACT_MAXLEN = 20000                   # don't scan unbounded blobs; they are truncated anyway


def _luhn_ok(digits):
    """Only redact digit runs that pass the Luhn check, so order ids and timestamps survive."""
    tot, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        tot += d; alt = not alt
    return tot % 10 == 0


def _card_issuer_ok(d):
    """True when the digit string starts like a real payment-card number (Visa, Mastercard, Amex,
    Discover, JCB, Diners, UnionPay, Maestro) and has a length that network issues. Luhn alone passes
    1 random digit string in 10, so 19-digit nanosecond timestamps (which start with 1) and many
    numeric ids were being redacted as cards."""
    n = len(d)
    p2, p3, p4 = int(d[:2]), int(d[:3]), int(d[:4])
    if d[0] == "4":                                   return n in (13, 16, 19)           # Visa
    if 51 <= p2 <= 55 or 2221 <= p4 <= 2720:          return n == 16                     # Mastercard
    if p2 in (34, 37):                                return n == 15                     # American Express
    if p4 == 6011 or p2 == 65 or 644 <= p3 <= 649:    return 16 <= n <= 19               # Discover
    if p2 == 62:                                      return 16 <= n <= 19               # UnionPay
    if 3528 <= p4 <= 3589:                            return 16 <= n <= 19               # JCB
    if 300 <= p3 <= 305 or p2 in (36, 38, 39):        return 14 <= n <= 19               # Diners
    if p2 in (50, 56, 57, 58) or p4 in (6304, 6759) or 6761 <= p4 <= 6763:
                                                      return 12 <= n <= 19               # Maestro
    return False


def redact(text):
    """Replace sensitive values in a string. Returns the string unchanged when nothing matches."""
    if REDACT_OFF or not text or not isinstance(text, str):
        return text
    head, tail = text[:_REDACT_MAXLEN], text[_REDACT_MAXLEN:]
    for name, rx, luhn in _REDACT_RULES:
        if luhn:
            def _sub(m):
                digits = re.sub(r"[ -]", "", m.group(0))
                if 13 <= len(digits) <= 19 and _luhn_ok(digits) and _card_issuer_ok(digits):
                    _redact_counts[name] += 1
                    return f"<{name}:redacted>"
                return m.group(0)
            head = rx.sub(_sub, head)
        elif name == "apikey":
            def _sub2(m):
                _redact_counts[name] += 1
                return m.group(0).replace(m.group(1), f"<{name}:redacted>")
            head = rx.sub(_sub2, head)
        else:
            def _sub3(m, _n=name):
                _redact_counts[_n] += 1
                return f"<{_n}:redacted>"
            head = rx.sub(_sub3, head)
    return head + tail


def redact_attrs(d):
    """Redact every string value in an attribute dict (keys are left alone)."""
    if REDACT_OFF or not isinstance(d, dict):
        return d
    for k, v in d.items():
        if isinstance(v, str):
            d[k] = redact(v)
    return d


# ---------------------------------------------------------------- environments
# An environment is one connected slice of telemetry: a Kubernetes namespace, a whole cluster, or
# whatever a service reports in deployment.environment. Resolution order for each incoming batch:
#   1. deployment.environment       (an app that labels itself "staging"/"prod" wins)
#   2. <cluster>/<namespace>        (multi-cluster: the same namespace name in two clusters stays apart)
#   3. k8s.namespace.name           (single cluster)
# The resolved slug is stored on every span, log and metric, so filtering is an indexed column lookup
# rather than a JSON scan. Environments register themselves the first time data arrives.
_ENV_MAX = 200                       # safety cap: never auto-register an unbounded number of slugs
_env_cache = {}                      # slug -> last_seen (unix seconds), flushed to the table periodically
_env_known = set()
_env_lock = threading.Lock()
_views_seeded = False
BUILTIN_VIEWS = [
    # (slug, name, kind, description, params)
    ("all-errors",     "All errors",        "logs",   "Every ERROR and above in the current range.",
        {"min_severity": 17}),
    ("warn-and-up",    "Warnings and above", "logs",  "Noisy enough to notice but not pages: WARN, ERROR, FATAL.",
        {"min_severity": 13}),
    ("pod-logs",       "Pod logs",          "logs",   "Everything collected from Kubernetes pods, grouped by pod.",
        {"groupby": "k8s.pod.name"}),
    ("k8s-events",     "Kubernetes events", "logs",   "Events emitted by the cluster itself (Warning and above).",
        {"q": "event.name:", "min_severity": 9}),
    ("noisy-services", "Noisiest services", "logs",   "Which services are producing the most logs right now.",
        {"groupby": "service"}),
    ("recurring",      "Recurring patterns", "logs",  "Groups of similar messages, the ones that happen often.",
        {"view": "patterns"}),
    ("latest-errors",  "Latest errors",     "errors", "Error groups seen in the current range, newest first.",
        {"sort": "last_seen"}),
    ("frequent-errors","Most frequent errors","errors","Error groups by count: fix one to silence many.",
        {"sort": "count"}),
    ("new-errors",     "New errors",        "errors", "Error groups that first appeared in the last 24 hours.",
        {"new_within_hours": 24}),
]


def _seed_builtin_views():
    """Create the built-in views once per database. User deletes are honoured via the
    tombstone table below so this never resurrects them."""
    global _views_seeded
    if _views_seeded:
        return
    now = int(time.time())
    try:
        with db() as c:
            killed = {r[0] for r in c.execute("SELECT slug FROM view_tombstones").fetchall()}
            for slug, name, kind, desc, params in BUILTIN_VIEWS:
                if slug in killed:
                    continue
                c.execute("""INSERT INTO views(slug, name, kind, params, description, builtin, updated)
                             VALUES(?,?,?,?,?,1,?) ON CONFLICT(slug) DO NOTHING""",
                          (slug, name, kind, json.dumps(params), desc, now))
        _views_seeded = True
    except Exception as e:
        print("view seeding failed:", e)


_env_loaded = False
_svc_pending = {}   # (service, env) -> last seen (unix s), written to services_seen by _svc_flush
_svc_written = {}   # (service, env) -> when we last wrote it; rewriting more than every 30s is wasted I/O


def _svc_seen(service, env):
    if service:
        _svc_pending[(service, env or "")] = int(time.time())


def _svc_flush():
    if not _svc_pending:
        return
    now = int(time.time())
    with _env_lock:
        batch = {k: v for k, v in _svc_pending.items() if now - _svc_written.get(k, 0) >= 30}
        for k in batch:
            _svc_pending.pop(k, None)
    if not batch:
        return
    try:
        with db() as c:
            for (svc, env), ts in batch.items():
                c.execute("""INSERT INTO services_seen(service, env, last_seen) VALUES(?,?,?)
                             ON CONFLICT(service, env) DO UPDATE SET last_seen=excluded.last_seen""", (svc, env, ts))
        with _env_lock:
            for k in batch:
                _svc_written[k] = now
    except Exception as e:
        print("service registry flush error:", e)


_res_pending = {}   # key -> last seen (unix s), written to resource_seen by _res_flush
_res_written = {}   # key -> when it was last written; rewriting more than every 60s is wasted I/O
_RES_ATTRS = ("k8s.cluster.name", "k8s.namespace.name", "k8s.node.name", "k8s.pod.name", "k8s.deployment.name",
              "telemetry.sdk.language", "telemetry.sdk.version", "service.version")


def _res_seen(sig, service, res, env):
    """Remember that this (service, cluster, namespace, node, pod, ...) sent this signal. One dict write per resource per
    batch; the table only ever holds one row per distinct resource, so it stays small however many spans arrive."""
    if not service or not isinstance(res, dict):
        return
    a = res.get("attributes") if isinstance(res.get("attributes"), dict) else res
    g = lambda k: str(a.get(k) or "")
    node = g("k8s.node.name")
    key = (sig, service, g("k8s.cluster.name"), str(a.get("k8s.namespace.name") or a.get("service.namespace") or ""), node,
           g("k8s.pod.name"), g("k8s.deployment.name"), g("telemetry.sdk.language"), g("telemetry.sdk.version"),
           g("service.version"), env or "")
    if len(_res_pending) > 50_000:      # a pathological sender (a new pod name every request) must not grow memory without bound
        _res_pending.clear()
    _res_pending[key] = int(time.time())


_RES_UPSERT = ("INSERT INTO resource_seen(sig,service,cluster,ns,node,pod,deployment,lang,sdk_version,svc_version,env,last_seen) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sig,service,cluster,ns,node,pod,deployment,lang,sdk_version,svc_version,env) "
               "DO UPDATE SET last_seen=CASE WHEN excluded.last_seen>resource_seen.last_seen THEN excluded.last_seen ELSE resource_seen.last_seen END")


def _res_flush():
    if not _res_pending:
        return
    now = int(time.time())
    with _env_lock:
        batch = {k: v for k, v in _res_pending.items() if now - _res_written.get(k, 0) >= 60}
        for k in batch:
            _res_pending.pop(k, None)
    if not batch:
        return
    try:
        with db() as c:      # sorted by key: concurrent batches take their row locks in one order (no deadlocks on Postgres)
            c.executemany(_RES_UPSERT, [(*k, ts) for k, ts in sorted(batch.items())])
        with _env_lock:
            for k in batch:
                _res_written[k] = now
        if len(_res_written) > 100_000:
            _res_written.clear()
    except Exception as e:
        print("resource registry flush error:", e)


def _slug(*parts):
    raw = "/".join(p for p in parts if p)
    return re.sub(r"[^a-zA-Z0-9._/-]+", "-", raw).strip("-/")[:120]


# A human-friendly name for the cluster this NebuLens runs in, when telemetry doesn't carry
# k8s.cluster.name. Set via the chart (env.NEBULENS_CLUSTER_NAME) so every namespace in this cluster
# rolls up to one environment named after the cluster.
CLUSTER_NAME = os.environ.get("NEBULENS_CLUSTER_NAME", "").strip()


def _env_of(res):
    """Resolve the environment for a resource. An environment is a deployment.environment value
    (staging/prod/...) when present, otherwise the cluster. Namespaces live INSIDE an environment,
    they are not environments themselves. Returns (slug, meta) with meta.namespace for roll-up."""
    if not isinstance(res, dict):
        return "", {}
    cluster = (res.get("k8s.cluster.name") or res.get("cluster") or CLUSTER_NAME).strip()
    ns = (res.get("k8s.namespace.name") or res.get("service.namespace") or "").strip()
    denv = (res.get("deployment.environment") or res.get("deployment.environment.name") or "").strip()
    cl = _cur_cluster.get()
    if cl:  # data from a registered cluster always lands in that cluster's environment
        return cl["slug"], {"kind": "cluster", "cluster": cl["name"], "namespace": ns, "env_attr": denv}
    if denv:
        return _slug(denv), {"kind": "deployment", "cluster": cluster, "namespace": ns, "env_attr": denv}
    if cluster:
        return _slug(cluster), {"kind": "cluster", "cluster": cluster, "namespace": ns, "env_attr": ""}
    # No cluster name configured and none in telemetry: fall back to one implicit environment so the
    # page still works, and tell the operator how to name it.
    return "default", {"kind": "cluster", "cluster": "", "namespace": ns, "env_attr": "", "unnamed": True}


def _env_seen(slug, meta):
    """Remember that this environment is alive. Registration is written by the flush below."""
    if not slug:
        return
    now = int(time.time())
    with _env_lock:
        if slug not in _env_known and len(_env_known) >= _ENV_MAX:
            return
        _env_cache[slug] = (now, meta)


def _env_flush():
    """Persist newly seen environments and their last_seen. Cheap: one pass over a small dict."""
    global _env_loaded
    _svc_flush()
    _res_flush()
    with _env_lock:
        pending, _env_cache_copy = dict(_env_cache), None
        _env_cache.clear()
    if not pending and _env_loaded:
        return
    try:
        if not _env_loaded:
            with _env_lock:
                _env_known.update(r["slug"] for r in q("SELECT slug FROM environments"))
                _env_loaded = True
        with db() as c:
            for slug, (ts, meta) in pending.items():
                if slug in _env_known:
                    c.execute("UPDATE environments SET last_seen=? WHERE slug=?", (ts, slug))
                else:
                    c.execute("""INSERT INTO environments(slug,name,cluster,namespace,env_attr,kind,source,first_seen,last_seen)
                                 VALUES(?,?,?,?,?,?,'auto',?,?)""",
                              (slug, slug, meta.get("cluster", ""), meta.get("namespace", ""),
                               meta.get("env_attr", ""), meta.get("kind", "namespace"), ts, ts))
                    with _env_lock:
                        _env_known.add(slug)
    except Exception as e:
        print("environment flush error:", e)


# Environments the current request may read. None = unrestricted. Set by the auth middleware from
# the caller's SSO groups (Pro); every query helper that touches telemetry applies it.
_cur_envs = contextvars.ContextVar("nebulens_envs", default=None)

# ---- Multi-cluster console --------------------------------------------------------------------------------------
# A remote cluster sends its telemetry with its own token (p0c_...). The token decides which cluster the data belongs
# to: the server overwrites k8s.cluster.name and files every record under that cluster's environment, so a sender can
# neither claim another cluster's name nor write into another cluster's environment. Set by the ingest middleware.
_cur_cluster = contextvars.ContextVar("nebulens_cluster", default=None)
CLUSTER_TOKEN_PREFIX = "p0c_"
_CLUSTER_CACHE_TTL = 10.0     # seconds; a rotated, disabled or deleted token stops working within this on every replica
_cluster_cache = {}           # token_hash -> (expires_monotonic, row or None)
_cluster_touch_at = {}        # slug -> last time last_seen was written (written at most every 30 s)


def _hash_token(tok):
    return hashlib.sha256(tok.encode()).hexdigest()


def _cluster_slug(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40]


def _cluster_cache_clear():
    _cluster_cache.clear()


def _cluster_for_token(tok):
    """The registered cluster row for a p0c_ token, or None. Looked up by hash with a short cache."""
    if not tok or not tok.startswith(CLUSTER_TOKEN_PREFIX) or len(tok) > 200:
        return None
    h = _hash_token(tok)
    now = time.monotonic()
    hit = _cluster_cache.get(h)
    if hit and hit[0] > now:
        return hit[1]
    rows = q("SELECT slug, name, disabled FROM clusters WHERE token_hash=?", (h,))
    row = {"slug": rows[0]["slug"], "name": rows[0]["name"], "disabled": bool(rows[0]["disabled"])} if rows else None
    if len(_cluster_cache) > 2000:   # unknown tokens are cached too, but never without bound
        _cluster_cache.clear()
    _cluster_cache[h] = (now + _CLUSTER_CACHE_TTL, row)
    return row


def _cluster_touch_due(slug):
    return time.time() - _cluster_touch_at.get(slug, 0) >= 30


def _cluster_touch(slug):
    now = time.time()
    _cluster_touch_at[slug] = now
    try:
        with db() as c:
            c.execute("UPDATE clusters SET last_seen=? WHERE slug=?", (int(now), slug))
    except Exception as e:
        print("cluster last_seen update error:", e)


def env_scope(param: str = "", column: str = "env"):
    """(sql_fragment, args) restricting a query to the caller's allowed environments and, optionally,
    to one environment they asked for. Raises 403 if they ask for one they may not see."""
    allowed = _cur_envs.get()
    want = [e for e in (param or "").split(",") if e.strip()]
    if want and allowed is not None:
        denied = [e for e in want if e not in allowed]
        if denied:
            raise HTTPException(403, {"error": "env_forbidden",
                                      "message": f"You do not have access to environment(s): {', '.join(denied)}",
                                      "environments": sorted(allowed)})
    sel = want or (sorted(allowed) if allowed is not None else [])
    if not sel:
        return "", []
    return f" AND {column} IN (" + ",".join("?" * len(sel)) + ")", list(sel)


def _env_groups_allowed(groups, role=""):
    """Allowed environment slugs for a set of SSO groups, or None when unrestricted.

    Deny-by-default once any rule exists: a user whose groups match no rule sees nothing, whether
    they have no groups at all or only unlisted ones. Admins are always exempt, so a misconfigured
    rule can never lock every operator out of their own install."""
    if not IS_PRO or role == "admin":
        return None
    rows = q("SELECT grp, slug FROM env_access")
    if not rows:
        return None  # no rules configured: everyone sees everything
    by = {}
    for r in rows:
        by.setdefault(r["grp"], set()).add(r["slug"])
    out = set()
    for g in set(by) & set(groups or []):
        out |= by[g]
    return out


# ---------------------------------------------------------------- OTLP decoding
def _val(v):
    """OTLP AnyValue (JSON form) -> python."""
    if not isinstance(v, dict):
        return v
    for k in ("stringValue", "boolValue", "doubleValue"):
        if k in v:
            return v[k]
    if "intValue" in v:
        return int(v["intValue"])
    if "arrayValue" in v:
        return [_val(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return _kv(v["kvlistValue"].get("values", []))
    return None


def _kv(lst):
    return {a["key"]: _val(a.get("value", {})) for a in (lst or [])}


def _res(rs):
    r = _kv(rs.get("resource", {}).get("attributes", []))
    cl = _cur_cluster.get()
    if cl:
        r["k8s.cluster.name"] = cl["name"]       # the token decides the cluster, whatever the sender claims
    elif CLUSTER_NAME and not r.get("k8s.cluster.name"):
        r["k8s.cluster.name"] = CLUSTER_NAME     # this NebuLens's own cluster: name it so the Kubernetes pages can group by it
    return r.get("service.name", "unknown"), r


def _ns(x):
    return int(x) if x not in (None, "") else 0


def _hex(x):
    # JSON OTLP uses hex strings; protobuf->dict gives base64, handled in _to_json
    return x or ""


def _body_json(raw: bytes, ctype: str, msg_cls):
    if "protobuf" in ctype:
        from google.protobuf.json_format import MessageToDict
        m = msg_cls()
        m.ParseFromString(raw)
        d = MessageToDict(m, preserving_proto_field_name=False)
        return _fix_ids(d)
    return json.loads(raw or b"{}")


def _fix_ids(o):
    """MessageToDict base64-encodes bytes; convert trace/span ids to hex."""
    import base64
    if isinstance(o, dict):
        for k, v in list(o.items()):
            if k in ("traceId", "spanId", "parentSpanId") and isinstance(v, str):
                try:
                    o[k] = base64.b64decode(v).hex()
                except Exception:
                    pass
            else:
                o[k] = _fix_ids(v)
    elif isinstance(o, list):
        return [_fix_ids(x) for x in o]
    return o


_known_services: set = set()
_known_services_ts: float = 0

def _check_service_limit(svc_names: set):
    """No service cap any more (the free tier is limited by nodes, shown as a banner, and never refuses data)."""
    return
    if IS_PRO or not svc_names:   # noqa - kept for reference only
        return
    global _known_services, _known_services_ts
    now = time.time()
    if now - _known_services_ts > 60:
        rows = q("SELECT DISTINCT service FROM spans")
        _known_services = {r["service"] for r in rows}
        _known_services_ts = now
    new = svc_names - _known_services
    if new and len(_known_services | svc_names) > TIER_LIMITS["max_services"]:
        raise HTTPException(
            403,
            {"error": "tier_limit",
             "message": f"Community tier allows up to {TIER_LIMITS['max_services']} services. Upgrade to Pro for unlimited.",
             "limit": TIER_LIMITS["max_services"],
             "current_services": sorted(_known_services),
             "rejected_services": sorted(new),
             "tier": "community",
             "upgrade_url": PRICING_URL})
    # Remember services we just accepted, so a burst of new services inside the 60s cache window
    # cannot slip past the cap.
    _known_services = _known_services | svc_names


def ingest_traces(d):
    rows = []
    batch_services = set()
    for rs in d.get("resourceSpans", []):
        svc, res = _res(rs)
        batch_services.add(svc)
        rj = json.dumps(res)
        env, env_meta = _env_of(res); _env_seen(env, env_meta); _svc_seen(svc, env); _res_seen("spans", svc, res, env)
        for ss in rs.get("scopeSpans", []):
            for s in ss.get("spans", []):
                st, en = _ns(s.get("startTimeUnixNano")), _ns(s.get("endTimeUnixNano"))
                status = s.get("status", {})
                rows.append((s.get("traceId", ""), s.get("spanId", ""), s.get("parentSpanId", ""), svc,
                             s.get("name", ""), int(s.get("kind", 0) if not isinstance(s.get("kind"), str) else
                                                    {"SPAN_KIND_SERVER": 2, "SPAN_KIND_CLIENT": 3, "SPAN_KIND_PRODUCER": 4,
                                                     "SPAN_KIND_CONSUMER": 5, "SPAN_KIND_INTERNAL": 1}.get(s["kind"], 0)),
                             st, en, (en - st) / 1e6,
                             {"STATUS_CODE_ERROR": 2, "STATUS_CODE_OK": 1}.get(status.get("code"), status.get("code", 0))
                             if isinstance(status.get("code", 0), str) else status.get("code", 0),
                             redact(status.get("message", "")), json.dumps(redact_attrs(_kv(s.get("attributes")))), rj, env))
    _check_service_limit(batch_services)
    with db() as c:
        c.executemany("INSERT INTO spans(trace_id,span_id,parent_id,service,name,kind,start_ns,end_ns,dur_ms,status,status_msg,attrs,res,env) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        _rollup_write(c, _rollup_aggregate((r[3], r[13], r[6], r[9], r[8]) for r in rows if r[2] == "" or r[5] == 2))
        _ops = _op_aggregate((r[3], r[13], r[4], r[6], r[9], r[8]) for r in rows if r[2] == "" or r[5] in (2, 5))
        if _ops:
            c.executemany(_OP_UPSERT, _ops)
        _env_rollup_write(c, ((r[6], r[13], r[3], r[9] == 2) for r in rows), spans=True)
    _known_services.update(batch_services)
    _env_flush()
    return len(rows)


_SEV = {"TRACE": 1, "DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21}

# Container logs usually reach us with no severity (the level is just text inside the line), so every
# record would show as UNSET. Work the level out from the start of the log line instead. Only the first
# line and first ~100 characters are looked at, so a stack trace or message text that mentions "error"
# never changes the level of an INFO line.
_SEV_HEAD = re.compile(r"^[^\n]{0,100}?\b(FATAL|CRITICAL|PANIC|EMERG(?:ENCY)?|ERROR|ERR|SEVERE|WARNING|WARN|INFO|NOTICE|DEBUG|DBUG|TRACE|TRCE)\b")
_SEV_DOTNET = re.compile(r"^(crit|fail|warn|info|dbug|trce): ")
_SEV_HEAD_WORD = {"FATAL": "FATAL", "CRITICAL": "FATAL", "PANIC": "FATAL", "EMERG": "FATAL", "EMERGENCY": "FATAL",
                  "ERROR": "ERROR", "ERR": "ERROR", "SEVERE": "ERROR", "WARNING": "WARN", "WARN": "WARN",
                  "INFO": "INFO", "NOTICE": "INFO", "DEBUG": "DEBUG", "DBUG": "DEBUG", "TRACE": "TRACE", "TRCE": "TRACE"}
_SEV_DOTNET_WORD = {"crit": "FATAL", "fail": "ERROR", "warn": "WARN", "info": "INFO", "dbug": "DEBUG", "trce": "TRACE"}
# JSON or logfmt lines: {"level":"error",...}  level=warn  severity: info
_SEV_KV = re.compile(r"""(?i)\b(?:log\.?level|level|severity|lvl)["']?\s*[:=]\s*["']?(fatal|critical|panic|error|err|severe|warning|warn|notice|info|debug|trace)\b""")
_SEV_KV_WORD = {"critical": "FATAL", "panic": "FATAL", "fatal": "FATAL", "error": "ERROR", "err": "ERROR", "severe": "ERROR",
                "warning": "WARN", "warn": "WARN", "notice": "INFO", "info": "INFO", "debug": "DEBUG", "trace": "TRACE"}


# klog / glog, used by Kubernetes components and many Go programs: "E1110 09:02:43.388887 ..."
# where the first letter is the level (I, W, E, F).
_SEV_KLOG = re.compile(r"^([IWEF])\d{4} \d{2}:\d{2}:\d{2}")
_SEV_KLOG_WORD = {"I": "INFO", "W": "WARN", "E": "ERROR", "F": "FATAL"}
# A Java stack trace, or its first line, with no level word of its own. Logged almost exclusively
# on failures, so treat it as an error rather than leaving it unclassified.
_SEV_JAVA_TRACE = re.compile(r"^\s*(?:Exception in thread |Caused by: |[\w$]+(?:\.[\w$]+)*(?:Exception|Error|Throwable)\b|at [\w$.<>]+\()")
# "OpenJDK 64-Bit Server VM warning: ..." and similar lowercase tool warnings
_SEV_WARNING_WORD = re.compile(r"^[^\n]{0,60}?\bwarning:")


# Framework/agent startup chatter that carries no level word but is plainly informational: the Spring
# Boot banner (ASCII art + ":: Spring Boot ::"), JVM/agent notices, and lifecycle breadcrumbs. Marking
# these INFO (not UNSET) is what clears the bulk of "unclassified" noise on JVM services.
_SEV_INFO_NOISE = re.compile(
    r":: Spring Boot ::|^[ .|_()\\/=~^]*[.|_()\\/=][ .|_()\\/=~^']{6,}$|"   # spring banner / ascii-art lines
    r"Picked up JAVA_TOOL_OPTIONS|"                                    # JVM env notice
    r"\b(?:Started|Starting|Stopping|Started up|Tomcat (?:started|initialized)|"
    r"Initializing|Registering|Bootstrapping|Updating agent working path|"
    r"Agent Working Dir Path|MicroAI Agent not attached)\b")
# Lines that announce a failure without the word ERROR: "Error: ...", "Not Found ...", a bare
# "Exception:" / "+-> Exception:", "Caused by:", or "... failed".
_SEV_ERROR_NOISE = re.compile(
    r"^\+-> |"                                                         # Elastic APM exception detail lines
    r"^(?:\+-> )?(?:Error|Fatal|Panic)\b[: ]|"
    r"\bMissing required environment variable\b|"
    r"^(?:\+-> )?Exception\b|\bstack trace\b|"
    r"\b(?:failed to|could not|unable to)\b", re.IGNORECASE)
# librdkafka (Kafka client) log lines: "%4|1794349053.533|REQTMOUT|rdkafka#consumer-2| ..." where the digit is a
# syslog level: 0-2 critical, 3 error, 4 warning, 5-6 notice/info, 7 debug.
_SEV_RDKAFKA = re.compile(r"^%([0-7])\|")
_SEV_RDKAFKA_WORD = {"0": "FATAL", "1": "FATAL", "2": "FATAL", "3": "ERROR", "4": "WARN", "5": "INFO", "6": "INFO", "7": "DEBUG"}
# A level written in brackets with mixed case, e.g. the Elastic APM agent's "[2026-11-10 ...][5][Error] - {...}" or
# Serilog-style "[Warning]". The plain level-word rule above only accepts upper case.
_SEV_BRACKET = re.compile(r"^[^\n]{0,100}?\[(Critical|Fatal|Error|Warning|Warn|Information|Info|Debug|Trace)\]", re.IGNORECASE)
_SEV_BRACKET_WORD = {"critical": "FATAL", "fatal": "FATAL", "error": "ERROR", "warning": "WARN", "warn": "WARN",
                     "information": "INFO", "info": "INFO", "debug": "DEBUG", "trace": "TRACE"}
# Spring Security prints this (at WARN) as a multi-line message whose later lines carry no level.
_SEV_WARN_NOISE = re.compile(r"\bgenerated (?:security )?password\b", re.IGNORECASE)
# A first line with no letters or digits at all (ASCII-art banners, "=====" separators) is decoration, not a problem.
_SEV_DECOR = re.compile(r"[^A-Za-z0-9]{6,}")
_SEV_HTTP = re.compile(r'"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) [^"]+" (\d{3})\b|\bstatus(?:_code)?[=:" ]+(\d{3})\b|\bHTTP/\d\.\d" (\d{3})\b')


def _severity_from_status(code):
    if 500 <= code <= 599: return _SEV["ERROR"], "ERROR"
    if 400 <= code <= 499: return _SEV["WARN"], "WARN"
    if 100 <= code <= 399: return _SEV["INFO"], "INFO"
    return 0, ""


def _infer_severity(body):
    """Best-effort (severity_number, severity_text) from the log text; (0, "") when unsure."""
    if not isinstance(body, str) or not body:
        return 0, ""
    head = body[:300]
    m = _SEV_KLOG.match(head)
    if m:
        t = _SEV_KLOG_WORD[m.group(1)]
        return _SEV[t], t
    m = _SEV_RDKAFKA.match(head)
    if m:
        t = _SEV_RDKAFKA_WORD[m.group(1)]
        return _SEV[t], t
    if head.lstrip()[:1] == "{":  # structured JSON line: trust its level field first
        m = _SEV_KV.search(head)
        if m:
            t = _SEV_KV_WORD[m.group(1).lower()]
            return _SEV[t], t
    m = _SEV_DOTNET.match(head)  # ASP.NET console format: "fail: Microsoft..."
    if m:
        t = _SEV_DOTNET_WORD[m.group(1)]
        return _SEV[t], t
    m = _SEV_HEAD.search(head)  # earliest level word in the header wins
    if m:
        t = _SEV_HEAD_WORD[m.group(1)]
        return _SEV[t], t
    m = _SEV_KV.search(head.split("\n", 1)[0])
    if m:
        t = _SEV_KV_WORD[m.group(1).lower()]
        return _SEV[t], t
    m = _SEV_BRACKET.match(head)
    if m:
        t = _SEV_BRACKET_WORD[m.group(1).lower()]
        return _SEV[t], t
    if _SEV_JAVA_TRACE.match(head):
        return _SEV["ERROR"], "ERROR"
    if _SEV_WARNING_WORD.match(head):
        return _SEV["WARN"], "WARN"
    if _SEV_WARN_NOISE.search(head):
        return _SEV["WARN"], "WARN"
    if _SEV_DECOR.fullmatch(head.split("\n", 1)[0].strip()):
        return _SEV["INFO"], "INFO"
    if _SEV_INFO_NOISE.search(head):
        return _SEV["INFO"], "INFO"
    if _SEV_ERROR_NOISE.search(head):
        return _SEV["ERROR"], "ERROR"
    m = _SEV_HTTP.search(head)
    if m:
        code = int(next(g for g in m.groups() if g))
        isev, itext = _severity_from_status(code)
        if isev:
            return isev, itext
    return 0, ""


_below_min_dropped = [0]  # log records discarded by NEBULENS_MIN_LOG_SEVERITY, reported by /api/redaction


def _below_min_severity(sev):
    """True when a record with this severity_number must not be stored under NEBULENS_MIN_LOG_SEVERITY."""
    sev = int(sev or 0)
    if sev == 0:
        return LOG_UNKNOWN_SEVERITY != "keep"
    return sev < MIN_LOG_SEVERITY


def ingest_logs(d):
    rows = []
    for rl in d.get("resourceLogs", []):
        svc, res = _res(rl)
        if svc == "unknown" and res.get("k8s.container.name"):
            svc = res["k8s.container.name"]  # filelog container logs carry no service.name
        rj = json.dumps(res)
        env, env_meta = _env_of(res); _env_seen(env, env_meta); _svc_seen(svc, env); _res_seen("logs", svc, res, env)
        for sl in rl.get("scopeLogs", []):
            for l in sl.get("logRecords", []):
                sev = l.get("severityNumber", 0)
                if isinstance(sev, str):
                    sev = _SEV.get(sev.replace("SEVERITY_NUMBER_", "").split("_")[0], 0)
                body = _val(l.get("body", {}))
                if isinstance(body, str) and not body.strip():
                    _blank_dropped[0] += 1   # blank lines carry no information
                    continue
                if isinstance(body, str):
                    body = redact(body)      # before storage: the raw value never reaches the database
                sev_text = l.get("severityText", "")
                attrs = redact_attrs(_kv(l.get("attributes")))
                if not int(sev or 0):  # no level from the source: read it from the text
                    isev, itext = _infer_severity(body)
                    if isev:
                        sev, sev_text = isev, sev_text or itext
                        attrs["nebulens.severity.inferred"] = True
                if MIN_LOG_SEVERITY and _below_min_severity(sev):
                    _below_min_dropped[0] += 1   # not stored, and not reported as rejected (exporters must not retry)
                    continue
                rows.append((_ns(l.get("timeUnixNano") or l.get("observedTimeUnixNano")) or time.time_ns(), svc, int(sev),
                             sev_text, body if isinstance(body, str) else json.dumps(body),
                             l.get("traceId", ""), l.get("spanId", ""), json.dumps(attrs), rj, env))
    with db() as c:
        c.executemany("INSERT INTO logs(ts_ns,service,severity,sev_text,body,trace_id,span_id,attrs,res,env) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        _env_rollup_write(c, ((r[0], r[9], r[1], r[2] >= 17) for r in rows), spans=False)
    _env_flush()
    return len(rows)


def ingest_metrics(d):
    rows = []
    for rm in d.get("resourceMetrics", []):
        svc, res = _res(rm)
        rj = json.dumps(res)
        env, env_meta = _env_of(res); _env_seen(env, env_meta); _svc_seen(svc, env); _res_seen("metrics", svc, res, env)
        for sm in rm.get("scopeMetrics", []):
            for m in sm.get("metrics", []):
                name, unit = m.get("name", ""), m.get("unit", "")
                for mtype in ("gauge", "sum", "histogram", "exponentialHistogram", "summary"):
                    if mtype not in m:
                        continue
                    for p in m[mtype].get("dataPoints", []):
                        if mtype in ("histogram", "exponentialHistogram", "summary"):
                            cnt = float(p.get("count", 0) or 0)
                            tot = float(p.get("sum", 0) or 0)
                            val = tot / cnt if cnt else 0.0  # mean of the interval
                        else:
                            val = float(p["asDouble"]) if "asDouble" in p else float(p.get("asInt", 0))
                        rows.append((_ns(p.get("timeUnixNano")) or time.time_ns(), svc, name, unit, mtype, val,
                                     json.dumps(redact_attrs(_kv(p.get("attributes")))), rj, env))
    seen = collections.Counter()
    for r in rows:  # (ts_ns, service, name, unit, mtype, ...)
        seen[(r[2], r[3], r[4], r[1] or "", int(r[0] // 3_600_000_000_000))] += 1
    with db() as c:
        c.executemany("INSERT INTO metrics(ts_ns,service,name,unit,mtype,value,attrs,res,env) VALUES(?,?,?,?,?,?,?,?,?)", rows)
        if seen:
            c.executemany("""INSERT INTO metric_names_seen(name,unit,mtype,service,hr,points) VALUES(?,?,?,?,?,?)
                             ON CONFLICT(name,unit,mtype,service,hr)
                             DO UPDATE SET points = metric_names_seen.points + excluded.points""",
                          [(*k, n) for k, n in seen.items()])
    _env_flush()
    return len(rows)


# ---------------------------------------------------------------- validation models
SIGNALS = {"error_rate", "p95_latency", "request_rate", "log_errors", "metric_avg", "metric_max", "synthetic_down"}
OPS_SET = {">", ">=", "<", "<="}


class AlertRuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    signal: Literal["error_rate", "p95_latency", "request_rate", "log_errors", "metric_avg", "metric_max", "synthetic_down"]
    expr: str = Field(default="", max_length=512)
    op: Literal[">", ">=", "<", "<="]
    threshold: float = Field(ge=-1e12, le=1e12)
    window_s: int = Field(default=300, ge=15, le=86400)
    service: str = Field(default="", max_length=128)
    webhook: str = Field(default="", max_length=2048)

    @field_validator("webhook")
    @classmethod
    def _wh(cls, v):
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("webhook must be http(s) URL")
        return v


class SyntheticIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=8, max_length=2048)
    interval_s: int = Field(default=60, ge=10, le=3600)
    expect_status: int = Field(default=200, ge=100, le=599)
    max_ms: float = Field(default=2000, ge=1, le=60000)

    @field_validator("url")
    @classmethod
    def _u(cls, v):
        if not v.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class DashboardIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    panels: list = Field(default_factory=list, max_length=50)


class SloIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    service: str = Field(min_length=1, max_length=128)
    objective_pct: float = Field(ge=50.0, le=100.0)
    latency_ms: float = Field(ge=1, le=60000)
    window_days: int = Field(default=30, ge=1, le=90)


# ---------------------------------------------------------------- app
from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app):
    _start()      # defined below; resolved when the app starts
    yield


app = FastAPI(title="NebuLens", docs_url="/api/docs", redoc_url=None, lifespan=_lifespan)


@app.middleware("http")
async def request_id(request: Request, call_next):
    """Attach a stable request id to every request + response.
    Must NOT catch exceptions - FastAPI's own handlers (validation, HTTPException)
    need to run first to produce proper 4xx responses."""
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request.state.rid = rid
    if request.method == "GET" and request.url.path.startswith("/api/"):
        _q_limited.set(True)
    resp = await call_next(request)
    resp.headers["x-request-id"] = rid
    return resp


@app.exception_handler(RequestValidationError)
async def _vexc(request: Request, exc: RequestValidationError):
    # Pydantic puts raw Exception objects in err["ctx"]; stringify so JSON works. When the body couldn't be
    # parsed as JSON at all (e.g. a non-JSON Content-Type, or no Content-Type), err["input"] is the raw request
    # body as bytes -- also not JSON-serializable, which crashed this handler itself with a 500 (found live,
    # via /signup and /login: unauthenticated public endpoints, so anyone could trigger it with no credentials).
    details = []
    for err in exc.errors():
        clean = {k: v for k, v in err.items() if k != "ctx"}
        if "ctx" in err:
            clean["ctx"] = {k: str(v) for k, v in err["ctx"].items()}
        if isinstance(clean.get("input"), (bytes, bytearray)):
            clean["input"] = clean["input"].decode("utf-8", "replace")
        details.append(clean)
    return JSONResponse({"error": "validation_failed", "details": details,
                         "request_id": getattr(request.state, "rid", "")}, status_code=400)


def _is_query_timeout(exc):
    m = str(exc).lower()
    return "interrupted" in m or "statement timeout" in m or "canceling statement" in m


@app.exception_handler(sqlite3.OperationalError)
async def _qtimeout(request: Request, exc: Exception):
    if _is_query_timeout(exc):
        return JSONResponse({"error": "query_timeout", "status": 503, "request_id": getattr(request.state, "rid", ""),
                             "message": f"This view took longer than {QUERY_TIMEOUT:.0f} s and was stopped. Try a shorter time range."},
                            status_code=503)
    raise exc


if USE_PG:
    # Postgres cancels a timed-out statement with its own exception type (not sqlite3.OperationalError)
    app.add_exception_handler(psycopg.errors.QueryCanceled, _qtimeout)


@app.exception_handler(HTTPException)
async def _hexc(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        body = {**exc.detail, "status": exc.status_code, "request_id": getattr(request.state, "rid", "")}
    else:
        body = {"error": "http_error", "message": exc.detail, "status": exc.status_code, "request_id": getattr(request.state, "rid", "")}
    return JSONResponse(body, status_code=exc.status_code)

from fastapi.middleware.cors import CORSMiddleware
# Browser RUM (Angular/React/Vue) posts OTLP straight to /v1/* from any origin. navigator.sendBeacon always
# sends credentialed requests and browsers reject a wildcard Allow-Origin for those, so the origin is echoed.
# That is only safe for the ingest endpoints, which authenticate with a bearer/ingest key header. It must NOT
# apply to /api/*: SSO sessions are cookies, so echoing any origin there would let a page on another
# (same-site) host read data with a signed-in user's session. /api/* is therefore same-origin only.
class _IngestOnlyCORS(CORSMiddleware):
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/v1/"):
            await super().__call__(scope, receive, send)
        else:
            await self.app(scope, receive, send)


app.add_middleware(_IngestOnlyCORS, allow_origin_regex=".*", allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"], expose_headers=["*"])

# Baseline response headers. A Content-Security-Policy is deliberately not set here: the UI builds styles inline,
# so a policy strict enough to matter needs testing in a real browser first. Set NEBULENS_ALLOW_FRAMING=1 only if
# you embed NebuLens in another page.
_ALLOW_FRAMING = os.environ.get("NEBULENS_ALLOW_FRAMING", "").strip() == "1"


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    if not _ALLOW_FRAMING:
        h.setdefault("X-Frame-Options", "DENY")
    https = request.url.scheme == "https" or (os.environ.get("NEBULENS_TRUST_PROXY") == "1"
                                              and request.headers.get("x-forwarded-proto", "") == "https")
    if https:
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


# ---------------------------------------------------------------- Kubernetes monitoring (Dash0-style UX)
# These endpoints power the dedicated Kubernetes page: per-namespace detection, per-workload instrumentation
# status, and a readiness check for the setup wizard. They read the res column via _as_dict / _jload so the
# same code path works on SQLite (TEXT) and Postgres (JSONB).


def _res_ready(minutes):
    """True when resource_seen covers a window this long (and it is long enough to be worth reading from there)."""
    if minutes < _ROLLUP_MIN_WINDOW:
        return False
    try:
        r = q("SELECT value FROM rollup_state WHERE key='res_from'")
        return bool(r) and (time.time_ns() - int(minutes * 60e9)) // _MIN_NS >= int(r[0]["value"])
    except Exception:
        return False


def _iter_resources(minutes, env=""):
    """Yield (signal, service, attributes) for the resources that sent telemetry in the last `minutes` minutes.
    Served from the small resource_seen registry when it covers the window; otherwise falls back to sampling raw rows
    (20k per table, so a huge database still answers, though only from a sample)."""
    if _res_ready(minutes):
        sql = ("SELECT sig, service, cluster, ns, node, pod, deployment, lang, sdk_version, svc_version FROM resource_seen "
               "WHERE last_seen>=?" + (" AND env=?" if env else ""))
        for sig, svc, cl, ns, node, pod, dep, lang, sdkv, sv in qrows(sql, (int(time.time() - minutes * 60), *((env,) if env else ()))):
            a = {k: v for k, v in (("k8s.cluster.name", cl), ("k8s.namespace.name", ns), ("k8s.node.name", node), ("k8s.pod.name", pod),
                                   ("k8s.deployment.name", dep), ("telemetry.sdk.language", lang), ("telemetry.sdk.version", sdkv),
                                   ("service.version", sv)) if v}
            yield sig, svc, a
        return
    t0 = since_ns(minutes)
    for table, tcol in (("spans", "start_ns"), ("logs", "ts_ns"), ("metrics", "ts_ns")):
        where, args = (f"{tcol}>=? AND env=?", (t0, env)) if env else (f"{tcol}>=?", (t0,))
        for r in q(f"SELECT service, res FROM {table} WHERE {where} LIMIT 20000", args):
            d = _as_dict(r["res"])
            # OTLP stores attributes under "attributes" when the row was ingested with the proto shape; be tolerant.
            a = d.get("attributes", d) if isinstance(d, dict) else {}
            if not isinstance(a, dict):
                continue
            yield table, r["service"], a


@app.get("/api/kubernetes/overview")
@ttl_cached
def k8s_overview(minutes: float = 60, cluster: str = ""):
    """Headline numbers for the Kubernetes page: nodes seen, namespaces active, workloads instrumented."""
    nodes, namespaces, clusters = set(), set(), set()
    workloads = set()
    instrumented = set()
    languages = collections.Counter()
    signals = collections.Counter()
    for table, svc, a in _iter_resources(minutes, cluster):
        if a.get("k8s.node.name"): nodes.add(a["k8s.node.name"])
        ns = a.get("k8s.namespace.name") or a.get("service.namespace")
        if ns: namespaces.add(ns)
        if a.get("k8s.cluster.name"): clusters.add(a["k8s.cluster.name"])
        if svc and ns: workloads.add((svc, ns))
        sdk = a.get("telemetry.sdk.language")
        if sdk:
            languages[sdk] += 1
            if svc and ns:
                instrumented.add((svc, ns))
        signals[table] += 1
    return {
        "clusters": sorted(clusters), "nodes": len(nodes), "namespaces": len(namespaces),
        "workloads": len(workloads), "instrumented_workloads": len(instrumented),
        "languages": dict(languages), "signals": dict(signals),
        "window_minutes": minutes,
    }


@app.get("/api/kubernetes/namespaces")
@ttl_cached
def k8s_namespaces(minutes: float = 60, cluster: str = ""):
    """Per-namespace: how many services, how many workloads instrumented (telemetry.sdk.language seen),
    and which signals (spans/logs/metrics) are flowing. Order: most services first."""
    per_ns = {}
    for table, svc, a in _iter_resources(minutes, cluster):
        ns = a.get("k8s.namespace.name") or a.get("service.namespace") or ""
        if not ns:
            continue
        info = per_ns.setdefault(ns, {"namespace": ns, "services": set(), "instrumented": set(),
                                      "languages": collections.Counter(), "signals": collections.Counter(),
                                      "cluster": a.get("k8s.cluster.name", "")})
        if svc:
            info["services"].add(svc)
            if a.get("telemetry.sdk.language"):
                info["instrumented"].add(svc)
                info["languages"][a["telemetry.sdk.language"]] += 1
        info["signals"][table] += 1
    out = []
    for ns, info in per_ns.items():
        out.append({"namespace": ns, "cluster": info["cluster"],
                    "services": len(info["services"]), "instrumented": len(info["instrumented"]),
                    "languages": dict(info["languages"]), "signals": dict(info["signals"]),
                    "coverage_pct": round(100 * len(info["instrumented"]) / max(1, len(info["services"])), 0)})
    return sorted(out, key=lambda x: -x["services"])


@app.get("/api/kubernetes/workloads")
@ttl_cached
def k8s_workloads(minutes: float = 60, namespace: str = "", cluster: str = ""):
    """Per-workload (service + namespace) with SDK language/version and whether each signal type was seen.
    If `namespace` is given, only that namespace is returned."""
    per_wl = {}
    for table, svc, a in _iter_resources(minutes, cluster):
        ns = a.get("k8s.namespace.name") or a.get("service.namespace") or ""
        if not svc:
            continue
        if namespace and ns != namespace:
            continue
        key = (svc, ns)
        wl = per_wl.setdefault(key, {"service": svc, "namespace": ns,
                                     "language": "", "sdk_version": "", "version": "",
                                     "deployment": "", "pods": set(), "node": "",
                                     "signals": set()})
        wl["signals"].add(table)
        if a.get("telemetry.sdk.language"): wl["language"] = a["telemetry.sdk.language"]
        if a.get("telemetry.sdk.version"): wl["sdk_version"] = a["telemetry.sdk.version"]
        if a.get("service.version"): wl["version"] = a["service.version"]
        if a.get("k8s.deployment.name"): wl["deployment"] = a["k8s.deployment.name"]
        if a.get("k8s.pod.name"): wl["pods"].add(a["k8s.pod.name"])
        if a.get("k8s.node.name"): wl["node"] = a["k8s.node.name"]
    out = []
    for wl in per_wl.values():
        out.append({**wl, "pods": sorted(wl["pods"])[:10], "pod_count": len(wl["pods"]),
                    "signals": sorted(wl["signals"]), "instrumented": bool(wl["language"])})
    return sorted(out, key=lambda x: (not x["instrumented"], x["namespace"], x["service"]))


@app.get("/api/kubernetes/wizard")
def k8s_wizard():
    """Status of each setup step for the onboarding wizard. Progress is detected from real telemetry,
    so a step turns green exactly when NebuLens starts seeing the data that step is supposed to produce.
    No guessing, no cluster API calls, no SSH — just: do we see what we expect to see?"""
    # Step 1: operator installed (defined as "we see telemetry at all in the observability namespace,
    # or a cluster-collector service.name that matches ours"). A newly-installed cluster passes this when
    # the NebuLens collector itself sends its first metric.
    ov = k8s_overview(minutes=60)
    step1 = ov["nodes"] > 0 or ov["namespaces"] > 0
    # Step 2: at least one non-system namespace has signals (user's own workloads reaching the collector).
    SYSTEM_NS = {"kube-system", "kube-public", "kube-node-lease", "observability", "nebulens", "dash0-system"}
    user_ns = [n for n in k8s_namespaces(minutes=60) if n["namespace"] not in SYSTEM_NS]
    step2 = len(user_ns) > 0
    # Step 3: at least one workload has telemetry.sdk.language set (i.e. agent-injected or SDK-instrumented).
    step3 = ov["instrumented_workloads"] > 0
    # Step 4: traces are flowing (not just logs or metrics), which confirms agent injection worked end-to-end.
    step4 = ov["signals"].get("spans", 0) > 0

    return {
        "steps": [
            {"id": "install", "title": "NebuLens collectors reporting",
             "description": "The bundled OpenTelemetry Collector DaemonSet forwards container logs plus node and Kubernetes metrics. Nothing to install here; this turns green when the collectors are sending.",
             "ok": step1, "evidence": f"{ov['nodes']} node{'s' if ov['nodes'] != 1 else ''} reporting"},
            {"id": "monitor-namespace", "title": "Workloads sending telemetry",
             "description": "Any app outside the system namespaces that sends OTLP to NebuLens or its collector shows up here. Existing SDKs only need the endpoint changed.",
             "ok": step2, "evidence": f"{len(user_ns)} user namespace{'s' if len(user_ns) != 1 else ''} with telemetry"},
            {"id": "instrument-workload", "title": "Instrument a workload (zero-code)",
             "description": "Enable the OpenTelemetry Operator once, then run scripts/instrument.sh for a Deployment. The pod restarts once with the language agent attached; no image rebuild.",
             "ok": step3, "evidence": f"{ov['instrumented_workloads']} instrumented workload{'s' if ov['instrumented_workloads'] != 1 else ''}"},
            {"id": "see-traces", "title": "See traces end-to-end",
             "description": "Once the agent is attached and your app serves a request, spans arrive at NebuLens and show up on the Traces page.",
             "ok": step4, "evidence": "traces arriving" if step4 else "no traces yet"},
        ],
        "complete": step1 and step2 and step3 and step4,
    }


# ---------------------------------------------------------------- Integrations catalog
import integrations as _ints


@ttl_cached
def _integration_facts(minutes=60):
    """What integration detection needs, read once from the small registries written at ingest (metric names per hour,
    one row per distinct resource) instead of scanning raw spans, logs and metrics once per integration. On a large or
    network-mounted database the old scans made the Integrations page time out."""
    _res_flush()   # resource rows are batched once a minute; include what arrived since
    hr0 = int(since_ns(minutes) // 3_600_000_000_000)
    names = {r[0] for r in qrows("SELECT DISTINCT name FROM metric_names_seen WHERE hr>=?", (hr0,))}
    langs, k8s = set(), False
    for lang, pod, node in qrows("SELECT DISTINCT lang, pod, node FROM resource_seen WHERE last_seen>=?",
                                 (int(time.time() - minutes * 60),)):
        if lang:
            langs.add(str(lang).lower())
        if pod or node:
            k8s = True
    return {"metric_names": names, "langs": langs, "k8s": k8s, "minutes": minutes}


def _recent_attr_keys(facts):
    """Attribute keys in a small sample of the newest rows (record and resource attributes), read at most once per request."""
    if "attr_keys" not in facts:
        keys, t0 = set(), since_ns(facts["minutes"])
        for table, tcol in (("logs", "ts_ns"), ("spans", "start_ns"), ("metrics", "ts_ns")):
            for r in q(f"SELECT attrs, res FROM {table} WHERE {tcol}>=? ORDER BY {tcol} DESC LIMIT 200", (t0,)):
                keys.update(_as_dict(r["attrs"]))
                res = _as_dict(r["res"])
                keys.update(res.get("attributes") if isinstance(res.get("attributes"), dict) else res)
        facts["attr_keys"] = keys
    return facts["attr_keys"]


def _detect_integration(integ, minutes=60, facts=None):
    """Return ("active" | "no_data", details) for one integration based on recent telemetry."""
    f = facts or _integration_facts(minutes)
    d = integ["detect"]
    seen = {"attributes": [], "metric_names": sorted(set(d.get("metric_names") or []) & f["metric_names"])}
    langs = set(d.get("languages") or [])
    if langs:                                    # a language integration: that language must actually be reporting
        seen["attributes"] = ["telemetry.sdk.language=" + x for x in sorted(langs & f["langs"])]
    elif d.get("kubernetes"):                    # data from pods or nodes
        seen["attributes"] = list(d.get("attributes") or []) if f["k8s"] else []
    elif d.get("attributes"):
        seen["attributes"] = sorted(set(d["attributes"]) & _recent_attr_keys(f))
    is_active = bool(seen["attributes"] or seen["metric_names"])
    return ("active" if is_active else "no_data"), seen


def _install_record(iid):
    rows = q("SELECT installed_at, dashboard_ids, view_ids, alert_ids FROM integration_installs WHERE integration_id=?",
             (iid,))
    if not rows:
        return None
    r = rows[0]
    return {"installed_at": r["installed_at"], "dashboard_ids": _as_list(r["dashboard_ids"]),
            "view_ids": _as_list(r["view_ids"]), "alert_ids": _as_list(r["alert_ids"])}


@app.get("/api/integrations")
def integrations_list(minutes: float = 60):
    """Catalogue of all integrations with install state and detection status."""
    out = []
    facts = _integration_facts(minutes)
    # Build a map of alert templates for fast lookup.
    tpl_by_id = {t["id"]: t for t in ALERT_TEMPLATES}
    for integ in _ints.INTEGRATIONS:
        installed = _install_record(integ["id"])
        state, _ = _detect_integration(integ, minutes=minutes, facts=facts)
        out.append({
            "id": integ["id"], "name": integ["name"], "icon": integ["icon"],
            "category": integ["category"], "description": integ["description"],
            "setup": integ["setup"],
            "installed": installed is not None, "installed_at": installed["installed_at"] if installed else None,
            "state": state,  # "active" | "no_data"
            "counts": {"dashboards": len(integ["dashboards"]), "alerts": len(integ["alerts"]),
                       "views": len(integ["views"])},
        })
    return out


@app.get("/api/integrations/{iid}")
def integrations_get(iid: str):
    integ = _ints.by_id(iid)
    if not integ:
        raise HTTPException(404, "unknown integration")
    installed = _install_record(iid)
    state, detected = _detect_integration(integ, minutes=60)
    tpl_by_id = {t["id"]: t for t in ALERT_TEMPLATES}
    return {
        "id": integ["id"], "name": integ["name"], "icon": integ["icon"],
        "category": integ["category"], "description": integ["description"],
        "setup": integ["setup"], "setup_blocks": integ["setup_blocks"],
        "dashboards": integ["dashboards"], "views": integ["views"],
        "alerts": [tpl_by_id[a] for a in integ["alerts"] if a in tpl_by_id],
        "detect": integ["detect"],
        "installed": installed, "state": state, "detected": detected,
    }


def _slugify(text, existing):
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "view"
    slug, n = base, 1
    while slug in existing:
        n += 1; slug = f"{base}-{n}"
    return slug


@app.post("/api/integrations/{iid}/install")
def integrations_install(iid: str):
    """Install an integration's dashboards and views. Idempotent: re-installing is a no-op."""
    integ = _ints.by_id(iid)
    if not integ:
        raise HTTPException(404, "unknown integration")
    if IS_PRO:
        _tier_check("integrations", "dashboards")
    existing = _install_record(iid)
    if existing:
        return {"ok": True, "already_installed": True, **existing}

    dashboard_ids, view_ids = [], []
    now = int(time.time())
    with db() as c:
        # Dashboards: name-unique, upsert to get the id.
        for d in integ["dashboards"]:
            name = d["name"]
            c.execute("""INSERT INTO dashboards(name, panels, updated) VALUES(?,?,?)
                         ON CONFLICT(name) DO UPDATE SET panels=excluded.panels, updated=excluded.updated""",
                      (name, json.dumps(d["panels"]), now))
            row = c.execute("SELECT id FROM dashboards WHERE name=?", (name,)).fetchone()
            if row is not None:
                dashboard_ids.append(row[0] if isinstance(row, tuple) else row["id"])
        # Views: slug-unique; they already have a tombstone so a user-hidden one stays hidden.
        # The write cursor returns tuples on Postgres (unlike the q() dict-row helper), so index by position.
        all_view_slugs = {r[0] for r in c.execute("SELECT slug FROM views").fetchall()}
        tombstones = {r[0] for r in c.execute("SELECT slug FROM view_tombstones").fetchall()}
        for v in integ["views"]:
            if v["slug"] in tombstones:
                continue
            slug = v["slug"] if v["slug"] not in all_view_slugs else _slugify(v["name"], all_view_slugs)
            c.execute("""INSERT INTO views(slug, name, kind, params, description, builtin, updated) VALUES(?,?,?,?,?,0,?)
                         ON CONFLICT(slug) DO NOTHING""",
                      (slug, v["name"], v["kind"], json.dumps(v["params"]), v.get("description", ""), now))
            row = c.execute("SELECT id FROM views WHERE slug=?", (slug,)).fetchone()
            if row is not None:
                view_ids.append(row[0] if isinstance(row, tuple) else row["id"])
                all_view_slugs.add(slug)
        c.execute("""INSERT INTO integration_installs(integration_id, installed_at, dashboard_ids, view_ids, alert_ids)
                     VALUES(?,?,?,?,?)
                     ON CONFLICT(integration_id) DO UPDATE SET installed_at=excluded.installed_at,
                       dashboard_ids=excluded.dashboard_ids, view_ids=excluded.view_ids, alert_ids=excluded.alert_ids""",
                  (iid, now, json.dumps(dashboard_ids), json.dumps(view_ids), json.dumps(integ["alerts"])))
    return {"ok": True, "installed_at": now, "dashboard_ids": dashboard_ids, "view_ids": view_ids, "alert_ids": integ["alerts"]}


@app.post("/api/integrations/{iid}/uninstall")
def integrations_uninstall(iid: str):
    """Remove whatever this integration installed. Leaves user-made modifications alone."""
    existing = _install_record(iid)
    if not existing:
        return {"ok": True, "was_installed": False}
    with db() as c:
        for did in existing["dashboard_ids"]:
            c.execute("DELETE FROM dashboards WHERE id=?", (did,))
        for vid in existing["view_ids"]:
            c.execute("DELETE FROM views WHERE id=?", (vid,))
        c.execute("DELETE FROM integration_installs WHERE integration_id=?", (iid,))
    return {"ok": True, "removed": {"dashboards": len(existing["dashboard_ids"]), "views": len(existing["view_ids"])}}


# ---------------------------------------------------------------- PromQL (subset)
# A small, honest subset of PromQL, enough that Grafana dashboards, PromQL alerts and curl can query NebuLens's
# metrics without a translation layer. The real PromQL spec is bigger than this; what is supported:
#   selector            m or m{label="v", other=~"regex"}   (operators: =, !=, =~, !~)
#   range selector      m[5m]                               (seconds: s, minutes: m, hours: h, days: d)
#   functions           rate(m[5m])   deriv(m[5m])          (on counters-of-time; see below)
#   aggregations        sum|avg|min|max|count (expr) by (service, env) | without (...)
#   binary ops          + - * /      (vector-scalar, scalar-scalar, and vector-vector matched by shared labels)
#   comparisons         == != > >= < <=   (used for alert rules)
# NOT supported in this version:
#   histogram_quantile, topk/bottomk, unary -, nested range vectors, ignoring/on/group_left/right, @ modifier,
#   subqueries, string functions, absent(). Return type matches Prometheus: {data:{resultType, result:[...]}}.


class _PQ:
    __slots__ = ("src", "i")
    def __init__(self, src): self.src = src; self.i = 0
    def peek(self): return self.src[self.i] if self.i < len(self.src) else ""
    def eof(self): return self.i >= len(self.src)
    def skip_ws(self):
        while self.i < len(self.src) and self.src[self.i].isspace():
            self.i += 1
    def match(self, s):
        self.skip_ws()
        if self.src.startswith(s, self.i):
            self.i += len(s); return True
        return False
    def expect(self, s):
        if not self.match(s):
            raise ValueError(f"expected {s!r} at offset {self.i}")
    def ident(self):
        self.skip_ws(); start = self.i
        while self.i < len(self.src) and (self.src[self.i].isalnum() or self.src[self.i] in "_:."):
            self.i += 1
        return self.src[start:self.i] if self.i > start else ""
    def string(self):
        self.skip_ws()
        if self.peek() not in ("'", '"'):
            raise ValueError("expected string")
        q = self.src[self.i]; self.i += 1; start = self.i
        while self.i < len(self.src) and self.src[self.i] != q:
            if self.src[self.i] == "\\" and self.i + 1 < len(self.src):
                self.i += 2
            else:
                self.i += 1
        out = self.src[start:self.i].encode().decode("unicode_escape")
        self.i += 1  # closing quote
        return out


def _parse_duration(s):
    """5m -> 300, 2h30m -> 9000. Returns seconds."""
    m = re.fullmatch(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", s or "")
    if not m or s == "":
        raise ValueError(f"bad duration {s!r}")
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


_PROMQL_AGGS = {"sum", "avg", "min", "max", "count"}
_PROMQL_FNS = {"rate", "deriv"}


def _promql_parse(src):
    """Parse one PromQL expression into a small AST tuple tree:
    ("num", float)
    ("vec", name, [(op, label, value), ...], range_seconds_or_None)
    ("agg", op, child, by_or_without, [labels])
    ("fn", name, child)
    ("bin", op, lhs, rhs)
    ("cmp", op, lhs, rhs)
    """
    pq = _PQ(src)
    tree = _parse_expr(pq, 0)
    pq.skip_ws()
    if not pq.eof():
        raise ValueError(f"trailing text at offset {pq.i}: {pq.src[pq.i:pq.i+20]!r}")
    return tree


# Pratt parser: lowest precedence first.
_PREC = {"or": 1, "and": 2, "==": 3, "!=": 3, ">": 3, ">=": 3, "<": 3, "<=": 3,
         "+": 4, "-": 4, "*": 5, "/": 5, "%": 5}


def _parse_expr(pq, min_prec):
    left = _parse_atom(pq)
    while True:
        pq.skip_ws()
        op = _peek_bin(pq)
        if not op or _PREC[op] < min_prec:
            break
        pq.i += len(op)
        right = _parse_expr(pq, _PREC[op] + 1)
        node_kind = "cmp" if op in ("==", "!=", ">", ">=", "<", "<=") else "bin"
        left = (node_kind, op, left, right)
    return left


def _peek_bin(pq):
    for op in ("==", "!=", ">=", "<=", ">", "<", "+", "-", "*", "/", "%"):
        if pq.src.startswith(op, pq.i):
            return op
    return None


def _parse_atom(pq):
    pq.skip_ws()
    if pq.match("("):
        inner = _parse_expr(pq, 0); pq.expect(")"); return inner
    # number
    m = re.match(r"-?\d+(?:\.\d+)?(?:e-?\d+)?", pq.src[pq.i:])
    if m and not pq.src[pq.i:pq.i + len(m.group()) + 1].rstrip().startswith(tuple("_abcdefghijklmnopqrstuvwxyz")):
        pq.i += len(m.group()); return ("num", float(m.group()))
    name = pq.ident()
    if not name:
        raise ValueError(f"expected an expression at offset {pq.i}")
    # aggregation: sum|avg|... [by|without (labels)] ( expr )
    if name in _PROMQL_AGGS:
        grouping = (None, [])
        pq.skip_ws()
        if pq.match("by"):
            labels = _parse_label_list(pq); grouping = ("by", labels)
        elif pq.match("without"):
            labels = _parse_label_list(pq); grouping = ("without", labels)
        pq.expect("(")
        child = _parse_expr(pq, 0)
        pq.expect(")")
        if grouping[0] is None:
            pq.skip_ws()
            if pq.match("by"):
                grouping = ("by", _parse_label_list(pq))
            elif pq.match("without"):
                grouping = ("without", _parse_label_list(pq))
        return ("agg", name, child, grouping[0], grouping[1])
    # function: rate|deriv ( expr )
    if name in _PROMQL_FNS:
        pq.expect("(")
        child = _parse_expr(pq, 0)
        pq.expect(")")
        return ("fn", name, child)
    # selector: name {matchers}? [range]?
    matchers = []
    if pq.match("{"):
        pq.skip_ws()
        if not pq.match("}"):
            while True:
                lbl = pq.ident()
                if not lbl:
                    raise ValueError("expected label name")
                pq.skip_ws()
                op = None
                for o in ("=~", "!~", "!=", "="):
                    if pq.src.startswith(o, pq.i):
                        pq.i += len(o); op = o; break
                if op is None:
                    raise ValueError("expected =, !=, =~ or !~")
                val = pq.string()
                matchers.append((op, lbl, val))
                pq.skip_ws()
                if pq.match(","):
                    continue
                pq.expect("}"); break
    rng = None
    if pq.match("["):
        start = pq.i
        while pq.i < len(pq.src) and pq.src[pq.i] != "]":
            pq.i += 1
        rng_text = pq.src[start:pq.i]
        pq.expect("]")
        rng = _parse_duration(rng_text)
    return ("vec", name, matchers, rng)


def _parse_label_list(pq):
    pq.expect("(")
    out = []
    pq.skip_ws()
    if pq.match(")"):
        return out
    while True:
        name = pq.ident()
        if not name:
            raise ValueError("expected label name")
        out.append(name); pq.skip_ws()
        if pq.match(","):
            continue
        pq.expect(")"); return out


def _promql_fetch_vector(name, matchers, t0_ns, t1_ns):
    """Pull every point for this (name, matchers) between t0 and t1, group by (service, env, attrs-as-labels)."""
    rows = q(f"SELECT ts_ns, service, env, value, attrs FROM metrics WHERE name=? AND ts_ns>=? AND ts_ns<=?",
             (name, t0_ns, t1_ns))
    series = {}
    for r in rows:
        try:
            attrs = _as_dict(r["attrs"])
        except Exception:
            attrs = {}
        labels = {"__name__": name, "service": r["service"] or "", "env": r["env"] or ""}
        for k, v in attrs.items():
            if isinstance(v, (str, int, float, bool)):
                labels[k] = str(v)
        if not _promql_match(labels, matchers):
            continue
        # key by the full label set, so two pods become two series
        key = tuple(sorted((k, v) for k, v in labels.items() if k != "__name__"))
        s = series.get(key)
        if s is None:
            s = series[key] = {"metric": labels, "values": []}
        s["values"].append((r["ts_ns"], float(r["value"])))
    for s in series.values():
        s["values"].sort()
    return list(series.values())


def _promql_match(labels, matchers):
    for op, name, val in matchers:
        v = labels.get(name, "")
        if op == "=" and v != val: return False
        if op == "!=" and v == val: return False
        if op == "=~" and not re.fullmatch(val, v): return False
        if op == "!~" and re.fullmatch(val, v): return False
    return True


def _promql_eval_instant(node, t_ns):
    """Evaluate for an instant query at time t: returns list of series, each with one value at t."""
    kind = node[0]
    if kind == "num":
        return [{"metric": {}, "values": [(t_ns, node[1])]}]
    if kind == "vec":
        _, name, matchers, rng = node
        if rng is None:
            # instant vector: take the latest point per series within a short lookback (5 min)
            t0 = t_ns - 300 * int(1e9)
            series = _promql_fetch_vector(name, matchers, t0, t_ns)
            for s in series:
                s["values"] = s["values"][-1:] if s["values"] else []
            return [s for s in series if s["values"]]
        # range vector: keep all points in the range (function above will reduce them)
        return _promql_fetch_vector(name, matchers, t_ns - rng * int(1e9), t_ns)
    if kind == "fn":
        child = _promql_eval_instant(node[2], t_ns)
        reducer = {"rate": _fn_rate, "deriv": _fn_deriv}[node[1]]
        out = []
        for s in child:
            v = reducer(s["values"])
            if v is not None:
                out.append({"metric": s["metric"], "values": [(t_ns, v)]})
        return out
    if kind == "agg":
        _, op, child, group_kind, labels = node
        inputs = _promql_eval_instant(child, t_ns)
        groups = {}
        for s in inputs:
            if group_kind == "by":
                key = tuple(sorted((k, s["metric"].get(k, "")) for k in labels))
            elif group_kind == "without":
                key = tuple(sorted((k, v) for k, v in s["metric"].items() if k not in labels and k != "__name__"))
            else:
                key = ()
            g = groups.setdefault(key, [])
            if s["values"]:
                g.append(s["values"][-1][1])
        reducers = {"sum": sum, "avg": lambda xs: sum(xs) / len(xs) if xs else 0.0,
                    "min": min, "max": max, "count": len}
        out = []
        for key, vals in groups.items():
            if not vals:
                continue
            m = dict(key)
            out.append({"metric": m, "values": [(t_ns, float(reducers[op](vals)))]})
        return out
    if kind in ("bin", "cmp"):
        _, op, lhs, rhs = node
        L = _promql_eval_instant(lhs, t_ns); R = _promql_eval_instant(rhs, t_ns)
        return _promql_binop(op, L, R, keep_bool=(kind == "cmp"))
    raise ValueError(f"unknown node {kind}")


_BIN = {"+": lambda a, b: a + b, "-": lambda a, b: a - b, "*": lambda a, b: a * b,
        "/": lambda a, b: a / b if b else float("nan"), "%": lambda a, b: a % b if b else float("nan")}
_CMP = {"==": lambda a, b: a == b, "!=": lambda a, b: a != b, ">": lambda a, b: a > b,
        ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b}


def _promql_binop(op, L, R, keep_bool=False):
    def scalar(side):
        return len(side) == 1 and side[0]["metric"] == {} and len(side[0]["values"]) == 1
    both = _BIN.get(op) or _CMP.get(op)
    if both is None:
        raise ValueError("bad op " + op)
    out = []
    if scalar(L) and scalar(R):
        a, b = L[0]["values"][0][1], R[0]["values"][0][1]
        r = both(a, b)
        if keep_bool and not r:
            return []
        return [{"metric": {}, "values": [(L[0]["values"][0][0], 1.0 if keep_bool else float(r))]}]
    if scalar(L) or scalar(R):
        sc, vec, left_is_scalar = (L, R, True) if scalar(L) else (R, L, False)
        a = sc[0]["values"][0][1]
        for s in vec:
            if not s["values"]:
                continue
            ts, v = s["values"][-1]
            r = both(a, v) if left_is_scalar else both(v, a)
            if op in _CMP:
                if r:
                    out.append({"metric": s["metric"], "values": [(ts, 1.0 if keep_bool else v)]})
            else:
                out.append({"metric": s["metric"], "values": [(ts, float(r))]})
        return out
    # vector-vector: match by shared labels
    def key(s): return tuple(sorted((k, v) for k, v in s["metric"].items() if k != "__name__"))
    byR = {key(s): s for s in R}
    for s in L:
        k = key(s)
        if k not in byR:
            continue
        a, b = s["values"][-1][1], byR[k]["values"][-1][1]
        if op in _CMP:
            if both(a, b):
                out.append({"metric": s["metric"], "values": [(s["values"][-1][0], 1.0 if keep_bool else a)]})
        else:
            out.append({"metric": s["metric"], "values": [(s["values"][-1][0], float(both(a, b)))]})
    return out


def _fn_rate(values):
    """Per-second rate of a counter over the range. Resets on counter decreases, as Prometheus does."""
    if len(values) < 2:
        return None
    total, t0, last = 0.0, values[0][0], values[0][1]
    for ts, v in values[1:]:
        if v < last:
            total += v  # counter reset: treat the drop as continuing from 0
        else:
            total += v - last
        last = v
    span_s = (values[-1][0] - t0) / 1e9
    return total / span_s if span_s > 0 else None


def _fn_deriv(values):
    """Simple linear-regression slope, per second. Good enough for gauges without being noisy."""
    if len(values) < 2:
        return None
    t0 = values[0][0]
    xs = [(ts - t0) / 1e9 for ts, _ in values]; ys = [v for _, v in values]
    n = len(xs); sx = sum(xs); sy = sum(ys)
    sxx = sum(x * x for x in xs); sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    return (n * sxy - sx * sy) / denom


@app.get("/api/promql/query")
def promql_query(query: str, time: float = 0):
    """Instant query at a given time (unix seconds). Default: now. Prometheus-compatible JSON."""
    try:
        tree = _promql_parse(query)
    except ValueError as e:
        return {"status": "error", "errorType": "bad_data", "error": str(e)}
    t_ns = int((time or __import__("time").time()) * 1e9)
    try:
        result = _promql_eval_instant(tree, t_ns)
    except Exception as e:
        return {"status": "error", "errorType": "execution", "error": str(e)}
    # A result is only reported as "scalar" when the EXPRESSION itself is scalar (a literal, or scalar vs scalar
    # binop). Aggregations like count() return an unlabelled vector, which is what Prometheus does.
    is_scalar = tree[0] == "num" or (tree[0] == "bin" and tree[2][0] == "num" and tree[3][0] == "num")
    if is_scalar and len(result) == 1 and result[0]["metric"] == {}:
        ts, v = result[0]["values"][0]
        return {"status": "success", "data": {"resultType": "scalar", "result": [ts / 1e9, str(v)]}}
    return {"status": "success", "data": {"resultType": "vector",
            "result": [{"metric": s["metric"], "value": [s["values"][-1][0] / 1e9, str(s["values"][-1][1])]}
                       for s in result if s["values"]]}}


@app.get("/api/promql/query_range")
def promql_query_range(query: str, start: float, end: float, step: float = 60):
    """Range query between start and end (unix seconds), stepped by `step` seconds."""
    try:
        tree = _promql_parse(query)
    except ValueError as e:
        return {"status": "error", "errorType": "bad_data", "error": str(e)}
    step = max(1.0, float(step))
    if end <= start:
        return {"status": "error", "errorType": "bad_data", "error": "end must be greater than start"}
    out = {}
    try:
        t = start
        while t <= end + 0.001:
            res = _promql_eval_instant(tree, int(t * 1e9))
            for s in res:
                key = tuple(sorted(s["metric"].items()))
                e = out.setdefault(key, {"metric": s["metric"], "values": []})
                if s["values"]:
                    e["values"].append([t, str(s["values"][-1][1])])
            t += step
    except Exception as e:
        return {"status": "error", "errorType": "execution", "error": str(e)}
    return {"status": "success", "data": {"resultType": "matrix", "result": list(out.values())}}


@app.get("/api/promql/labels")
def promql_labels():
    """List every label name NebuLens knows about (for Grafana auto-complete)."""
    names = {"__name__", "service", "env"}
    for r in q("SELECT DISTINCT name FROM metrics LIMIT 2000"):
        pass  # names are the metric names list
    for r in q("SELECT attrs FROM metrics ORDER BY ts_ns DESC LIMIT 1000"):
        try:
            for k in _as_dict(r["attrs"]):
                names.add(k)
        except Exception:
            pass
    return {"status": "success", "data": sorted(names)}


@app.get("/api/promql/label/{name}/values")
def promql_label_values(name: str):
    """Values a label takes. Only the common labels are indexed; others scan recent rows."""
    if name == "__name__":
        return {"status": "success", "data": [r["name"] for r in q("SELECT DISTINCT name FROM metrics LIMIT 5000")]}
    if name in ("service", "env"):
        # services_seen is a small registry kept at ingest; DISTINCT over the metrics table scans every row
        col = "service" if name == "service" else "env"
        rows = q(f"SELECT DISTINCT {col} FROM services_seen WHERE {col}<>''")
        return {"status": "success", "data": sorted(r[col] for r in rows)}
    values = set()
    for r in q("SELECT attrs FROM metrics ORDER BY ts_ns DESC LIMIT 2000"):
        try:
            v = _as_dict(r["attrs"]).get(name)
            if v is not None:
                values.add(str(v))
        except Exception:
            pass
    return {"status": "success", "data": sorted(values)}


# ---------------------------------------------------------------- SSO (OIDC) + RBAC (self-hosted)
# Identity sources, checked in order: bearer NEBULENS_TOKEN (admin, unchanged), optional NEBULENS_READ_TOKEN (viewer), a signed
# session cookie issued after an OpenID Connect login, and - only when none of those is configured - open access (as before).
# Roles: viewer (read) < editor (create/change dashboards, alerts, SLOs, synthetics) < admin (also the audit log).
# /v1/* ingest is never session-authenticated: it needs NEBULENS_TOKEN or NEBULENS_INGEST_TOKEN when either is set.
ROLE_RANK = {"viewer": 1, "editor": 2, "admin": 3}
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _csv(name):
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def _load_oidc():
    cfg = dict(
        issuer=os.environ.get("NEBULENS_OIDC_ISSUER", "").strip().rstrip("/"),
        client_id=os.environ.get("NEBULENS_OIDC_CLIENT_ID", "").strip(),
        client_secret=os.environ.get("NEBULENS_OIDC_CLIENT_SECRET", ""),
        scopes=os.environ.get("NEBULENS_OIDC_SCOPES", "openid email profile"),
        role_claim=os.environ.get("NEBULENS_OIDC_ROLE_CLAIM", "groups").strip(),
        admin_groups=_csv("NEBULENS_OIDC_ADMIN_GROUPS"), editor_groups=_csv("NEBULENS_OIDC_EDITOR_GROUPS"),
        viewer_groups=_csv("NEBULENS_OIDC_VIEWER_GROUPS"),
        default_role=os.environ.get("NEBULENS_OIDC_DEFAULT_ROLE", "viewer").strip().lower(),
        allowed_domains=[d.lower().lstrip("@") for d in _csv("NEBULENS_OIDC_ALLOWED_DOMAINS")],
        redirect_url=os.environ.get("NEBULENS_OIDC_REDIRECT_URL", "").strip(),
        session_ttl_h=float(os.environ.get("NEBULENS_SESSION_TTL_HOURS", "12")),
        provider_name=os.environ.get("NEBULENS_OIDC_PROVIDER_NAME", "SSO").strip() or "SSO",
    )
    if cfg["default_role"] not in ROLE_RANK and cfg["default_role"] not in ("", "none", "deny"):
        raise RuntimeError("NEBULENS_OIDC_DEFAULT_ROLE must be viewer, editor, admin or none")
    if cfg["default_role"] in ("none", "deny"):
        cfg["default_role"] = ""
    return cfg


OIDC = _load_oidc()
READ_TOKEN = os.environ.get("NEBULENS_READ_TOKEN", "")
INGEST_TOKEN = os.environ.get("NEBULENS_INGEST_TOKEN", "")
# Regulated buyers keep audit trails for years. 0 disables deletion entirely (keep forever).
AUDIT_DAYS = float(os.environ.get("NEBULENS_AUDIT_RETENTION_DAYS", "90"))
_process_session_secret = None  # cache for the per-process fallback below, so repeated calls agree within one run


def _load_session_secret():
    """NEBULENS_SESSION_SECRET if set. Otherwise a random secret kept in a 0600 file beside the SQLite database, so SSO
    sessions survive a restart (Postgres and read-only volumes fall back to a per-process secret)."""
    global _process_session_secret
    env = os.environ.get("NEBULENS_SESSION_SECRET")
    if env:
        return env.encode()
    if _process_session_secret is not None:
        return _process_session_secret
    if not USE_PG:
        path = DB_PATH + ".session-secret"
        try:
            with open(path, "r", encoding="ascii") as f:
                v = f.read().strip()
            if len(v) >= 32:
                return v.encode()
        except OSError:
            pass
        v = secrets.token_hex(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(v)
            return v.encode()
        except OSError:
            pass
    _process_session_secret = secrets.token_hex(32).encode()
    return _process_session_secret


_SESSION_SECRET = _load_session_secret()


def sso_enabled():
    return not HOSTED and bool(OIDC["issuer"] and OIDC["client_id"])


if sso_enabled():
    if not os.environ.get("NEBULENS_SESSION_SECRET"):
        print("warning: NEBULENS_SESSION_SECRET is not set; using a generated secret stored beside the database (set it explicitly when running more than one replica)")
    if not (AUTH_TOKEN or INGEST_TOKEN):
        print("warning: SSO is on but /v1/* ingest has no token; set NEBULENS_TOKEN or NEBULENS_INGEST_TOKEN")


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(x):
    return base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))


def _sign(payload):
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return body + "." + _b64(hmac.new(_SESSION_SECRET, body.encode(), hashlib.sha256).digest())


def _unsign(tok):
    """Verify an HMAC-signed cookie value and return its payload, or None if forged, malformed or expired."""
    try:
        body, sig = (tok or "").split(".", 1)
        good = _b64(hmac.new(_SESSION_SECRET, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        d = json.loads(_unb64(body))
        return d if d.get("exp", 0) > time.time() else None
    except Exception:
        return None


def _required_role(method, path):
    if path == "/api/audit" or path.startswith("/api/audit/"):
        return "admin"
    if path.startswith("/api/environments/access"):
        return "admin"
    if path.startswith("/api/environments/") and path.endswith("/purge"):
        return "admin"   # permanently deletes an environment's telemetry
    if path == "/api/clusters" or path.startswith("/api/clusters/"):
        return "admin"   # tokens are credentials: only admins may list, create, rotate or remove them
    if method in _SAFE_METHODS or path in _READ_ONLY_POSTS:
        return "viewer"
    return "editor"


def _tok_eq(given, want):
    return bool(want) and hmac.compare_digest(given.encode(), want.encode())


def _bearer(request):
    h = request.headers.get("authorization", "")
    return h[7:] if h.startswith("Bearer ") else ""


def _identify(request):
    t = _bearer(request)
    if t:
        if _tok_eq(t, AUTH_TOKEN):
            return dict(actor="api-token", role="admin", via="token")
        if _tok_eq(t, READ_TOKEN):
            return dict(actor="read-token", role="viewer", via="token")
    if sso_enabled() and IS_PRO:      # SSO is Pro: after a trial ends, sessions from it stop working (the token still does)
        sess = _unsign(request.cookies.get("nl_session", ""))
        if sess and sess.get("role") in ROLE_RANK:
            return dict(actor=sess.get("email") or sess.get("sub", "?"), name=sess.get("name", ""), role=sess["role"],
                        via="session", groups=sess.get("grp") or [])
    if not (AUTH_TOKEN or READ_TOKEN or sso_enabled()):
        return dict(actor="anonymous", role="admin", via="open")
    return None


AUDIT_READS = os.environ.get("NEBULENS_AUDIT_READS", "").strip().lower() in ("1", "true", "yes")
AUDIT_SYSLOG = os.environ.get("NEBULENS_AUDIT_SYSLOG", "").strip()  # host:port[/udp|/tcp], e.g. siem:514/udp
_blank_dropped = [0]  # blank log lines discarded at ingest, reported by /api/redaction
_audit_chain_lock = threading.Lock()
_audit_prev = None  # hash of the newest audit row; loaded lazily so the chain survives restarts


def _audit_row_hash(prev, row):
    """Tamper-evident chain: each row hashes its own fields plus the previous row's hash, so
    deleting or editing any row breaks every hash after it."""
    payload = "|".join(str(x) for x in (prev, row["ts"], row["actor"], row["role"], row["via"],
                                        row["method"], row["path"], row["status"], row["detail"],
                                        row["ip"], row["req_id"], row["envs"]))
    return hashlib.sha256(payload.encode()).hexdigest()


def _audit_syslog(row):
    """Mirror one audit record to a syslog collector (Splunk, QRadar, rsyslog). Best effort:
    a SIEM being down must never block or fail the request that is being audited."""
    if not AUDIT_SYSLOG or not IS_ENTERPRISE:     # streaming the audit log to a SIEM is an Enterprise feature
        return
    try:
        dest, _, proto = AUDIT_SYSLOG.partition("/")
        host, _, port = dest.partition(":")
        port = int(port or 514)
        # RFC 5424, facility 13 (log audit) severity 6 (informational) => priority 110
        msg = (f"<110>1 {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(row['ts'] / 1e9))} "
               f"{socket.gethostname()} nebulens - - - " + json.dumps(row, separators=(",", ":")))
        data = msg.encode()[:8192]
        if (proto or "udp").lower() == "tcp":
            with socket.create_connection((host, port), timeout=2) as sk:
                sk.sendall(data + b"\n")
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
                sk.sendto(data, (host, port))
    except Exception as e:
        print("audit syslog failed:", e)


def _audit(ident, method, path, status, detail="", ip="", req_id="", envs=""):
    global _audit_prev
    try:
        row = dict(ts=time.time_ns(), actor=ident["actor"], role=ident["role"], via=ident["via"],
                   method=method, path=path, status=int(status), detail=detail[:300],
                   ip=ip, req_id=req_id, envs=envs)
        with _audit_chain_lock:
            if _audit_prev is None:
                last = q("SELECT hash FROM audit ORDER BY id DESC LIMIT 1")
                _audit_prev = (last[0]["hash"] if last else "") or ""
            prev = _audit_prev
            row_hash = _audit_row_hash(prev, row)
            _audit_prev = row_hash
        with db() as c:
            c.execute("""INSERT INTO audit(ts,actor,role,via,method,path,status,detail,ip,req_id,envs,prev_hash,hash)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (row["ts"], row["actor"], row["role"], row["via"], row["method"], row["path"],
                       row["status"], row["detail"], ip, req_id, envs, prev, row_hash))
        _audit_syslog(dict(row, hash=row_hash))
    except Exception as e:
        print("audit error:", e)


_PUBLIC_API = ("/api/mode", "/api/auth/config")


@app.middleware("http")
async def auth(request: Request, call_next):
    if HOSTED:
        return await _hosted_auth(request, call_next)
    path, method = request.url.path, request.method
    if path.startswith("/v1/"):
        if method == "OPTIONS":  # CORS preflight carries no credentials by design; the real request is still checked
            return await call_next(request)
        bearer = _bearer(request)
        if bearer.startswith(CLUSTER_TOKEN_PREFIX):
            cl = await run_in_threadpool(_cluster_for_token, bearer)
            if cl is None:
                return _auth_fail(request)
            if cl["disabled"]:
                return JSONResponse({"error": "cluster_disabled",
                                     "message": f"cluster '{cl['slug']}' is disabled in this NebuLens"}, status_code=403)
            _cur_cluster.set({"slug": cl["slug"], "name": cl["name"]})
            if _cluster_touch_due(cl["slug"]):
                await run_in_threadpool(_cluster_touch, cl["slug"])
            return await call_next(request)
        allowed = [t for t in (AUTH_TOKEN, INGEST_TOKEN) if t]
        if allowed and not any(_tok_eq(bearer, t) for t in allowed):
            return _auth_fail(request)
        return await call_next(request)
    if not path.startswith("/api/") or path in _PUBLIC_API or method == "OPTIONS":
        return await call_next(request)
    ident = _identify(request)
    if ident is None:
        return _auth_fail(request, sso=sso_enabled())
    # Per-environment access control (Pro): restrict this request to the environments the caller's
    # SSO groups allow. Endpoints that cannot apply the filter are refused outright for restricted
    # callers, so a scoped user can never read another environment's telemetry through a side door.
    allowed = _env_groups_allowed(ident.get("groups"), ident.get("role", "")) if ident.get("via") == "session" else None
    _cur_envs.set(allowed)
    if allowed is not None:
        if not allowed:
            return JSONResponse({"error": "env_forbidden", "status": 403,
                                 "message": "Your group is not granted access to any environment."}, status_code=403)
        if not path.startswith(_ENV_SCOPED_PATHS):
            return JSONResponse({"error": "env_forbidden", "status": 403,
                                 "message": "This view is not available to environment-restricted users.",
                                 "environments": sorted(allowed)}, status_code=403)
    need = _required_role(method, path)
    if ROLE_RANK[ident["role"]] < ROLE_RANK[need]:
        return JSONResponse({"error": "forbidden", "message": f"this action requires the {need} role (you are {ident['role']})",
                             "status": 403}, status_code=403)
    if ident["via"] == "session" and method not in _SAFE_METHODS:
        origin = request.headers.get("origin")  # cookie-authenticated writes must come from this site (CSRF)
        if origin and urllib.parse.urlparse(origin).netloc != request.headers.get("host", ""):
            return JSONResponse({"error": "forbidden", "message": "cross-origin request refused", "status": 403}, status_code=403)
    request.state.who = ident
    resp = await call_next(request)
    if method not in _SAFE_METHODS or (AUDIT_READS and path.startswith("/api/")):
        await run_in_threadpool(_audit, ident, method, path, resp.status_code, "",
                                _client_ip(request), resp.headers.get("x-request-id", ""),
                                ",".join(sorted(allowed)) if allowed else "")
    return resp


@app.middleware("http")
async def no_cache_ui(request: Request, call_next):
    """UI assets are tiny and change on deploy; always revalidate so users never see a stale UI."""
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


from starlette.requests import ClientDisconnect
from starlette.responses import Response
_ingest_disconnects = [0]   # senders that hung up mid-upload (see _otlp); shown in /api/stats


def _db_busy(e):
    """A transient database error (deadlock, lock timeout, dropped connection): worth retrying, not the sender's fault."""
    n = type(e).__name__
    msg = str(e).lower()
    return (n in ("DeadlockDetected", "SerializationFailure", "LockNotAvailable", "OperationalError", "InterfaceError", "PoolTimeout")
            or "deadlock detected" in msg or "database is locked" in msg)


async def _otlp(request: Request, name, mod, fn, kind):
    try:
        raw = await request.body()
    except ClientDisconnect:
        # The sender gave up mid-upload (usually its own timeout while the server is saturated). Nothing was stored and
        # its exporter will retry; this is not a server error, so do not log a stack trace for it.
        _ingest_disconnects[0] += 1
        return Response(status_code=400)
    ctype = request.headers.get("content-type", "")
    if request.headers.get("content-encoding") == "gzip":
        import gzip
        raw = gzip.decompress(raw)
    try:
        if "protobuf" in ctype:
            import importlib
            p = importlib.import_module(mod)
            d = _body_json(raw, ctype, getattr(p, name))
        else:
            d = json.loads(raw or b"{}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"bad OTLP payload: {e}")
    ident, reserved = getattr(request.state, "ident", None), 0
    if HOSTED:
        # Reserve capacity against the org's daily cap BEFORE inserting anything, atomically with the
        # admission check (see _reserve_usage) -- this is the authoritative quota gate; the middleware's
        # earlier check is just a cheap fast-path that avoids parsing a request from an org already over cap.
        ok, reserved = _reserve_usage(ident, kind, _otlp_precount(d, kind))
        if not ok:
            if ident:
                pexec("INSERT INTO usage(project_id,day,rejected) VALUES(?,?,1) ON CONFLICT(project_id,day) "
                      "DO UPDATE SET rejected=rejected+1", (ident["project_id"], _today()))
            return JSONResponse({"error": "quota_exceeded", "message": "this organization's daily ingest cap is reached"},
                                status_code=429, headers={"Retry-After": str(_seconds_to_midnight())})
    for attempt in range(4):
        try:
            n = await run_in_threadpool(fn, d)  # JSON decode + SQLite insert must not block the event loop
            break
        except HTTPException:
            raise  # keep tier/quota errors (403 tier_limit etc.) readable instead of calling them a bad payload
        except Exception as e:
            if _db_busy(e):    # the transaction was rolled back, so running it again cannot double count
                if attempt < 3:
                    await asyncio.sleep(random.uniform(0.02, 0.1) * (attempt + 1))
                    continue
                # 503 is retryable for OTLP senders; the 400 used before made collectors drop the batch
                raise HTTPException(503, f"database busy, retry: {type(e).__name__}", headers={"Retry-After": "1"})
            raise HTTPException(400, f"bad OTLP payload: {e}")
    if HOSTED:
        _usage_reconcile(ident, kind, reserved, n or 0)
    return {"partialSuccess": {}}


@app.post("/v1/traces")
async def v1_traces(r: Request):
    return await _otlp(r, "ExportTraceServiceRequest", "opentelemetry.proto.collector.trace.v1.trace_service_pb2", ingest_traces, "traces")


@app.post("/v1/logs")
async def v1_logs(r: Request):
    return await _otlp(r, "ExportLogsServiceRequest", "opentelemetry.proto.collector.logs.v1.logs_service_pb2", ingest_logs, "logs")


@app.post("/v1/metrics")
async def v1_metrics(r: Request):
    return await _otlp(r, "ExportMetricsServiceRequest", "opentelemetry.proto.collector.metrics.v1.metrics_service_pb2", ingest_metrics, "metrics")


@app.get("/healthz")
async def healthz():
    """Liveness: process is up and serving."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    """Readiness: database is reachable. A busy writer is not "not ready"."""
    try:
        await run_in_threadpool(q, "SELECT 1")
        return {"status": "ready", "retention_hours": RETENTION_HOURS}
    except Exception as e:
        raise HTTPException(503, f"not ready: {e}")


def _approx_rows(table):
    """Row count without counting: COUNT(*) over millions of rows took a minute, and the UI polls /api/stats every few
    seconds (the live badge), so the server stayed busy and showed "offline". SQLite: the rowid span (rows are appended and
    the oldest deleted, so it is close). Postgres: the planner's estimate. An empty table is always exactly 0."""
    if USE_PG:
        n = qrows("SELECT GREATEST(reltuples, 0)::bigint FROM pg_class WHERE oid = to_regclass(?)", (f"{PG_SCHEMA}.{table}",))
        n = int(n[0][0]) if n and n[0][0] is not None else 0
        if n <= 0:   # never analysed yet: just tell empty from not empty
            n = 1 if qrows(f"SELECT 1 FROM {table} LIMIT 1") else 0
        return n
    # Two separate queries on purpose: SQLite answers a lone MIN(rowid) or MAX(rowid) from the end of the b-tree instantly,
    # but "MAX(rowid) - MIN(rowid)" in one statement scans the whole table (25 s at 8M spans).
    hi = qrows(f"SELECT MAX(rowid) FROM {table}")[0][0]
    if hi is None:
        return 0
    lo = qrows(f"SELECT MIN(rowid) FROM {table}")[0][0]
    return int(hi) - int(lo) + 1


@app.get("/api/stats")
@ttl_cached
def stats():
    """Operational stats for monitoring NebuLens itself. Row counts are close estimates (see _approx_rows)."""
    return {
        "spans": _approx_rows("spans"),
        "logs": _approx_rows("logs"),
        "metrics": _approx_rows("metrics"),
        "services": q("SELECT COUNT(DISTINCT service) n FROM resource_seen WHERE sig='spans'")[0]["n"],
        "approximate": True,
        "ingest_disconnects": _ingest_disconnects[0],
        "alert_rules": q("SELECT COUNT(*) n FROM alert_rules")[0]["n"],
        "synthetics": q("SELECT COUNT(*) n FROM synthetics")[0]["n"],
        "retention_hours": RETENTION_HOURS,
        "db_path": DB_PATH if not HOSTED else "hosted",
    }


def since_ns(minutes):
    return time.time_ns() - int(float(minutes) * 60 * 1e9)


# ---- environment rollup (Environments page)
_ENV_UPSERT = ("INSERT INTO env_rollup(minute,env,service,spans,span_err,logs,log_err) VALUES(?,?,?,?,?,?,?) "
               "ON CONFLICT(minute,env,service) DO UPDATE SET spans=env_rollup.spans+excluded.spans, "
               "span_err=env_rollup.span_err+excluded.span_err, logs=env_rollup.logs+excluded.logs, log_err=env_rollup.log_err+excluded.log_err")


def _env_rollup_rows(items, spans):
    """items: (ts_ns, env, service, is_error) -> env_rollup rows, sorted by key (fixed lock order on Postgres)."""
    acc = {}
    for ts, env, svc, bad in items:
        k = (int(ts // 60_000_000_000), env or "", svc)
        a = acc.get(k)
        if a is None:
            a = acc[k] = [0, 0]
        a[0] += 1; a[1] += 1 if bad else 0
    return [(m, e, sv, n, er, 0, 0) if spans else (m, e, sv, 0, 0, n, er) for (m, e, sv), (n, er) in sorted(acc.items())]


def _env_rollup_write(c, items, spans):
    rows = _env_rollup_rows(items, spans)
    if rows:
        c.executemany(_ENV_UPSERT, rows)


def _marker(key):
    r = q("SELECT value FROM rollup_state WHERE key=?", (key,))
    return int(r[0]["value"]) if r else 1 << 62


def _env_ready(minutes):
    if minutes < _ROLLUP_MIN_WINDOW:
        return False
    try:
        return (time.time_ns() - int(minutes * 60e9)) // 60_000_000_000 >= _marker("env_from")
    except Exception:
        return False


# ---- span rollup (per-minute RED data written at ingest)
_RB = math.log(1.05)   # duration buckets are 5% wide, so percentiles read from the rollup are within ~5%
_ROLLUP_MIN_WINDOW = 5.0   # shorter windows are cheap to scan raw and want sub-minute accuracy
_ROLLUP_UPSERT = ("INSERT INTO span_rollup(minute,service,env,b,n,err,dur_sum,dur_max) VALUES(?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(minute,service,env,b) DO UPDATE SET n=span_rollup.n+excluded.n, err=span_rollup.err+excluded.err, "
                  "dur_sum=span_rollup.dur_sum+excluded.dur_sum, "
                  "dur_max=CASE WHEN excluded.dur_max>span_rollup.dur_max THEN excluded.dur_max ELSE span_rollup.dur_max END")


def _dur_bucket(ms):
    return int(math.log1p(max(ms, 0.0)) / _RB)


def _bucket_value(b):
    return math.exp((b + 0.5) * _RB) - 1


def _rollup_aggregate(spans):
    """spans: iterable of (service, env, start_ns, status, dur_ms) -> rows for span_rollup."""
    acc = {}
    for svc, env, start_ns, status, dur in spans:
        k = (int(start_ns // 60_000_000_000), svc, env or "", _dur_bucket(dur))
        a = acc.get(k)
        if a is None:
            a = acc[k] = [0, 0, 0.0, 0.0]
        a[0] += 1; a[1] += 1 if status == 2 else 0; a[2] += dur; a[3] = max(a[3], dur)
    # Sorted by primary key: concurrent ingest transactions then take their row locks in the same order, so two batches
    # touching the same minute can wait for each other but never deadlock (Postgres aborted one of them before).
    return [(m, sv, ev, b, a[0], a[1], a[2], a[3]) for (m, sv, ev, b), a in sorted(acc.items())]


_OP_UPSERT = ("INSERT INTO op_rollup(minute,service,env,name,n,err,dur_sum,dur_max) VALUES(?,?,?,?,?,?,?,?) "
              "ON CONFLICT(minute,service,env,name) DO UPDATE SET n=op_rollup.n+excluded.n, err=op_rollup.err+excluded.err, "
              "dur_sum=op_rollup.dur_sum+excluded.dur_sum, "
              "dur_max=CASE WHEN excluded.dur_max>op_rollup.dur_max THEN excluded.dur_max ELSE op_rollup.dur_max END")


def _op_aggregate(spans):
    """spans: iterable of (service, env, name, start_ns, status, dur_ms) -> op_rollup rows, sorted by key (no deadlocks)."""
    acc = {}
    for svc, env, name, start_ns, status, dur in spans:
        k = (int(start_ns // 60_000_000_000), svc, env or "", (name or "")[:200])
        a = acc.get(k)
        if a is None:
            a = acc[k] = [0, 0, 0.0, 0.0]
        a[0] += 1; a[1] += 1 if status == 2 else 0; a[2] += dur; a[3] = max(a[3], dur)
    return [(m, sv, ev, nm, a[0], a[1], a[2], a[3]) for (m, sv, ev, nm), a in sorted(acc.items())]


def _rollup_write(c, rows):
    if rows:
        c.executemany(_ROLLUP_UPSERT, rows)


def _rollup_from():
    r = q("SELECT value FROM rollup_state WHERE key='from_minute'")
    return int(r[0]["value"]) if r else 1 << 62


def _rollup_range(minutes):
    """(first_minute, seconds_covered) when the rollup can answer a window of this length, else None."""
    if minutes < _ROLLUP_MIN_WINDOW:
        return None
    now = time.time_ns()
    m0 = (now - int(minutes * 60e9)) // 60_000_000_000
    try:
        if m0 < _rollup_from():
            return None
    except Exception:
        return None
    return m0, max(60.0, (now - m0 * 60e9) / 1e9)


def _hist_pct(hist, n, frac, mx):
    """Value at rank frac of the merged duration histogram {bucket: count}."""
    rank, run = int(n * frac), 0
    for b in sorted(hist):
        run += hist[b]
        if run > rank:
            return min(_bucket_value(b), mx)
    return mx


def _rollup_services(m0, esql, eargs, secs):
    per = {}
    for svc, b, n, err, ds, dm in qrows(
            f"SELECT service, b, SUM(n), SUM(err), SUM(dur_sum), MAX(dur_max) FROM span_rollup WHERE minute>=?{esql} GROUP BY service, b",
            (m0, *eargs)):
        a = per.setdefault(svc, [0, 0, 0.0, 0.0, {}])
        n = int(n); a[0] += n; a[1] += int(err or 0); a[2] += float(ds or 0); a[3] = max(a[3], float(dm or 0)); a[4][b] = n
    rows = []
    for svc, (n, err, tot, mx, h) in sorted(per.items(), key=lambda kv: -kv[1][0]):
        rows.append(dict(service=svc, n=n, err=err, avg_ms=tot / n, max_ms=mx, p50=_hist_pct(h, n, .5, mx),
                         p95=_hist_pct(h, n, .95, mx), p99=_hist_pct(h, n, .99, mx), rate=n / secs, err_pct=100 * err / n))
    return rows


def _rollup_window(m_lo, m_hi=None):
    """{service: {n, err, err_pct, p95}} for rollup minutes in [m_lo, m_hi)."""
    sql, args = "SELECT service, b, SUM(n), SUM(err) FROM span_rollup WHERE minute>=?", [m_lo]
    if m_hi is not None:
        sql += " AND minute<?"; args.append(m_hi)
    per = {}
    for svc, b, n, err in qrows(sql + " GROUP BY service, b", tuple(args)):
        a = per.setdefault(svc, [0, 0, {}])
        n = int(n); a[0] += n; a[1] += int(err or 0); a[2][b] = n
    return {svc: {"n": n, "err": e, "err_pct": 100 * e / n if n else 0.0, "p95": _hist_pct(h, n, .95, float("inf"))}
            for svc, (n, e, h) in per.items()}


def _rollup_series(m0, t0, w, buckets, service=None):
    """{bucket: (n, err, dur_sum)} from the per-minute rollup, for buckets at least a minute wide."""
    sql, args = "SELECT minute, SUM(n), SUM(err), SUM(dur_sum) FROM span_rollup WHERE minute>=?", [m0]
    if service is not None:
        sql += " AND service=?"; args.append(service)
    out = {}
    for m, n, e, ds in qrows(sql + " GROUP BY minute", tuple(args)):
        # Every minute returned is inside the window, so clamp rather than drop: the minute still in progress (whose midpoint
        # can be later than "now") belongs in the last bucket, a partial first minute in the first.
        i = min(buckets - 1, max(0, int((m * 60_000_000_000 + 30_000_000_000 - t0) // w)))
        o = out.get(i, (0, 0, 0.0)); out[i] = (o[0] + int(n), o[1] + int(e or 0), o[2] + float(ds or 0))
    return out


# ---- edge rollup (service map)
_EDGE_LAG_NS = int(float(os.environ.get("NEBULENS_EDGE_LAG_SECONDS", "120")) * 1e9)   # wait for parents to be stored
_EDGE_SLICE_NS = 5 * 60_000_000_000
# The map is built from rolled-up edges, which trail live traffic by about the lag above (~2 min). The newest minutes are joined
# raw only while that is cheap: at thousands of spans/s the raw join took 10 s+ per request, but on a new install or a demo
# a 2-minute-late map looks empty. NEBULENS_MAP_TAIL: auto (default: include the newest minutes when fewer than
# _MAP_TAIL_MAX requests arrived since the cursor), 1 = always, 0 = never.
_MAP_TAIL = (os.environ.get("NEBULENS_MAP_TAIL", "auto").strip().lower() or "auto")
_MAP_TAIL = True if _MAP_TAIL in ("1", "true", "yes", "always") else False if _MAP_TAIL in ("0", "false", "no", "never") else "auto"
_MAP_TAIL_MAX = int(os.environ.get("NEBULENS_MAP_TAIL_MAX", "20000"))


def _map_tail_wanted(cur):
    if _MAP_TAIL != "auto":
        return _MAP_TAIL
    try:     # roots/server spans since the cursor, from the per-minute rollup: a cheap estimate of how big the raw join would be
        n = qrows("SELECT SUM(n) FROM span_rollup WHERE minute>=?", (cur // 60_000_000_000,))[0][0]
        return int(n or 0) < _MAP_TAIL_MAX
    except Exception:
        return False
_MIN_NS = 60_000_000_000
_EDGE_DST = """COALESCE(json_extract(attrs,'$."peer.service"'),
                               json_extract(attrs,'$."db.system"'),
                               json_extract(attrs,'$."messaging.system"'))"""
_EDGE_UPSERT = ("INSERT INTO edge_rollup(minute,src,dst,ext,calls,err,dur_sum) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(minute,src,dst,ext) DO UPDATE SET calls=edge_rollup.calls+excluded.calls, "
                "err=edge_rollup.err+excluded.err, dur_sum=edge_rollup.dur_sum+excluded.dur_sum")


def _edge_slice(lo_ns, hi_ns):
    """[(minute, src, dst, ext, calls, err, dur_sum)] for calls that started in [lo_ns, hi_ns)."""
    out = []
    for m, src, dst, n, e, d in qrows(
            """SELECT c.start_ns/60000000000, p.service, c.service, COUNT(*), SUM(CASE WHEN c.status=2 THEN 1 ELSE 0 END), SUM(c.dur_ms)
               FROM spans c JOIN spans p ON c.trace_id=p.trace_id AND c.parent_id=p.span_id
               WHERE c.start_ns>=? AND c.start_ns<? AND p.service<>c.service GROUP BY 1,2,3""", (lo_ns, hi_ns)):
        out.append((int(m), src, dst, 0, int(n), int(e or 0), float(d or 0)))
    # outbound calls to uninstrumented backends: explicit semantic attributes only (peer.service / db.system /
    # messaging.system), never raw server.address, so no bare-IP nodes that duplicate an instrumented service.
    for m, src, dst, n, e, d in qrows(
            f"""SELECT start_ns/60000000000, service, {_EDGE_DST}, COUNT(*), SUM(CASE WHEN status=2 THEN 1 ELSE 0 END), SUM(dur_ms)
                FROM spans WHERE kind=3 AND start_ns>=? AND start_ns<? AND {_EDGE_DST} IS NOT NULL GROUP BY 1,2,3""", (lo_ns, hi_ns)):
        out.append((int(m), src, dst, 1, int(n), int(e or 0), float(d or 0)))
    return out


def _edge_state(key):
    r = q("SELECT value FROM rollup_state WHERE key=?", (key,))
    return int(r[0]["value"]) if r else None


def _edge_cursor():
    return _edge_state("edge_cursor")


_edge_lock = threading.Lock()


def _edge_rollup_step(now_ns=None, max_slices=6):
    """Advance the edge rollup over spans old enough to have their parents. Safe to call from every worker tick."""
    with _edge_lock:
        return _edge_rollup_step_locked(now_ns or time.time_ns(), max_slices)


def _edge_rollup_step_locked(now_ns, max_slices):
    cur = _edge_cursor()
    if cur is None:
        oldest = qrows("SELECT MIN(start_ns) FROM spans")[0][0]
        cur = int((oldest if oldest is not None else now_ns) // _MIN_NS * _MIN_NS)
        with db() as c:
            c.execute("INSERT INTO rollup_state(key, value) VALUES('edge_cursor', ?) ON CONFLICT(key) DO NOTHING", (cur,))
        cur = _edge_cursor()
    end = (now_ns - _EDGE_LAG_NS) // _MIN_NS * _MIN_NS
    for _ in range(max_slices):
        if cur >= end:
            break
        hi = min(end, cur + _EDGE_SLICE_NS)
        rows = sorted(_edge_slice(cur, hi), key=lambda r: r[:4])
        with db() as c:       # rows and cursor move together, so a restart never double counts a slice
            if rows:
                c.executemany(_EDGE_UPSERT, rows)
            c.execute("UPDATE rollup_state SET value=? WHERE key='edge_cursor'", (hi,))
        cur = hi
    return cur


def _edge_ready(minutes):
    """(first_minute, cursor_ns) when /api/map can be served from edge_rollup plus a raw tail, else None."""
    if minutes < _ROLLUP_MIN_WINDOW:
        return None
    try:
        cur = _edge_cursor()
    except Exception:
        return None
    if cur is None or cur < time.time_ns() - _EDGE_LAG_NS - 15 * _MIN_NS:    # still catching up
        return None
    return (time.time_ns() - int(minutes * 60e9)) // _MIN_NS, cur


def _window(minutes, start_ms=0, end_ms=0):
    """(t0_ns, t1_ns) for a log query. A window dragged out on the chart (start_ms/end_ms, epoch milliseconds)
    wins over "the last N minutes"; t1 is 0 when there is no upper bound."""
    if start_ms and end_ms and end_ms > start_ms:
        return int(start_ms * 1e6), int(end_ms * 1e6)
    return since_ns(minutes), 0


# ---- resources / services (RED)
@app.get("/api/services")
@ttl_cached
def services(minutes: float = 60, env: str = ""):
    t0 = since_ns(minutes)
    esql, eargs = env_scope(env)
    rr = _rollup_range(minutes)
    if rr:
        return _services_add_seen(_rollup_services(rr[0], esql, eargs, rr[1]), t0, esql, eargs)
    # One scan: counts, errors, avg/max and percentiles per service, aggregated in Python from plain tuples.
    per = {}
    for svc, st, dur in qrows(f"SELECT service, status, dur_ms FROM spans WHERE start_ns>=? AND (parent_id='' OR kind=2){esql}", (t0, *eargs)):
        a = per.get(svc)
        if a is None:
            a = per[svc] = [0, 0, 0.0, 0.0, []]
        a[0] += 1; a[1] += st == 2; a[2] += dur; a[3] = max(a[3], dur); a[4].append(dur)
    rows = []
    for svc, (n, err, tot, mx, d) in sorted(per.items(), key=lambda kv: -kv[1][0]):
        d.sort()
        rows.append(dict(service=svc, n=n, err=err, avg_ms=tot / n, max_ms=mx,
                         p50=d[int(n * .5)], p95=d[min(n - 1, int(n * .95))], p99=d[min(n - 1, int(n * .99))]))
    for r in rows:
        r["rate"] = r["n"] / (minutes * 60)
        r["err_pct"] = 100 * (r["err"] or 0) / r["n"] if r["n"] else 0
        r["err"] = r["err"] or 0
    return _services_add_seen(rows, t0, esql, eargs)


def _services_add_seen(rows, t0, esql, eargs):
    # services seen only via logs/metrics
    known = {r["service"] for r in rows}
    # Read the small registry kept at ingest instead of SELECT DISTINCT over the logs and metrics tables
    # (which scans every metric row on a busy cluster and took about a minute).
    for x in q(f"SELECT DISTINCT service FROM services_seen WHERE last_seen>=?{esql}", (int(t0 / 1e9), *eargs)):
        if x["service"] not in known:
            known.add(x["service"])
            rows.append(dict(service=x["service"], n=0, err=0, err_pct=0, avg_ms=0, max_ms=0, p50=0, p95=0, p99=0, rate=0))
    return rows


@app.get("/api/services/{svc}/timeseries")
def service_ts(svc: str, minutes: float = 60, buckets: int = Query(60, ge=1, le=1000)):
    buckets = max(1, min(int(buckets), 1000))
    t0 = since_ns(minutes)
    w = int(minutes * 60 * 1e9 / buckets)
    rr = _rollup_range(minutes)
    if rr and w >= 60_000_000_000:
        by = _rollup_series(rr[0], t0, w, buckets, svc)
        return [dict(t=(t0 + i * w) / 1e6, rate=by[i][0] / (w / 1e9) if i in by else 0,
                     err=by[i][1] / (w / 1e9) if i in by else 0, avg_ms=by[i][2] / by[i][0] if i in by and by[i][0] else 0)
                for i in range(buckets)]
    rows = q("""SELECT (start_ns-?)/? b, COUNT(*) n, SUM(status=2) err, AVG(dur_ms) avg_ms
                FROM spans WHERE service=? AND start_ns>=? AND (parent_id='' OR kind=2) GROUP BY b""", (t0, w, svc, t0))
    by = {r["b"]: r for r in rows}
    out = []
    for i in range(buckets):
        r = by.get(i)
        out.append(dict(t=(t0 + i * w) / 1e6, rate=(r["n"] / (w / 1e9)) if r else 0, err=(r["err"] or 0) / (w / 1e9) if r else 0,
                        avg_ms=r["avg_ms"] if r else 0))
    return out


@app.get("/api/resources/{svc}")
def resource_attrs(svc: str):
    for t in ("spans", "logs", "metrics"):
        r = q(f"SELECT res FROM {t} WHERE service=? LIMIT 1", (svc,))
        if r:
            return _jload(r[0]["res"])
    return {}


@app.get("/api/operations")
@ttl_cached
def operations(service: str, minutes: float = 60):
    t0 = since_ns(minutes)
    if _ops_ready(minutes):
        return [{"name": nm, "n": int(n), "err": int(e or 0), "avg_ms": float(ds or 0) / float(n) if n else 0, "max_ms": float(mx or 0)}
                for nm, n, e, ds, mx in qrows("SELECT name, SUM(n), SUM(err), SUM(dur_sum), MAX(dur_max) FROM op_rollup "
                                              "WHERE service=? AND minute>=? GROUP BY name ORDER BY SUM(n) DESC LIMIT 100",
                                              (service, t0 // 60_000_000_000))]
    return q("""SELECT name, COUNT(*) n, SUM(status=2) err, AVG(dur_ms) avg_ms, MAX(dur_ms) max_ms
                FROM spans WHERE service=? AND start_ns>=? AND (kind IN (2,5) OR parent_id='') GROUP BY name ORDER BY n DESC LIMIT 100""",
             (service, t0))


# ---- service map
@app.get("/api/map")
@ttl_cached
def service_map(minutes: float = 60):
    t0 = since_ns(minutes)
    er = _edge_ready(minutes)
    if er:
        m0, cur = er
        acc = {}
        def add(src, dst, ext, n, e, d):
            a = acc.setdefault((src, dst, ext), [0, 0, 0.0]); a[0] += n; a[1] += e; a[2] += d
        for src, dst, ext, n, e, d in qrows("SELECT src, dst, ext, SUM(calls), SUM(err), SUM(dur_sum) FROM edge_rollup "
                                            "WHERE minute>=? GROUP BY src, dst, ext", (m0,)):
            add(src, dst, ext, int(n), int(e or 0), float(d or 0))
        if _map_tail_wanted(cur):
            for _m, src, dst, ext, n, e, d in _edge_slice(cur, 1 << 62):       # everything newer than the cursor (also clock-skewed spans)
                add(src, dst, ext, n, e, d)
        edges = [dict(src=k[0], dst=k[1], calls=a[0], err=a[1], avg_ms=a[2] / a[0]) for k, a in acc.items() if k[2] == 0 and a[0]]
        ext = [dict(src=k[0], dst=k[1], calls=a[0], err=a[1], avg_ms=a[2] / a[0]) for k, a in acc.items() if k[2] == 1 and a[0]]
        rr = _rollup_range(minutes)
        svc_names = ({r[0] for r in qrows("SELECT DISTINCT service FROM span_rollup")} if rr
                     else {s["service"] for s in q("SELECT DISTINCT service FROM spans")})
    else:
        edges = q("""SELECT p.service src, c.service dst, COUNT(*) calls, SUM(c.status=2) err, AVG(c.dur_ms) avg_ms
                     FROM spans c JOIN spans p ON c.trace_id=p.trace_id AND c.parent_id=p.span_id
                     WHERE c.start_ns>=? AND p.service<>c.service GROUP BY src,dst""", (t0,))
        ext = q(f"""SELECT service src, {_EDGE_DST} dst,
                          COUNT(*) calls, SUM(status=2) err, AVG(dur_ms) avg_ms
                   FROM spans WHERE kind=3 AND start_ns>=? GROUP BY src,dst HAVING {_EDGE_DST} IS NOT NULL""", (t0,))
        svc_names = {s["service"] for s in q("SELECT DISTINCT service FROM spans")}
    seen = {(e["src"], e["dst"]) for e in edges}
    for e in ext:
        # don't duplicate an edge we already derived from span parent/child,
        # and don't add a node that is actually an instrumented service
        if e["dst"] in svc_names or (e["src"], e["dst"]) in seen:
            continue
        edges.append(e)
    return edges


@app.get("/api/map/nodes")
@ttl_cached
def map_nodes(minutes: float = 60):
    """Per-service resource metadata for the service map (language badge, namespace, version).
    Kept separate from /api/map so that endpoint's contract (a bare edge list) is unchanged.
    Read from resource_seen (one row per distinct resource, written at ingest). It used to GROUP BY service over every
    span in the window comparing their resource JSON, which took over a minute with a few million spans and left the
    map waiting."""
    _res_flush()
    out = {}
    for service, lang, ns, ver, env in qrows(
            "SELECT service, lang, ns, svc_version, env FROM resource_seen WHERE sig='spans' AND last_seen>=? ORDER BY last_seen",
            (int(time.time() - minutes * 60),)):
        cur = out.setdefault(service, {"service": service, "language": "", "namespace": "", "version": "", "env": "", "runtime": ""})
        for k, v in (("language", lang), ("namespace", ns), ("version", ver), ("env", env)):
            if v:
                cur[k] = v          # rows come oldest first, so the newest non-empty value wins
    return list(out.values())


# ---- traces
def _trace_text_filter(s, minutes, service, where, args):
    """Text search in the trace list. It used to test name, attrs (the whole JSON text) and trace_id with a leading-wildcard
    LIKE on every span in the window (1.5-2 s at 1.3M spans and growing). Now: a hex fragment is a trace-id prefix on the
    index; key=value searches the attributes; any other word matches operation names known from op_rollup."""
    if re.fullmatch(r"[0-9a-fA-F]{4,32}", s):
        where.append("trace_id>=? AND trace_id<?"); args += [s.lower(), s.lower() + "g"]
        return True
    def op_names(text):
        sql, a = "SELECT DISTINCT name FROM op_rollup WHERE minute>=? AND name LIKE ?", [since_ns(minutes) // 60_000_000_000, f"%{text}%"]
        if service:
            sql += " AND service=?"; a.append(service)
        return [r[0] for r in qrows(sql + " LIMIT 200", tuple(a))]
    if "=" in s:
        # key=value: values such as an http.route usually appear in operation names (GET /cart), which are indexed through
        # op_rollup; only otherwise read attributes, and then only the newest 2 minutes (a whole-window attrs LIKE timed out).
        v = s.split("=", 1)[1].strip()
        names = op_names(v) if v and _ops_ready(minutes) else []
        if names:
            where.append("name IN (" + ",".join("?" * len(names)) + ")"); args += names
        else:
            where.append("start_ns>=? AND attrs LIKE ?"); args += [since_ns(min(minutes, 0.75)), f"%{v}%"]   # newest 45 s
        return True
    if _ops_ready(minutes):
        names = op_names(s)
        if not names:
            return False          # no operation with that name was seen in the window: nothing to scan for
        where.append("name IN (" + ",".join("?" * len(names)) + ")"); args += names
        return True
    where.append("start_ns>=? AND name LIKE ?"); args += [since_ns(min(minutes, 15)), f"%{s}%"]   # before op_rollup is ready
    return True


@app.get("/api/traces")
def traces(minutes: float = 60, service: str = "", errors: bool = False, min_ms: float = 0, q_: str = "", limit: int = 100):
    t0 = since_ns(minutes)
    where, args = ["start_ns>=?"], [t0]
    if service:
        where.append("service=?"); args.append(service)
    if q_ and _trace_text_filter(q_.strip(), minutes, service, where, args) is False:
        return []
    # Candidate traces = those with a span among the newest spans in the window (index scan on start_ns),
    # so listing 50 traces does not aggregate every trace in the window.
    cap = max(limit * 4, 400) if (errors or q_) else max(limit * 50, 5000)   # filtered lists: never walk 10k candidates
    cand = list(where) + (["status=2"] if errors else [])
    # Candidate traces: those with a span among the newest spans in the window (index scan on start_ns), newest first.
    ids, seen = [], set()
    for (tid,) in qrows(f"SELECT trace_id FROM spans WHERE {' AND '.join(cand)} ORDER BY start_ns DESC LIMIT {int(cap)}", tuple(args)):
        if tid not in seen:
            seen.add(tid); ids.append(tid)
    if not (errors or min_ms):
        ids = ids[:max(4 * limit, 200)]   # no filter can discard a candidate, so a few times the page size is plenty
    # Then one indexed fetch of just those traces' spans, aggregated here. The single SQL statement this replaces (IN-subquery,
    # GROUP BY and two correlated lookups per trace) took ~1.5 s on Postgres at 1.7M spans.
    agg = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for tid, name, svc, parent, st, en, status in qrows(
                f"SELECT trace_id, name, service, parent_id, start_ns, end_ns, status FROM spans WHERE trace_id IN ({','.join('?' * len(chunk))})",
                tuple(chunk)):
            a = agg.get(tid)
            if a is None:
                a = agg[tid] = {"trace_id": tid, "start_ns": st, "end": en, "spans": 0, "errors": 0, "svcs": set(), "rk": None,
                                "root": None, "root_service": None}
            a["start_ns"] = min(a["start_ns"], st); a["end"] = max(a["end"], en)
            a["spans"] += 1; a["errors"] += 1 if status == 2 else 0; a["svcs"].add(svc)
            rk = (parent != "", st)            # the root: a span with no parent, else the earliest span
            if a["rk"] is None or rk < a["rk"]:
                a["rk"], a["root"], a["root_service"] = rk, name, svc
    out = []
    for a in agg.values():
        dur = (a["end"] - a["start_ns"]) / 1e6
        if (errors and not a["errors"]) or (min_ms and dur < float(min_ms)):
            continue
        out.append({"trace_id": a["trace_id"], "start_ns": a["start_ns"], "dur_ms": dur, "spans": a["spans"], "errors": a["errors"],
                    "services": len(a["svcs"]), "root": a["root"], "root_service": a["root_service"]})
    out.sort(key=lambda x: -x["start_ns"])
    return out[:limit]


@app.get("/api/traces/{trace_id}")
def trace(trace_id: str):
    spans = q("SELECT * FROM spans WHERE trace_id=? ORDER BY start_ns LIMIT 20000", (trace_id,))   # bound a runaway trace
    if not spans:
        raise HTTPException(404, "trace not found")
    for s in spans:
        s["attrs"] = _jload(s["attrs"])
        s.pop("res", None)
    lo = min(x["start_ns"] for x in spans) - 60_000_000_000
    hi = max(x["end_ns"] or x["start_ns"] for x in spans) + 60_000_000_000
    logs = q("SELECT ts_ns, service, sev_text, severity, body, span_id FROM logs WHERE trace_id=? AND ts_ns>=? AND ts_ns<=? "
             "ORDER BY ts_ns LIMIT 2000", (trace_id, lo, hi))
    return {"spans": spans, "logs": logs}


# ---- logs
@app.get("/api/logs")
def logs(minutes: float = 60, service: str = "", min_severity: int = 0, q_: str = "", trace_id: str = "", limit: int = 300, env: str = "",
         start_ms: float = 0, end_ms: float = 0):
    t0, t1 = _window(minutes, start_ms, end_ms)
    where, args = ["ts_ns>=?"], [t0]
    if t1:
        where.append("ts_ns<?"); args.append(t1)
    esql, eargs = env_scope(env)
    if esql:
        where.append(esql[5:]); args += eargs
    if service:
        where.append("service=?"); args.append(service)
    if min_severity:
        where.append("severity>=?"); args.append(min_severity)
    if trace_id:
        where.append("trace_id=?"); args.append(trace_id)
    if q_:
        # supports `key=value` attribute filters and free text
        for tok in q_.split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                # Cast to text so numeric attrs (e.g. http.status_code=500) match too
                where.append("(CAST(json_extract(attrs, ?) AS TEXT)=? OR CAST(json_extract(res, ?) AS TEXT)=?)")
                args += [f'$."{k}"', v, f'$."{k}"', v]
            else:
                where.append("body LIKE ?"); args.append(f"%{tok}%")
    rows = q(f"SELECT * FROM logs WHERE {' AND '.join(where)} ORDER BY ts_ns DESC LIMIT ?", (*args, limit))
    for r in rows:
        r["attrs"] = _jload(r["attrs"])
        r["k8s"] = _k8s_ctx(r.pop("res", None), r["attrs"])
    return rows


_K8S_CTX_KEYS = ("k8s.cluster.name", "k8s.namespace.name", "k8s.pod.name", "k8s.container.name", "k8s.node.name")


def _k8s_ctx(res, attrs):
    """Where a log line ran (cluster / namespace / pod / container / node), for the Logs page. Only these keys, not the
    whole resource blob, so the response stays small."""
    try:
        r = _jload(res) if res else {}
    except Exception:
        r = {}
    r = r.get("attributes", r) if isinstance(r, dict) else {}
    out = {}
    for k in _K8S_CTX_KEYS:
        v = (r.get(k) if isinstance(r, dict) else None) or (attrs or {}).get(k)
        if v:
            out[k] = str(v)
    return out


@app.get("/api/logs/histogram")
@ttl_cached
def logs_hist(minutes: float = 60, service: str = "", buckets: int = Query(60, ge=1, le=1000), env: str = "",
              start_ms: float = 0, end_ms: float = 0):
    buckets = max(1, min(int(buckets), 1000))
    t0, t1 = _window(minutes, start_ms, end_ms)
    span = (t1 - t0) if t1 else int(minutes * 60 * 1e9)
    w = max(1, int(span / buckets))
    where, args = ["ts_ns>=?"], [t0]
    if t1:
        where.append("ts_ns<?"); args.append(t1)
    esql, eargs = env_scope(env)
    if esql:
        where.append(esql[5:]); args += eargs
    if service:
        where.append("service=?"); args.append(service)
    rows = q(f"""SELECT (ts_ns-?)/? b, COUNT(*) n,
                   SUM(CASE WHEN severity>=21 THEN 1 ELSE 0 END) fatal,
                   SUM(CASE WHEN severity>=17 AND severity<21 THEN 1 ELSE 0 END) err,
                   SUM(CASE WHEN severity>=13 AND severity<17 THEN 1 ELSE 0 END) warn,
                   SUM(CASE WHEN severity>=9 AND severity<13 THEN 1 ELSE 0 END) info,
                   SUM(CASE WHEN severity>=1 AND severity<9 THEN 1 ELSE 0 END) debug,
                   SUM(CASE WHEN severity<1 OR severity IS NULL THEN 1 ELSE 0 END) unset
                 FROM logs WHERE {' AND '.join(where)} GROUP BY b""", (t0, w, *args))
    by = {r["b"]: r for r in rows}
    def g(i, k): return by.get(i, {}).get(k) or 0
    return [dict(t=(t0 + i * w) / 1e6, n=g(i, "n"), fatal=g(i, "fatal"), err=g(i, "err"),
                 warn=g(i, "warn"), info=g(i, "info"), debug=g(i, "debug"), unset=g(i, "unset"))
            for i in range(buckets)]


# ---- metrics
@app.get("/api/metrics/names")
@ttl_cached
def metric_names(minutes: float = 60):
    """Metric names with point counts and the services that report them. Limited to the time window:
    without one this counted EVERY metric row ever stored on each visit to the Metrics page, which
    takes minutes on a busy cluster. minutes=0 returns everything (old behaviour)."""
    # Served from metric_names_seen (hourly buckets), so counts are per whole hour: a 60-minute window
    # covers the current and previous hour bucket. Names seen before this table existed appear once they
    # next report; nothing is backfilled from the raw table (that scan is the thing being avoided).
    _gc = "STRING_AGG(DISTINCT service, ',')" if USE_PG else "GROUP_CONCAT(DISTINCT service)"
    where, args = "", ()
    if minutes and minutes > 0:
        where, args = "WHERE hr>=?", (int(since_ns(minutes) // 3_600_000_000_000),)
    rows = q(f"SELECT name, unit, mtype, SUM(points) points, {_gc} services FROM metric_names_seen {where} "
             f"GROUP BY name, unit, mtype ORDER BY name", args)
    return [{"name": k, "unit": u, "mtype": "built-in", "points": None, "services": None, "description": d}
            for k, (u, d) in BUILTIN_METRICS.items()] + list(rows)


def _metric_series(name, service, minutes, buckets, agg, group_by=""):
    if name in BUILTIN_METRICS:
        return _builtin_series(name, service, minutes, buckets, group_by)
    buckets = max(1, min(int(buckets), 1000))
    t0 = since_ns(minutes); w = int(minutes * 60 * 1e9 / buckets)
    fn = {"avg": "AVG", "max": "MAX", "min": "MIN", "sum": "SUM", "count": "COUNT"}.get(agg, "AVG")
    where, args = ["name=?", "ts_ns>=?"], [name, t0]
    if service:
        where.append("service=?"); args.append(service)
    if group_by:
        # Look at the data point's own attributes first, then the resource's: Kubernetes metrics carry k8s.pod.name,
        # k8s.container.name and k8s.node.name on the resource, so grouping by them has to see those too.
        g = ", COALESCE(json_extract(attrs, ?), json_extract(res, ?)) grp"
        g_args = (f'$."{group_by}"', f'$."{group_by}"')
    else:
        g = ", service grp"
        g_args = ()
    rows = q(f"SELECT (ts_ns-?)/? b{g}, {fn}(value) v FROM metrics WHERE {' AND '.join(where)} GROUP BY b, grp", (t0, w, *g_args, *args))
    series = {}
    for r in rows:
        series.setdefault(str(r["grp"]), {})[r["b"]] = r["v"]
    return {"t": [(t0 + i * w) / 1e6 for i in range(buckets)],
            "series": {k: [v.get(i) for i in range(buckets)] for k, v in series.items()}}


# ---- built-in metrics: derived from spans and logs, nothing to configure
# Served from the per-minute rollups written at ingest (span_rollup: server/root spans per service with a duration histogram;
# env_rollup: span and log counts per service), so they cost no extra storage and stay fast at any volume. They behave like
# any other metric: Metrics explorer, dashboards and alerts (metric_avg / metric_max with the name as the expression).
BUILTIN_METRICS = {
    "nebulens.span.requests":       ("1/s", "Requests per second (server and root spans)"),
    "nebulens.span.errors":         ("1/s", "Failed requests per second"),
    "nebulens.span.error_rate":     ("%",   "Share of requests that failed"),
    "nebulens.span.duration.avg":   ("ms",  "Average request duration"),
    "nebulens.span.duration.p50":   ("ms",  "Median request duration"),
    "nebulens.span.duration.p95":   ("ms",  "95th percentile request duration"),
    "nebulens.span.duration.p99":   ("ms",  "99th percentile request duration"),
    "nebulens.log.records":         ("1/s", "Log records per second"),
    "nebulens.log.errors":          ("1/s", "Error log records (ERROR and above) per second"),
}


def _builtin_series(name, service, minutes, buckets, group_by=""):
    """Time series of one built-in metric, one series per service (or per environment with group_by env)."""
    buckets = max(1, min(int(buckets), 1000))
    t0 = since_ns(minutes); w = int(minutes * 60 * 1e9 / buckets)
    secs = max(w / 1e9, 60.0)                 # rollups are per minute: a narrower bucket still holds a whole minute
    gcol = "env" if group_by in ("env", "deployment.environment") else "service"
    where, args = "minute>=?", [t0 // 60_000_000_000]
    if service:
        where += " AND service=?"; args.append(service)
    idx = lambda m: min(buckets - 1, max(0, int((m * 60_000_000_000 + 30_000_000_000 - t0) // w)))
    series = {}
    if name.startswith("nebulens.log."):
        col = "log_err" if name == "nebulens.log.errors" else "logs"
        for m, g, n in qrows(f"SELECT minute, {gcol}, SUM({col}) FROM env_rollup WHERE {where} GROUP BY minute, {gcol}", tuple(args)):
            d = series.setdefault(str(g or ""), {}); i = idx(m); d[i] = d.get(i, 0) + int(n or 0)
        out = {g: [(d[i] / secs) if i in d else None for i in range(buckets)] for g, d in series.items()}
    else:
        for m, g, b, n, e, ds, dm in qrows(
                f"SELECT minute, {gcol}, b, SUM(n), SUM(err), SUM(dur_sum), MAX(dur_max) FROM span_rollup WHERE {where} "
                f"GROUP BY minute, {gcol}, b", tuple(args)):
            d = series.setdefault(str(g or ""), {}); i = idx(m)
            a = d.setdefault(i, [0, 0, 0.0, 0.0, {}])
            n = int(n or 0); a[0] += n; a[1] += int(e or 0); a[2] += float(ds or 0); a[3] = max(a[3], float(dm or 0))
            a[4][b] = a[4].get(b, 0) + n

        def val(a):
            n, e, ds, mx, h = a
            if not n:
                return None
            if name == "nebulens.span.requests":
                return n / secs
            if name == "nebulens.span.errors":
                return e / secs
            if name == "nebulens.span.error_rate":
                return 100.0 * e / n
            if name == "nebulens.span.duration.avg":
                return ds / n
            frac = {"nebulens.span.duration.p50": .5, "nebulens.span.duration.p95": .95, "nebulens.span.duration.p99": .99}[name]
            return _hist_pct(h, n, frac, mx)
        out = {g: [val(d[i]) if i in d else None for i in range(buckets)] for g, d in series.items()}
    return {"t": [(t0 + i * w) / 1e6 for i in range(buckets)], "series": out}


@app.get("/api/metrics/query")
def metric_query(name: str, service: str = "", minutes: float = 60, buckets: int = Query(60, ge=1, le=1000), agg: str = "avg", group_by: str = ""):
    return _metric_series(name, service, minutes, buckets, agg, group_by)


# ---- service health score (unique differentiator vs Dash0)
# Combines error rate, p95 latency drift, log errors and synthetic health into
# a 0-100 score per service with a one-line reason. NebuLens computes this from
# raw signals; no rules required.
def _health_score(svc, n, err, p95, p95_prev, log_err_rate, syn_down):
    """The health score rules (unchanged), from numbers that may come from the rollup or from a raw scan."""
    if not n:
        return {"service": svc, "score": None, "reason": "no traffic in window"}
    err_pct = 100 * (err or 0) / n
    p95_prev = p95_prev or p95
    drift = (p95 - p95_prev) / p95_prev if p95_prev else 0
    score = 100.0
    reasons = []
    if err_pct > 1:
        score -= min(40, err_pct * 4)
        reasons.append(f"error rate {err_pct:.1f}%")
    if drift > 0.2:
        score -= min(25, drift * 50)
        reasons.append(f"p95 latency up {drift * 100:.0f}% vs prior window")
    if log_err_rate > 5:
        score -= min(20, log_err_rate)
        reasons.append(f"{log_err_rate:.0f} error logs/min")
    if syn_down > 0:
        score -= min(15, syn_down * 3)
        reasons.append(f"{syn_down} failed synthetic checks")
    score = max(0, round(score, 1))
    state = "healthy" if score >= 90 else "degraded" if score >= 70 else "unhealthy"
    return {"service": svc, "score": score, "state": state, "reason": "; ".join(reasons) or "all green",
            "signals": {"error_pct": round(err_pct, 2), "p95_ms": round(p95, 1),
                        "p95_drift_pct": round(drift * 100, 1), "log_err_per_min": round(log_err_rate, 1)}}


def _health_inputs_fast(minutes):
    """Everything the health score needs, for every service at once, from the rollups: {svc: (n, err, p95, p95_prev, log_err/min)}."""
    rr = _rollup_range(2 * minutes)
    if not rr or not _env_ready(minutes):
        return None
    m_cur = since_ns(minutes) // 60_000_000_000
    cur, prev = _rollup_window(m_cur), _rollup_window(rr[0], m_cur)
    logs = {svc: int(e or 0) for svc, e in qrows("SELECT service, SUM(log_err) FROM env_rollup WHERE minute>=? GROUP BY service", (m_cur,))}
    out = {}
    for svc, c in cur.items():
        p = prev.get(svc)
        out[svc] = (c["n"], c["err"], c["p95"], p["p95"] if p and p["n"] else None, logs.get(svc, 0) / max(1, minutes))
    return out


def _syn_down(minutes):
    return q("SELECT COUNT(*) n FROM synthetic_runs WHERE ts>=? AND ok=0", (int(time.time() - minutes * 60),))[0]["n"]


@app.get("/api/services/{svc}/health")
def service_health(svc: str, minutes: float = 60):
    fast = _health_inputs_fast(minutes)
    if fast is not None:
        n, err, p95, p95_prev, log_rate = fast.get(svc, (0, 0, 0, None, 0))
        return _health_score(svc, n, err, p95, p95_prev, log_rate, _syn_down(minutes) if n else 0)
    t0 = since_ns(minutes)
    row = q("""SELECT COUNT(*) n, SUM(status=2) err, AVG(dur_ms) avg_ms FROM spans
               WHERE service=? AND start_ns>=? AND (parent_id='' OR kind=2)""", (svc, t0))[0]
    if not row["n"]:
        return {"service": svc, "score": None, "reason": "no traffic in window"}
    err_pct = 100 * (row["err"] or 0) / row["n"]
    dur = [x["dur_ms"] for x in q("""SELECT dur_ms FROM spans WHERE service=? AND start_ns>=?
                                     AND (parent_id='' OR kind=2) ORDER BY dur_ms""", (svc, t0))]
    p95 = dur[min(len(dur) - 1, int(len(dur) * .95))] if dur else 0
    # previous window baseline
    t_prev = t0 - int(float(minutes) * 60 * 1e9)
    dur_prev = [x["dur_ms"] for x in q("""SELECT dur_ms FROM spans WHERE service=? AND start_ns>=? AND start_ns<?
                                          AND (parent_id='' OR kind=2) ORDER BY dur_ms""", (svc, t_prev, t0))]
    p95_prev = dur_prev[min(len(dur_prev) - 1, int(len(dur_prev) * .95))] if dur_prev else p95
    drift = (p95 - p95_prev) / p95_prev if p95_prev else 0
    log_err_rate = q("SELECT COUNT(*) n FROM logs WHERE service=? AND ts_ns>=? AND severity>=17",
                     (svc, t0))[0]["n"] / max(1, minutes)
    # score components (each contributes up to the listed weight)
    score = 100.0
    reasons = []
    if err_pct > 1:
        p = min(40, err_pct * 4); score -= p
        reasons.append(f"error rate {err_pct:.1f}%")
    if drift > 0.2:
        p = min(25, drift * 50); score -= p
        reasons.append(f"p95 latency up {drift * 100:.0f}% vs prior window")
    if log_err_rate > 5:
        p = min(20, log_err_rate); score -= p
        reasons.append(f"{log_err_rate:.0f} error logs/min")
    syn_down = q("""SELECT COUNT(*) n FROM synthetic_runs
                    WHERE ts>=? AND ok=0""", (int(time.time() - minutes * 60),))[0]["n"]
    if syn_down > 0:
        score -= min(15, syn_down * 3)
        reasons.append(f"{syn_down} failed synthetic checks")
    score = max(0, round(score, 1))
    state = "healthy" if score >= 90 else "degraded" if score >= 70 else "unhealthy"
    return {"service": svc, "score": score, "state": state, "reason": "; ".join(reasons) or "all green",
            "signals": {"error_pct": round(err_pct, 2), "p95_ms": round(p95, 1),
                        "p95_drift_pct": round(drift * 100, 1), "log_err_per_min": round(log_err_rate, 1)}}


@app.get("/api/services-summary")
@ttl_cached
def services_summary(minutes: float = 60, buckets: int = Query(30, ge=1, le=200)):
    """Health score and trend sparkline for every service in one call. The Services page used to make two requests per service
    (60 for 30 services), each scanning raw spans. Returns {} when the rollups do not cover the window; the page then falls back."""
    fast = _health_inputs_fast(minutes)
    rr = _rollup_range(minutes)
    if fast is None or not rr:
        return {}
    syn = _syn_down(minutes)
    t0 = since_ns(minutes)
    w = int(minutes * 60 * 1e9 / buckets)
    series = {}
    for m, svc, n, e in qrows("SELECT minute, service, SUM(n), SUM(err) FROM span_rollup WHERE minute>=? GROUP BY minute, service", (rr[0],)):
        i = min(buckets - 1, max(0, int((m * 60_000_000_000 + 30_000_000_000 - t0) // w)))  # the minute in progress belongs in the last bucket
        if 0 <= i < buckets:
            a = series.setdefault(svc, {})
            o = a.get(i, (0, 0)); a[i] = (o[0] + int(n), o[1] + int(e or 0))
    out = {}
    for svc, (n, err, p95, p95_prev, log_rate) in fast.items():
        b = series.get(svc, {})
        out[svc] = {"health": _health_score(svc, n, err, p95, p95_prev, log_rate, syn),
                    "ts": [{"t": (t0 + i * w) / 1e6, "rate": b[i][0] / (w / 1e9) if i in b else 0,
                            "err": b[i][1] / (w / 1e9) if i in b else 0} for i in range(buckets)]}
    return out


# ---- SLOs (service-level objectives) -- feature gap vs dashboards-only competitors
@app.get("/api/slos")
def slos_list():
    rows = q("SELECT * FROM slos")
    for r in rows:
        t0 = time.time_ns() - int(r["window_days"] * 86400 * 1e9)
        if _rollup_range(r["window_days"] * 1440.0):
            # From the per-minute duration histogram (was: every span of the window, once per SLO, on every page via home/insights).
            total, err, fast = 0, 0, 0
            for b, n, e in qrows("SELECT b, SUM(n), SUM(err) FROM span_rollup WHERE service=? AND minute>=? GROUP BY b",
                                 (r["service"], t0 // 60_000_000_000)):
                total += int(n); err += int(e or 0)
                if _bucket_value(b) <= r["latency_ms"]:
                    fast += int(n)
            good = max(0, fast - err)
        else:
            tot = q("SELECT COUNT(*) n, SUM(status=2) err FROM spans WHERE service=? AND start_ns>=? AND (parent_id='' OR kind=2)",
                    (r["service"], t0))[0]
            d = [x["dur_ms"] for x in q("SELECT dur_ms FROM spans WHERE service=? AND start_ns>=? AND (parent_id='' OR kind=2)", (r["service"], t0))]
            good = max(0, sum(1 for x in d if x <= r["latency_ms"]) - (tot["err"] or 0))
            total = tot["n"] or 0
        attainment = 100 * good / total if total else None
        budget_used = max(0.0, (100 - attainment) / (100 - r["objective_pct"]) * 100) if attainment is not None and r["objective_pct"] < 100 else None  # share of the allowed failures already used
        r["attainment_pct"] = round(attainment, 3) if attainment is not None else None
        r["error_budget_used_pct"] = round(budget_used, 1) if budget_used is not None else None
        r["events"] = total
    return rows


@app.post("/api/slos")
async def slo_create(body: SloIn):
    _tier_check("slos", "slos")
    try:
        with db() as c:
            c.execute("INSERT INTO slos(name,service,objective_pct,latency_ms,window_days) VALUES(?,?,?,?,?)",
                      (body.name, body.service, body.objective_pct, body.latency_ms, body.window_days))
    except _IntegrityError:
        raise HTTPException(409, f"SLO named '{body.name}' already exists")
    return {"ok": True}


@app.delete("/api/slos/{sid}")
def slo_delete(sid: int):
    with db() as c:
        if not c.execute("SELECT 1 FROM slos WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "SLO not found")
        c.execute("DELETE FROM slos WHERE id=?", (sid,))
    return {"ok": True}


# ---- custom dashboards (saved panels on top of metric queries)
@app.get("/api/dashboards")
def dashboards_list():
    rows = q("SELECT * FROM dashboards ORDER BY name")
    for r in rows:
        r["panels"] = _as_list(r["panels"])
    return rows


@app.post("/api/dashboards")
async def dashboard_save(body: DashboardIn):
    existing = q("SELECT id FROM dashboards WHERE name=?", (body.name,))
    if not existing:
        _tier_check("dashboards", "dashboards")
    with db() as c:
        c.execute("""INSERT INTO dashboards(name,panels,updated) VALUES(?,?,?)
                     ON CONFLICT(name) DO UPDATE SET panels=excluded.panels, updated=excluded.updated""",
                  (body.name, json.dumps(body.panels), int(time.time())))
    return {"ok": True}


@app.delete("/api/dashboards/{did}")
def dashboard_delete(did: int):
    with db() as c:
        if not c.execute("SELECT 1 FROM dashboards WHERE id=?", (did,)).fetchone():
            raise HTTPException(404, "dashboard not found")
        c.execute("DELETE FROM dashboards WHERE id=?", (did,))
    return {"ok": True}


# ---- alerts


def eval_signal(signal, expr, service, window_s):
    t0 = time.time_ns() - int(window_s * 1e9)
    sv, sa = (" AND service=?", [service]) if service else ("", [])
    if signal == "error_rate":
        r = q(f"SELECT COUNT(*) n, SUM(status=2) e FROM spans WHERE start_ns>=? AND (parent_id='' OR kind=2){sv}", (t0, *sa))[0]
        return (100.0 * (r["e"] or 0) / r["n"]) if r["n"] else 0.0
    if signal == "request_rate":
        r = q(f"SELECT COUNT(*) n FROM spans WHERE start_ns>=? AND (parent_id='' OR kind=2){sv}", (t0, *sa))[0]
        return r["n"] / window_s
    if signal == "p95_latency":
        d = [x["dur_ms"] for x in q(f"SELECT dur_ms FROM spans WHERE start_ns>=? AND (parent_id='' OR kind=2){sv} ORDER BY dur_ms", (t0, *sa))]
        return d[min(len(d) - 1, int(len(d) * .95))] if d else 0.0
    if signal == "log_errors":
        return float(q(f"SELECT COUNT(*) n FROM logs WHERE ts_ns>=? AND severity>=17{sv}" + (" AND body LIKE ?" if expr else ""),
                       (t0, *sa, *([f"%{expr}%"] if expr else [])))[0]["n"])
    if signal in ("metric_avg", "metric_max") and expr in BUILTIN_METRICS:
        mins = max(1.0, window_s / 60.0)
        if signal == "metric_avg":       # the whole window as one bucket
            vals = [v for vs in _builtin_series(expr, service, mins, 1)["series"].values() for v in vs if v is not None]
            return max(vals) if vals else 0.0          # worst service when no service is set
        vals = [v for vs in _builtin_series(expr, service, mins, max(1, int(mins)))["series"].values() for v in vs if v is not None]
        return max(vals) if vals else 0.0
    if signal in ("metric_avg", "metric_max"):
        fn = "AVG" if signal == "metric_avg" else "MAX"
        r = q(f"SELECT {fn}(value) v FROM metrics WHERE name=? AND ts_ns>=?{sv}", (expr, t0, *sa))[0]
        return r["v"] if r["v"] is not None else 0.0
    if signal == "synthetic_down":
        r = q("SELECT COUNT(*) n FROM synthetic_runs WHERE ok=0 AND ts>=?" + (" AND check_id=?" if expr else ""),
              (int(time.time()) - window_s, *([int(expr)] if expr else [])))[0]
        return float(r["n"])
    return 0.0


OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b}


def notify(url, payload):
    if not url:
        return
    try:
        blocked = _blocked_dest(url)
        if blocked:
            print("webhook blocked:", blocked)
            return
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        _urlopen(req, timeout=8).read()
    except Exception as e:
        print("webhook failed:", e)


def evaluate_rules():
    for r in q("SELECT * FROM alert_rules WHERE enabled=1"):
        v = eval_signal(r["signal"], r["expr"], r["service"], r["window_s"])
        firing = OPS.get(r["op"], OPS[">"])(v, r["threshold"])
        new = "firing" if firing else "ok"
        now = int(time.time())
        with db() as c:
            c.execute("UPDATE alert_rules SET last_value=?, last_eval=?, state=?, since=CASE WHEN state<>? THEN ? ELSE since END WHERE id=?",
                      (v, now, new, new, now, r["id"]))
            if new != r["state"]:
                msg = f"{r['name']}: {r['signal']} {v:.2f} {r['op']} {r['threshold']}" if firing else f"{r['name']} recovered ({v:.2f})"
                c.execute("INSERT INTO alert_events(rule_id,ts,state,value,msg) VALUES(?,?,?,?,?)", (r["id"], now, new, v, msg))
                threading.Thread(target=notify, args=(r["webhook"], {"text": f"[NebuLens] {new.upper()} - {msg}", "state": new, "rule": r["name"], "value": v}), daemon=True).start()


@app.get("/api/alerts")
def alerts():
    return q("SELECT * FROM alert_rules ORDER BY id")


@app.post("/api/alerts")
async def alert_create(body: AlertRuleIn):
    _tier_check("alerts", "alert_rules")
    with db() as c:
        c.execute("INSERT INTO alert_rules(name,signal,expr,op,threshold,window_s,service,webhook) VALUES(?,?,?,?,?,?,?,?)",
                  (body.name, body.signal, body.expr, body.op, body.threshold,
                   body.window_s, body.service, body.webhook))
    return {"ok": True}


@app.delete("/api/alerts/{rid}")
def alert_delete(rid: int):
    with db() as c:
        if not c.execute("SELECT 1 FROM alert_rules WHERE id=?", (rid,)).fetchone():
            raise HTTPException(404, "alert rule not found")
        c.execute("DELETE FROM alert_rules WHERE id=?", (rid,))
        c.execute("DELETE FROM alert_events WHERE rule_id=? OR rule_id NOT IN (SELECT id FROM alert_rules)", (rid,))
    return {"ok": True}


@app.post("/api/alerts/{rid}/toggle")
def alert_toggle(rid: int):
    with db() as c:
        if not c.execute("SELECT 1 FROM alert_rules WHERE id=?", (rid,)).fetchone():
            raise HTTPException(404, "alert rule not found")
        c.execute("UPDATE alert_rules SET enabled=1-enabled WHERE id=?", (rid,))
    return {"ok": True}


@app.get("/api/alert-events")
def alert_events():
    return q("SELECT e.*, r.name FROM alert_events e LEFT JOIN alert_rules r ON r.id=e.rule_id ORDER BY e.id DESC LIMIT 100")


# ---- synthetics
def run_check(ch):
    t = time.time(); ok, status, err, ssl_days = 0, 0, "", None
    blocked = _blocked_dest(ch["url"])
    if blocked:
        with db() as c:
            c.execute("INSERT INTO synthetic_runs(check_id,ts,ok,status,ms,ssl_days,err) VALUES(?,?,?,?,?,?,?)", (ch["id"], int(time.time()), 0, 0, 0.0, None, blocked))
        return
    try:
        req = urllib.request.Request(ch["url"], headers={"User-Agent": "NebuLens-Synthetic/1.0"})
        try:
            resp = _urlopen(req, timeout=15); status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        ms = (time.time() - t) * 1000
        ok = int(status == ch["expect_status"] and ms <= ch["max_ms"])
        if not ok:
            err = f"status {status}" if status != ch["expect_status"] else f"slow {ms:.0f}ms > {ch['max_ms']:.0f}ms"
    except Exception as e:
        ms = (time.time() - t) * 1000; err = str(e)[:200]
    u = urlparse(ch["url"])
    if u.scheme == "https":
        try:
            with socket.create_connection((u.hostname, u.port or 443), timeout=8) as s:
                with ssl.create_default_context().wrap_socket(s, server_hostname=u.hostname) as ss:
                    exp = ssl.cert_time_to_seconds(ss.getpeercert()["notAfter"])
                    ssl_days = (exp - time.time()) / 86400
                    if ssl_days < 7 and ok:
                        ok, err = 0, f"certificate expires in {ssl_days:.1f}d"
        except Exception:
            pass
    with db() as c:
        c.execute("INSERT INTO synthetic_runs(check_id,ts,ok,status,ms,ssl_days,err) VALUES(?,?,?,?,?,?,?)", (ch["id"], int(time.time()), ok, status, ms, ssl_days, err))


@app.get("/api/synthetics")
def synthetics():
    rows = q("SELECT * FROM synthetics ORDER BY id")
    for r in rows:
        runs = q("SELECT * FROM synthetic_runs WHERE check_id=? ORDER BY ts DESC LIMIT 40", (r["id"],))
        r["last"] = runs[0] if runs else None
        r["history"] = list(reversed(runs))
        r["uptime"] = (100 * sum(x["ok"] for x in runs) / len(runs)) if runs else None
    return rows


@app.post("/api/synthetics")
async def synth_create(body: SyntheticIn):
    _tier_check("synthetics", "synthetics")
    with db() as c:
        c.execute("INSERT INTO synthetics(name,url,interval_s,expect_status,max_ms) VALUES(?,?,?,?,?)",
                  (body.name, body.url, body.interval_s, body.expect_status, body.max_ms))
    return {"ok": True}


@app.delete("/api/synthetics/{cid}")
def synth_delete(cid: int):
    with db() as c:
        if not c.execute("SELECT 1 FROM synthetics WHERE id=?", (cid,)).fetchone():
            raise HTTPException(404, "synthetic not found")
        c.execute("DELETE FROM synthetics WHERE id=?", (cid,))
        c.execute("DELETE FROM synthetic_runs WHERE check_id=?", (cid,))
    return {"ok": True}


@app.post("/api/synthetics/{cid}/run")
def synth_run(cid: int):
    ch = q("SELECT * FROM synthetics WHERE id=?", (cid,))
    if not ch:
        raise HTTPException(404)
    run_check(ch[0])
    return {"ok": True}


# ---- environments (namespaces, clusters, deployment environments)
_ENV_SCOPED_PATHS = ("/api/environments", "/api/logs", "/api/services", "/api/traces", "/api/metrics",
                     "/api/mode", "/api/auth", "/api/stats")


class EnvIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=1, max_length=120)
    name: str = Field(default="", max_length=120)
    color: str = Field(default="", max_length=32)


class EnvAccessIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grp: str = Field(min_length=1, max_length=200)
    slugs: list[str] = Field(default_factory=list, max_length=200)


@app.get("/api/redaction")
def api_redaction():
    """What redaction is active and how much it has removed since start. Evidence for a reviewer
    that sensitive values are dropped before storage."""
    return {"enabled": not REDACT_OFF,
            "rules": [n for n, _, _ in _REDACT_RULES],
            "redacted_since_start": dict(_redact_counts),
            "blank_log_lines_dropped_since_start": _blank_dropped[0],
            "log_min_severity": MIN_LOG_SEVERITY,
            "logs_below_min_severity_dropped_since_start": _below_min_dropped[0],
            "note": "Redaction runs at ingest. Values already stored before a rule was enabled are not changed."}


@app.get("/api/environments")
@ttl_cached
def environments(minutes: float = 60):
    """Connected environments with live activity. Restricted callers see only their own."""
    t0 = since_ns(minutes)
    scope, sargs = env_scope()
    rows = q("SELECT * FROM environments ORDER BY slug")
    allowed = _cur_envs.get()
    if allowed is not None:
        rows = [r for r in rows if r["slug"] in allowed]
    if _env_ready(minutes):
        return _environments_out(rows, *_environments_fast(minutes, t0, scope, sargs))
    spans = {r["env"]: r for r in q(
        f"SELECT env, COUNT(*) n, SUM(status=2) err, COUNT(DISTINCT service) services FROM spans WHERE start_ns>=?{scope} GROUP BY env",
        (t0, *sargs))}
    logs_ = {r["env"]: r for r in q(
        f"SELECT env, COUNT(*) n, SUM(severity>=17) err FROM logs WHERE ts_ns>=?{scope} GROUP BY env", (t0, *sargs))}
    # Services seen in ANY signal. Counting only spans showed "0 services" for every environment
    # that sends logs but no traces yet.
    svc_counts = {r["env"]: r["n"] for r in q(
        f"""SELECT env, COUNT(DISTINCT service) n FROM (
              SELECT env, service FROM spans WHERE start_ns>=?{scope}
              UNION SELECT env, service FROM logs WHERE ts_ns>=?{scope}) GROUP BY env""",
        (t0, *sargs, t0, *sargs))}
    # Namespaces inside each environment, with a service count per namespace, from recent telemetry.
    # Namespaces are a property of an environment here, not environments in their own right.
    ns_by_env = {}
    for r in q(f"""SELECT env, ns, COUNT(DISTINCT service) services FROM (
                     SELECT env, service, COALESCE(json_extract(res,'$."k8s.namespace.name"'),
                            json_extract(res,'$."service.namespace"')) ns FROM spans WHERE start_ns>=?{scope}
                     UNION
                     SELECT env, service, COALESCE(json_extract(res,'$."k8s.namespace.name"'),
                            json_extract(res,'$."service.namespace"')) ns FROM logs WHERE ts_ns>=?{scope}
                   ) WHERE ns IS NOT NULL AND ns<>'' GROUP BY env, ns ORDER BY services DESC""",
                 (t0, *sargs, t0, *sargs)):
        ns_by_env.setdefault(r["env"], []).append({"namespace": r["ns"], "services": r["services"]})
    return _environments_out(rows, spans, logs_, svc_counts, ns_by_env)


def _environments_fast(minutes, t0, scope, sargs):
    """The same four aggregates from env_rollup and resource_seen: a few thousand small rows instead of every span and log."""
    m0 = t0 // 60_000_000_000
    spans, logs_, svc_counts = {}, {}, {}
    for env, n, e, ln, le, ns_ in qrows(f"SELECT env, SUM(spans), SUM(span_err), SUM(logs), SUM(log_err), COUNT(DISTINCT service) "
                                        f"FROM env_rollup WHERE minute>=?{scope} GROUP BY env", (m0, *sargs)):
        spans[env] = {"n": int(n or 0), "err": int(e or 0)}
        logs_[env] = {"n": int(ln or 0), "err": int(le or 0)}
        svc_counts[env] = int(ns_ or 0)
    ns_by_env = {}
    if _res_ready(minutes):
        for env, ns, n in qrows(f"SELECT env, ns, COUNT(DISTINCT service) FROM resource_seen WHERE last_seen>=? AND sig IN ('spans','logs') "
                                f"AND ns<>''{scope} GROUP BY env, ns ORDER BY 3 DESC", (int(t0 // 1_000_000_000), *sargs)):
            ns_by_env.setdefault(env, []).append({"namespace": ns, "services": int(n)})
    return spans, logs_, svc_counts, ns_by_env


def _environments_out(rows, spans, logs_, svc_counts, ns_by_env):
    out = []
    for r in rows:
        sp, lg = spans.get(r["slug"], {}), logs_.get(r["slug"], {})
        n, err = sp.get("n", 0) or 0, sp.get("err", 0) or 0
        namespaces = ns_by_env.get(r["slug"], [])
        out.append(dict(r, spans=n, span_errors=err, err_pct=(100 * err / n) if n else 0.0,
                        services=svc_counts.get(r["slug"], 0) or 0, logs=lg.get("n", 0) or 0,
                        log_errors=lg.get("err", 0) or 0,
                        namespaces=namespaces, namespace_count=len(namespaces),
                        unnamed=(r["slug"] == "default" and not r["cluster"]),
                        active=bool(n or lg.get("n")),
                        stale_minutes=(int(time.time()) - (r["last_seen"] or 0)) / 60))
    return out


class ViewBody(BaseModel):
    name: str
    kind: str
    params: dict = {}
    description: str = ""


@app.get("/api/views")
def views_list(kind: str = ""):
    """List built-in and user-saved views, optionally filtered by kind (logs / errors / traces / metrics)."""
    _seed_builtin_views()
    where, args = "", []
    if kind:
        where, args = " WHERE kind=?", [kind]
    rows = q(f"SELECT id, slug, name, kind, params, description, builtin, favorite, updated FROM views{where} "
             "ORDER BY favorite DESC, builtin DESC, name", args)
    out = []
    for r in rows:
        try:
            params = _as_dict(r["params"])
        except Exception:
            params = {}
        out.append({"id": r["id"], "slug": r["slug"], "name": r["name"], "kind": r["kind"],
                    "params": params, "description": r["description"],
                    "builtin": bool(r["builtin"]), "favorite": bool(r["favorite"]), "updated": r["updated"]})
    return out


def _slugify(text, existing):
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "view"
    slug, n = base, 1
    while slug in existing:
        n += 1; slug = f"{base}-{n}"
    return slug


@app.post("/api/views")
def views_create(body: ViewBody):
    """Save the current filter state as a named view. Pro and admin only, matching dashboards."""
    if IS_PRO:
        _tier_check("views", "dashboards")
    if body.kind not in ("logs", "errors", "traces", "metrics"):
        raise HTTPException(400, "kind must be logs, errors, traces or metrics")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    existing = {r["slug"] for r in q("SELECT slug FROM views")}
    slug = _slugify(name, existing)
    with db() as c:
        c.execute("INSERT INTO views(slug, name, kind, params, description, builtin, updated) VALUES(?,?,?,?,?,0,?)",
                  (slug, name[:80], body.kind, json.dumps(body.params or {}), (body.description or "")[:300], int(time.time())))
    return {"ok": True, "slug": slug}


@app.delete("/api/views/{vid}")
def views_delete(vid: int):
    """Delete a view. A built-in deletion is remembered so the seed does not re-create it."""
    row = q("SELECT slug, builtin FROM views WHERE id=?", (vid,))
    if not row:
        raise HTTPException(404, "not found")
    with db() as c:
        c.execute("DELETE FROM views WHERE id=?", (vid,))
        if row[0]["builtin"]:
            c.execute("INSERT INTO view_tombstones(slug) VALUES(?) ON CONFLICT(slug) DO NOTHING", (row[0]["slug"],))
    return {"ok": True}


@app.post("/api/views/{vid}/favorite")
def views_favorite(vid: int):
    """Toggle whether this view is pinned to the top of its kind."""
    row = q("SELECT favorite FROM views WHERE id=?", (vid,))
    if not row:
        raise HTTPException(404, "not found")
    with db() as c:
        c.execute("UPDATE views SET favorite=? WHERE id=?", (0 if row[0]["favorite"] else 1, vid))
    return {"ok": True, "favorite": not row[0]["favorite"]}


@app.get("/api/rum/summary")
def rum_summary(minutes: float = 60, env: str = "", service: str = ""):
    """What the 'Browser' page needs in one call. Reads RUM page_view events out of the logs table."""
    t0 = since_ns(minutes)
    args = [t0]; where = "ts_ns>=?"
    if service: where += " AND service=?"; args.append(service)
    if env:     where += " AND env=?"; args.append(env)
    # Only the page_view events the SDK emits. The attribute name is set by the SDK.
    rows = q(f"SELECT ts_ns, service, body, attrs FROM logs WHERE {where} AND severity>=9 "
             f"ORDER BY ts_ns DESC LIMIT 10000", args)
    views = []
    err_count = 0
    for r in rows:
        a = _as_dict(r["attrs"])
        if a.get("event.name") == "page_view":
            views.append({"ts": r["ts_ns"], "service": r["service"], "path": a.get("url.path") or "/",
                          "load_ms": a.get("rum.load_ms"), "lcp_ms": a.get("rum.lcp_ms"),
                          "cls": a.get("rum.cls"), "inp_ms": a.get("rum.inp_ms"),
                          "fcp_ms": a.get("rum.fcp_ms"), "ttfb_ms": a.get("rum.ttfb_ms")})
    err_rows = q(f"SELECT COUNT(*) n FROM logs WHERE {where} AND severity>=17", args)
    err_count = err_rows[0]["n"] if err_rows else 0

    def pct(vals, p):
        xs = sorted(v for v in vals if isinstance(v, (int, float)) and v >= 0)
        if not xs:
            return None
        k = max(0, min(len(xs) - 1, int(len(xs) * p / 100)))
        return xs[k]

    paths = {}
    for v in views:
        p_ = paths.setdefault(v["path"], {"path": v["path"], "views": 0, "load_ms": [], "lcp_ms": []})
        p_["views"] += 1
        if v["load_ms"] is not None: p_["load_ms"].append(v["load_ms"])
        if v["lcp_ms"] is not None:  p_["lcp_ms"].append(v["lcp_ms"])
    slowest = sorted(paths.values(), key=lambda p: -(pct(p["load_ms"], 95) or 0))[:10]
    for p_ in slowest:
        p_["load_p50"] = pct(p_["load_ms"], 50); p_["load_p95"] = pct(p_["load_ms"], 95)
        p_["lcp_p75"] = pct(p_["lcp_ms"], 75)
        p_.pop("load_ms"); p_.pop("lcp_ms")

    all_load = [v["load_ms"] for v in views if v["load_ms"] is not None]
    all_lcp  = [v["lcp_ms"]  for v in views if v["lcp_ms"]  is not None]
    all_cls  = [v["cls"]     for v in views if v["cls"]     is not None]
    all_inp  = [v["inp_ms"]  for v in views if v["inp_ms"]  is not None]
    return {
        "views": len(views), "errors": err_count,
        "error_rate_pct": round(100 * err_count / max(1, len(views)), 2),
        "load_p50": pct(all_load, 50), "load_p95": pct(all_load, 95),
        "lcp_p75": pct(all_lcp, 75), "cls_p75": pct(all_cls, 75), "inp_p75": pct(all_inp, 75),
        "slowest_paths": slowest,
    }


@app.get("/api/environments/discover")
@ttl_cached
def environments_discover(minutes: float = 1440):
    """Namespaces/clusters seen in telemetry, including any not registered yet (none, normally:
    environments register themselves on first ingest). Useful right after connecting a new cluster."""
    t0 = since_ns(minutes)
    scope, sargs = env_scope()
    if _env_ready(minutes):
        # From the per-minute counts: the Environments page asks for a whole day, which was a full scan of a day of spans and logs.
        agg, unassigned = {}, 0
        for env, n in qrows(f"SELECT env, SUM(spans) + SUM(logs) FROM env_rollup WHERE minute>=?{scope} GROUP BY env",
                            (t0 // 60_000_000_000, *sargs)):
            if env:
                agg[env] = int(n or 0)
        r = qrows(f"SELECT SUM(spans) FROM env_rollup WHERE minute>=? AND env=''{scope}", (t0 // 60_000_000_000, *sargs))
        unassigned = int((r[0][0] if r else 0) or 0)
        known = {r["slug"] for r in q("SELECT slug FROM environments")}
        return {"environments": [{"slug": k, "records": v, "registered": k in known} for k, v in sorted(agg.items()) if v],
                "unassigned_spans": unassigned}
    seen = q(f"SELECT env, COUNT(*) n FROM spans WHERE start_ns>=? AND env<>''{scope} GROUP BY env", (t0, *sargs))
    seen += q(f"SELECT env, COUNT(*) n FROM logs WHERE ts_ns>=? AND env<>''{scope} GROUP BY env", (t0, *sargs))
    agg = {}
    for r in seen:
        agg[r["env"]] = agg.get(r["env"], 0) + (r["n"] or 0)
    known = {r["slug"] for r in q("SELECT slug FROM environments")}
    unknown_rows = q(f"SELECT COUNT(*) n FROM spans WHERE start_ns>=? AND env=''{scope}", (t0, *sargs))
    return {"environments": [{"slug": k, "records": v, "registered": k in known} for k, v in sorted(agg.items())],
            "unassigned_spans": unknown_rows[0]["n"] if unknown_rows else 0}


@app.post("/api/environments")
def environment_save(body: EnvIn):
    """Rename / recolor an environment, or pre-register one before its first data arrives."""
    now = int(time.time())
    with db() as c:
        c.execute("""INSERT INTO environments(slug,name,color,source,first_seen,last_seen)
                     VALUES(?,?,?,'manual',?,0)
                     ON CONFLICT(slug) DO UPDATE SET name=excluded.name, color=excluded.color""",
                  (body.slug, body.name or body.slug, body.color, now))
    with _env_lock:
        _env_known.add(body.slug)
    return {"ok": True}


@app.delete("/api/environments/{slug}")
def environment_delete(slug: str):
    """Forget an environment. Its telemetry is untouched; the row reappears if data keeps arriving."""
    with db() as c:
        c.execute("DELETE FROM environments WHERE slug=?", (slug,))
        c.execute("DELETE FROM env_access WHERE slug=?", (slug,))
    with _env_lock:
        _env_known.discard(slug)
    return {"ok": True}


_purging = {}   # slug -> started (unix s) while its data is being deleted


def _purge_env(slug, db_path):
    """Delete every span, log and metric of one environment, plus its summary rows, in small batches so ingest and the
    dashboards keep running. Runs in the background; the environment disappears from the list straight away."""
    tok = _cur_db.set(db_path)
    try:
        for table in ("spans", "logs", "metrics"):
            if USE_PG:
                with db() as c:
                    c.execute(f"DELETE FROM {table} WHERE env=?", (slug,))
                continue
            while True:
                with db() as c:
                    n = c.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE env=? LIMIT 20000)",
                                  (slug,)).rowcount
                if n < 20000:
                    break
                time.sleep(0.05)
        with db() as c:
            for table in ("span_rollup", "env_rollup", "resource_seen", "services_seen", "environments", "env_access"):
                c.execute(f"DELETE FROM {table} WHERE {'slug' if table in ('environments', 'env_access') else 'env'}=?", (slug,))
        print(f"environment {slug!r}: data deleted", flush=True)
    except Exception as e:
        print(f"environment {slug!r}: deleting data stopped ({e}); run the delete again to finish", flush=True)
    finally:
        _purging.pop(slug, None)
        _cur_db.reset(tok)


@app.post("/api/environments/{slug}/purge")
def environment_purge(slug: str):
    """Admin: remove an environment AND permanently delete its telemetry (spans, logs, metrics and summaries). Cannot be
    undone. Telemetry that keeps arriving for it afterwards creates it again."""
    if not slug or len(slug) > 200:
        raise HTTPException(400, "bad environment")
    known = q("SELECT 1 FROM environments WHERE slug=?", (slug,)) or qrows("SELECT 1 FROM env_rollup WHERE env=? LIMIT 1", (slug,))
    if not known:
        raise HTTPException(404, "environment not found")
    if slug in _purging:
        return {"ok": True, "status": "already deleting"}
    with db() as c:
        c.execute("DELETE FROM environments WHERE slug=?", (slug,))
    with _env_lock:
        _env_known.discard(slug)
    _purging[slug] = int(time.time())
    threading.Thread(target=_purge_env, args=(slug, _cur_db.get()), daemon=True).start()
    return {"ok": True, "status": "deleting"}


@app.get("/api/environments/access")
def environment_access():
    """Pro: which SSO groups are limited to which environments. A group with no row sees everything."""
    _tier_gate("Per-environment access control", "enterprise")
    rows = q("SELECT grp, slug FROM env_access ORDER BY grp, slug")
    by = {}
    for r in rows:
        by.setdefault(r["grp"], []).append(r["slug"])
    return {"access": [{"grp": g, "slugs": v} for g, v in by.items()],
            "sso_enabled": sso_enabled(),
            "note": "Groups come from the OIDC claim configured in NEBULENS_OIDC_GROUPS_CLAIM. "
                    "A group with no entry here can see every environment."}


@app.put("/api/environments/access")
def environment_access_set(body: EnvAccessIn):
    _tier_gate("Per-environment access control", "enterprise")
    with db() as c:
        c.execute("DELETE FROM env_access WHERE grp=?", (body.grp,))
        for sl in body.slugs:
            c.execute("INSERT INTO env_access(grp,slug) VALUES(?,?)", (body.grp, sl))
    return {"ok": True, "grp": body.grp, "slugs": body.slugs}


# ---------------------------------------------------------------- clusters (one console, many clusters)
class ClusterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    note: str = Field(default="", max_length=200)


class ClusterPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, min_length=1, max_length=60)
    note: Optional[str] = Field(default=None, max_length=200)
    disabled: Optional[bool] = None


def _new_cluster_token():
    return CLUSTER_TOKEN_PREFIX + secrets.token_urlsafe(32)


def _no_clusters_in_hosted():
    if HOSTED:
        raise HTTPException(404, {"error": "not_available",
                                  "message": "Clusters are managed with organizations and projects in hosted mode."})


@app.get("/api/clusters")
def clusters_list():
    """Registered clusters with live status. Admin only: this is where ingest credentials are managed."""
    _no_clusters_in_hosted()
    rows = q("SELECT slug,name,token_prefix,note,disabled,created,last_seen FROM clusters ORDER BY name")
    now, t0 = int(time.time()), since_ns(60)
    logs_, spans_ = {}, {}
    if rows:
        ph = ",".join("?" * len(rows))
        slugs = [r["slug"] for r in rows]
        if _env_ready(60):    # per-minute counts written at ingest (was: COUNT(*) over raw spans and logs on every call)
            for env, sp, lg in qrows(f"SELECT env, SUM(spans), SUM(logs) FROM env_rollup WHERE minute>=? AND env IN ({ph}) GROUP BY env",
                                     (t0 // 60_000_000_000, *slugs)):
                spans_[env], logs_[env] = int(sp or 0), int(lg or 0)
        else:
            logs_ = {r["env"]: r["n"] for r in q(
                f"SELECT env, COUNT(*) n FROM logs WHERE ts_ns>=? AND env IN ({ph}) GROUP BY env", (t0, *slugs))}
            spans_ = {r["env"]: r["n"] for r in q(
                f"SELECT env, COUNT(*) n FROM spans WHERE start_ns>=? AND env IN ({ph}) GROUP BY env", (t0, *slugs))}
    out = []
    for r in rows:
        ls = r["last_seen"] or 0
        status = "disabled" if r["disabled"] else "waiting" if not ls else "live" if now - ls < 300 else "idle"
        out.append({"slug": r["slug"], "name": r["name"], "note": r["note"] or "", "disabled": bool(r["disabled"]),
                    "status": status, "created": r["created"], "last_seen": ls, "token_prefix": r["token_prefix"] or "",
                    "logs_1h": logs_.get(r["slug"], 0), "spans_1h": spans_.get(r["slug"], 0)})
    return {"clusters": out, "this_cluster": CLUSTER_NAME,
            "ingest_auth_required": bool(AUTH_TOKEN or INGEST_TOKEN),
            "token_prefix": CLUSTER_TOKEN_PREFIX}


@app.post("/api/clusters")
def cluster_create(body: ClusterIn):
    """Register a cluster and return its ingest token. The token is shown here once and never again."""
    _no_clusters_in_hosted()
    _tier_check("clusters", "clusters")
    name = body.name.strip()
    slug = _cluster_slug(name)
    if not slug:
        raise HTTPException(400, "The cluster name must contain letters or digits.")
    if q("SELECT 1 FROM clusters WHERE slug=?", (slug,)):
        raise HTTPException(409, f"A cluster called '{slug}' already exists.")
    tok, now = _new_cluster_token(), int(time.time())
    try:
        with db() as c:
            c.execute("INSERT INTO clusters(slug,name,token_hash,token_prefix,note,disabled,created,last_seen) "
                      "VALUES(?,?,?,?,?,0,?,0)", (slug, name, _hash_token(tok), tok[:8], body.note.strip(), now))
            c.execute("""INSERT INTO environments(slug,name,cluster,kind,source,first_seen,last_seen)
                         VALUES(?,?,?,'cluster','manual',?,0)
                         ON CONFLICT(slug) DO UPDATE SET name=excluded.name, cluster=excluded.cluster, kind='cluster'""",
                      (slug, name, name, now))
    except _IntegrityError:
        raise HTTPException(409, f"A cluster called '{slug}' already exists.")
    with _env_lock:
        _env_known.add(slug)
    _cluster_cache_clear()
    return {"slug": slug, "name": name, "token": tok,
            "note": "Copy this token now. NebuLens stores only a hash of it and cannot show it again."}


@app.patch("/api/clusters/{slug}")
def cluster_update(slug: str, body: ClusterPatch):
    _no_clusters_in_hosted()
    if not q("SELECT 1 FROM clusters WHERE slug=?", (slug,)):
        raise HTTPException(404, "No such cluster.")
    with db() as c:
        if body.name is not None:
            c.execute("UPDATE clusters SET name=? WHERE slug=?", (body.name.strip(), slug))
            c.execute("UPDATE environments SET name=?, cluster=? WHERE slug=?", (body.name.strip(), body.name.strip(), slug))
        if body.note is not None:
            c.execute("UPDATE clusters SET note=? WHERE slug=?", (body.note.strip(), slug))
        if body.disabled is not None:
            c.execute("UPDATE clusters SET disabled=? WHERE slug=?", (1 if body.disabled else 0, slug))
    _cluster_cache_clear()
    return {"ok": True}


@app.post("/api/clusters/{slug}/rotate")
def cluster_rotate(slug: str):
    """Issue a new token. The old one stops working within seconds, so update the cluster's agent right away."""
    _no_clusters_in_hosted()
    if not q("SELECT 1 FROM clusters WHERE slug=?", (slug,)):
        raise HTTPException(404, "No such cluster.")
    tok = _new_cluster_token()
    with db() as c:
        c.execute("UPDATE clusters SET token_hash=?, token_prefix=? WHERE slug=?", (_hash_token(tok), tok[:8], slug))
    _cluster_cache_clear()
    return {"slug": slug, "token": tok,
            "note": "Copy this token now. The previous token for this cluster no longer works."}


@app.delete("/api/clusters/{slug}")
def cluster_delete(slug: str):
    """Remove the cluster's credentials. Telemetry already stored stays; the cluster simply can no longer send."""
    _no_clusters_in_hosted()
    with db() as c:
        c.execute("DELETE FROM clusters WHERE slug=?", (slug,))
    _cluster_cache_clear()
    return {"ok": True}


def sev_name(n):
    """OTLP severity number -> the level name shown in the UI."""
    n = int(n or 0)
    for lo, name in ((21, "FATAL"), (17, "ERROR"), (13, "WARN"), (9, "INFO"), (5, "DEBUG"), (1, "TRACE")):
        if n >= lo:
            return name
    return "UNSET"


# ---- log patterns / grouping / export (the "thousands of errors are really 5 problems" view)
_PAT_RULES = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "<hash>"),
    (re.compile(r"/[\w.\-/]{3,}"), "<path>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|m|h|KB|MB|GB)\b"), "<qty>"),
    (re.compile(r"\b\d+\b"), "<n>"),
]


def _pattern_of(body):
    """Collapse a log line to its shape: the first line with variable parts replaced. Two lines that
    differ only by an id, timestamp or number share a pattern."""
    line = (body or "").split("\n", 1)[0][:300]
    for rx, rep in _PAT_RULES:
        line = rx.sub(rep, line)
    return re.sub(r"\s+", " ", line).strip()


@app.get("/api/logs/patterns")
def logs_patterns(minutes: float = 60, service: str = "", min_severity: int = 0, q_: str = "",
                  env: str = "", limit: int = 2000, top: int = 40, start_ms: float = 0, end_ms: float = 0):
    """Group similar messages so a flood of near-identical errors collapses into a handful of rows."""
    rows = logs(minutes=minutes, service=service, min_severity=min_severity, q_=q_, limit=max(1, min(limit, 20000)), env=env,
                start_ms=start_ms, end_ms=end_ms)
    groups = {}
    for r in rows:
        k = _pattern_of(r.get("body", ""))
        g = groups.get(k)
        if g is None:
            g = groups[k] = {"pattern": k, "count": 0, "services": set(), "errors": 0,
                             "first_ts": r["ts_ns"], "last_ts": r["ts_ns"], "example": r.get("body", "")[:600],
                             "max_severity": 0, "sev_text": "", "levels": collections.Counter()}
        g["count"] += 1
        g["levels"][sev_name(r.get("severity") or 0)] += 1
        g["services"].add(r.get("service", ""))
        g["errors"] += 1 if (r.get("severity") or 0) >= 17 else 0
        g["first_ts"] = min(g["first_ts"], r["ts_ns"]); g["last_ts"] = max(g["last_ts"], r["ts_ns"])
        if (r.get("severity") or 0) > g["max_severity"]:
            g["max_severity"] = r.get("severity") or 0
            g["sev_text"] = r.get("sev_text") or sev_name(r.get("severity") or 0)
    out = sorted(groups.values(), key=lambda g: -g["count"])[:max(1, min(top, 200))]
    for g in out:
        g["services"] = sorted(x for x in g["services"] if x)
        g["levels"] = dict(g["levels"])
    return {"patterns": out, "scanned": len(rows), "distinct": len(groups),
            "truncated": len(rows) >= min(limit, 20000)}


@app.get("/api/errors")
@ttl_cached
def errors_list(minutes: float = 60, service: str = "", env: str = "", q_: str = "",
                sort: str = "count", new_within_hours: float = 0, top: int = 50, buckets: int = 24):
    """Group error-level (ERROR and above) logs by message pattern, with sparkline and first/last seen.

    sort = count (default) | last_seen | first_seen | new (groups whose first_seen is within new_within_hours)
    """
    rows = logs(minutes=minutes, service=service, min_severity=17, q_=q_, limit=10000, env=env)   # newest 10k error logs
    if not rows:
        return {"errors": [], "scanned": 0, "distinct": 0, "window_minutes": minutes}
    now_ns = time.time_ns()
    t0 = int(now_ns - minutes * 60e9)
    span_ns = max(1, now_ns - t0)
    bucket_ns = max(1, span_ns // max(1, buckets))
    groups = {}
    for r in rows:
        k = _pattern_of(r.get("body", ""))
        g = groups.get(k)
        if g is None:
            g = groups[k] = {"pattern": k, "count": 0, "services": set(), "first_ts": r["ts_ns"], "last_ts": r["ts_ns"],
                             "example": r.get("body", "")[:600], "sev_text": r.get("sev_text") or sev_name(r.get("severity") or 0),
                             "max_severity": r.get("severity") or 17, "trace_id": r.get("trace_id", ""),
                             "sparkline": [0] * buckets}
        g["count"] += 1
        g["services"].add(r.get("service", ""))
        g["first_ts"] = min(g["first_ts"], r["ts_ns"]); g["last_ts"] = max(g["last_ts"], r["ts_ns"])
        if (r.get("severity") or 0) > g["max_severity"]:
            g["max_severity"] = r.get("severity") or 0
            g["sev_text"] = r.get("sev_text") or sev_name(r.get("severity") or 0)
        if not g["trace_id"] and r.get("trace_id"):
            g["trace_id"] = r["trace_id"]
        b = min(buckets - 1, max(0, int((r["ts_ns"] - t0) // bucket_ns)))
        g["sparkline"][b] += 1
    out = list(groups.values())
    if new_within_hours > 0:
        cutoff = now_ns - int(new_within_hours * 3600e9)
        out = [g for g in out if g["first_ts"] >= cutoff]
    keyfn = {"count": lambda g: -g["count"], "last_seen": lambda g: -g["last_ts"],
             "first_seen": lambda g: -g["first_ts"], "new": lambda g: -g["first_ts"]}.get(sort, lambda g: -g["count"])
    out = sorted(out, key=keyfn)[:max(1, min(top, 200))]
    for g in out:
        g["services"] = sorted(x for x in g["services"] if x)
    return {"errors": out, "scanned": len(rows), "distinct": len(groups),
            "window_minutes": minutes, "bucket_minutes": bucket_ns / 60e9}


@app.get("/api/logs/groupby")
@ttl_cached
def logs_groupby(field: str, minutes: float = 60, service: str = "", min_severity: int = 0,
                 q_: str = "", env: str = "", top: int = 50, start_ms: float = 0, end_ms: float = 0):
    """Count logs by any attribute, resource attribute or built-in column (service, severity, env)."""
    rows = logs(minutes=minutes, service=service, min_severity=min_severity, q_=q_, limit=10000, env=env,
                start_ms=start_ms, end_ms=end_ms)
    builtin = {"service": lambda r: r.get("service"), "env": lambda r: r.get("env"),
               "severity": lambda r: r.get("sev_text") or sev_name(r.get("severity") or 0)}
    pick = builtin.get(field, lambda r: (r.get("attrs") or {}).get(field))
    counts, errs = collections.Counter(), collections.Counter()
    for r in rows:
        v = pick(r)
        if v is None or v == "":
            v = "(not set)"
        counts[str(v)] += 1
        if (r.get("severity") or 0) >= 17:
            errs[str(v)] += 1
    out = [{"value": k, "count": n, "errors": errs[k]} for k, n in counts.most_common(max(1, min(top, 500)))]
    return {"field": field, "groups": out, "scanned": len(rows), "distinct": len(counts)}


@app.get("/api/logs/fields")
def logs_fields(minutes: float = 60, env: str = "", sample: int = 500):
    """Attribute keys present in recent logs, so the UI can offer them for grouping and filtering."""
    rows = logs(minutes=minutes, limit=max(1, min(sample, 5000)), env=env)
    keys = collections.Counter()
    for r in rows:
        for k in (r.get("attrs") or {}):
            keys[k] += 1
    return {"builtin": ["service", "severity", "env"],
            "attributes": [{"key": k, "count": n} for k, n in keys.most_common(100)]}


@app.get("/api/logs/export")
def logs_export(minutes: float = 60, service: str = "", min_severity: int = 0, q_: str = "",
                env: str = "", limit: int = 50000, start_ms: float = 0, end_ms: float = 0):
    """The current log view as CSV."""
    rows = logs(minutes=minutes, service=service, min_severity=min_severity, q_=q_,
                limit=max(1, min(limit, 200000)), env=env, start_ms=start_ms, end_ms=end_ms)
    cols = ["ts_ns", "service", "env", "sev_text", "severity", "trace_id", "span_id", "body"]

    def esc(v):
        v = "" if v is None else str(v)
        return '"' + v.replace('"', '""') + '"' if any(ch in v for ch in ',"\n\r') else v

    def gen():
        yield ",".join(cols) + "\n"
        for r in rows:
            yield ",".join(esc(r.get(c, "")) for c in cols) + "\n"
    return StreamingResponse(gen(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=nebulens-logs.csv"})


# ---- log facets (UI sidebar)
@app.get("/api/logs/facets")
@ttl_cached
def logs_facets(minutes: float = 60, service: str = "", q_: str = "", env: str = "", min_severity: int = 0,
                start_ms: float = 0, end_ms: float = 0, sort: str = ""):
    """Sidebar counts. Each facet is counted with every OTHER filter applied but not its own, so the
    severity counts describe the selected service, and the service list still lets you switch."""
    t0, t1 = _window(minutes, start_ms, end_ms)
    base, args = ["ts_ns>=?"], [t0]
    if t1:
        base.append("ts_ns<?"); args.append(t1)
    esql, eargs = env_scope(env)
    if esql:
        base.append(esql[5:]); args += eargs
    for tok in q_.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            base.append("(CAST(json_extract(attrs, ?) AS TEXT)=? OR CAST(json_extract(res, ?) AS TEXT)=?)")
            args += [f'$."{k}"', v, f'$."{k}"', v]
        else:
            base.append("body LIKE ?"); args.append(f"%{tok}%")
    w_sev, a_sev = list(base), list(args)          # severity facet: honours the service filter
    if service:
        w_sev.append("service=?"); a_sev.append(service)
    w_svc, a_svc = list(base), list(args)          # service facet: honours the severity filter
    if min_severity:
        w_svc.append("severity>=?"); a_svc.append(int(min_severity))
    return {
        # err = ERROR/FATAL records, so the sidebar can rank the services that are actually in trouble.
        "service": q(f"""SELECT service value, COUNT(*) n, SUM(CASE WHEN severity>=17 THEN 1 ELSE 0 END) err
                         FROM logs WHERE {' AND '.join(w_svc)} GROUP BY service
                         ORDER BY {'err DESC, n DESC' if sort == 'errors' else 'n DESC'} LIMIT 500""", a_svc),
        "severity": q(f"""SELECT CASE WHEN severity>=21 THEN 'FATAL' WHEN severity>=17 THEN 'ERROR' WHEN severity>=13 THEN 'WARN'
                          WHEN severity>=9 THEN 'INFO' WHEN severity>=5 THEN 'DEBUG' WHEN severity>=1 THEN 'TRACE' ELSE 'UNSET' END value, COUNT(*) n
                          FROM logs WHERE {' AND '.join(w_sev)} GROUP BY value ORDER BY n DESC""", a_sev),
    }


# ---- NebuLens "intelligence" layer (rule-based, no LLM): insights, home summary, investigate, search
def _pct(sorted_vals, p):
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p))] if sorted_vals else 0.0


def _agg(rows):
    """{n, err, err_pct, p95} for (status, dur_ms) tuples."""
    n = len(rows)
    err = sum(1 for st, _ in rows if st == 2)
    return {"n": n, "err": err, "err_pct": 100 * err / n if n else 0.0, "p95": _pct(sorted(d for _, d in rows), .95)}


_SEV_RANK = {"critical": 0, "warning": 1, "info": 2}


@app.get("/api/insights")
@ttl_cached
def insights(minutes: float = 60):
    """Rule-based findings across all signals. Each: {severity, service, title, detail, link, kind}."""
    now = time.time_ns()
    t0 = now - int(minutes * 60e9)
    tp = t0 - int(minutes * 60e9)
    out = []

    def add(sev, kind, svc, title, detail, link):
        out.append({"severity": sev, "kind": kind, "service": svc, "title": title, "detail": detail, "link": link})

    recent_ns = now - int(min(minutes, 5) * 60e9)
    # Grouped scans instead of several queries per service: one pass over both windows' root spans, one for the last
    # minutes, one for error logs (partial index ix_logs_err).
    cur_w, prev_w = {}, {}
    rr = _rollup_range(2 * minutes)      # both windows must be covered
    if rr:
        m_cur = t0 // 60_000_000_000
        cur_a, prev_a = _rollup_window(m_cur), _rollup_window(rr[0], m_cur)
    else:
        cur_a = prev_a = None
        for svc, st, status, dur in qrows("SELECT service, start_ns, status, dur_ms FROM spans "
                                          "WHERE start_ns>=? AND start_ns<? AND (parent_id='' OR kind=2)", (tp, now + 1)):
            (cur_w if st >= t0 else prev_w).setdefault(svc, []).append((status, dur))
    if rr:     # minute buckets from the rollup: counts the current minute whole, so "silent" is judged to the minute
        last5s = {svc: int(n) for svc, n in qrows("SELECT service, SUM(n) FROM span_rollup WHERE minute>=? GROUP BY service",
                                                  (recent_ns // 60_000_000_000,))}
    else:
        last5s = dict(qrows("SELECT service, COUNT(*) FROM spans WHERE start_ns>=? AND (parent_id='' OR kind=2) GROUP BY service",
                            (recent_ns,)))
    logc = {r[0]: (r[1] or 0, r[2]) for r in qrows("SELECT service, SUM(ts_ns>=?), COUNT(*) FROM logs "
                                                     "WHERE severity>=17 AND ts_ns>=? GROUP BY service", (recent_ns, t0))}
    empty = {"n": 0, "err": 0, "err_pct": 0.0, "p95": 0.0}
    for s in sorted((set(cur_a) | set(prev_a)) if rr else (set(cur_w) | set(prev_w))):
        cur, prev = (cur_a.get(s, empty), prev_a.get(s, empty)) if rr else (_agg(cur_w.get(s, [])), _agg(prev_w.get(s, [])))
        if cur["n"] == 0:
            if prev["n"] >= 5:
                add("critical", "silent", s, f"{s} stopped reporting",
                    f"No requests in the last {minutes:g} min (previous window: {prev['n']}).", f"#/services/{s}")
            continue
        last5 = last5s.get(s, 0)
        if last5 == 0 and cur["n"] >= 5:
            add("critical", "silent", s, f"{s} went silent",
                f"{cur['n']} requests earlier in the window but none in the last {min(minutes, 5):g} min.", f"#/services/{s}")
        if cur["n"] >= 5 and cur["err_pct"] >= 5:
            spike = prev["n"] >= 5 and cur["err_pct"] >= 2 * max(prev["err_pct"], 0.5)
            sev = "critical" if cur["err_pct"] >= 20 else "warning"
            title = f"Error rate {'spike' if spike else 'elevated'} on {s}"
            detail = f"{cur['err_pct']:.1f}% of {cur['n']} requests failed" + (f" (was {prev['err_pct']:.1f}%)" if prev["n"] else "")
            add(sev, "error_rate", s, title, detail, f"#/traces?service={s}&errors=1")
        if cur["n"] >= 5 and prev["n"] >= 5 and prev["p95"] > 0 and cur["p95"] > 100 and cur["p95"] > 1.5 * prev["p95"]:
            add("warning", "latency", s, f"p95 latency drift on {s}",
                f"p95 {cur['p95']:.0f}ms vs {prev['p95']:.0f}ms in the prior window (+{100 * (cur['p95'] / prev['p95'] - 1):.0f}%).",
                f"#/services/{s}")
        e5, eall = logc.get(s, (0, 0))
        mins5 = min(minutes, 5)
        if e5 >= 5 and e5 / mins5 > 3 * (eall / max(minutes, 1)):
            add("warning", "log_burst", s, f"Error log burst on {s}",
                f"{e5} error logs in the last {mins5:g} min ({e5 / mins5:.1f}/min vs {eall / max(minutes, 1):.1f}/min average).",
                f"#/logs?service={s}&sev=17")
    for c in q("SELECT * FROM synthetics WHERE enabled=1"):
        last = q("SELECT * FROM synthetic_runs WHERE check_id=? ORDER BY ts DESC LIMIT 1", (c["id"],))
        if last and not last[0]["ok"]:
            add("critical", "synthetic", "", f"Synthetic check '{c['name']}' failing", last[0]["err"] or f"status {last[0]['status']}", "#/synthetics")
    for s in slos_list():
        used = s.get("error_budget_used_pct")
        if used is not None and used >= 75:
            add("critical" if used >= 100 else "warning", "slo", s["service"], f"SLO '{s['name']}' burning budget",
                f"{min(used, 999):.0f}% of error budget used (attainment {s['attainment_pct']}% vs objective {s['objective_pct']}%).", "#/slos")
    for r in q("SELECT * FROM alert_rules WHERE enabled=1 AND state='firing'"):
        add("critical", "alert", r["service"], f"Alert firing: {r['name']}",
            f"{r['signal']} = {(r['last_value'] or 0):.2f} {r['op']} {r['threshold']}", "#/alerts")
    out.sort(key=lambda x: _SEV_RANK[x["severity"]])
    return out


@app.get("/api/home")
@ttl_cached
def home(minutes: float = 60, buckets: int = Query(60, ge=1, le=1000)):
    buckets = max(1, min(int(buckets), 1000))
    t0 = since_ns(minutes)
    w = int(minutes * 60 * 1e9 / buckets)
    rr = _rollup_range(minutes)
    if rr and w < 60_000_000_000:
        # Buckets narrower than a minute used to force a raw scan of every request in the window (1.5M rows and a 25 s
        # timeout for "15 minutes" under load). Draw one point per minute from the rollup instead.
        buckets = max(1, int(minutes)); w = int(minutes * 60 * 1e9 / buckets)
    if rr:
        by = {i: {"n": v[0], "err": v[1]} for i, v in _rollup_series(rr[0], t0, w, buckets).items()}
    else:
        rows = q("""SELECT (start_ns-?)/? b, COUNT(*) n, SUM(status=2) err FROM spans
                    WHERE start_ns>=? AND (parent_id='' OR kind=2) GROUP BY b""", (t0, w, t0))
        by = {r["b"]: r for r in rows}
    ts = [dict(t=(t0 + i * w) / 1e6, rate=(by[i]["n"] / (w / 1e9)) if i in by else 0,
               err_rate=((by[i]["err"] or 0) / (w / 1e9)) if i in by else 0) for i in range(buckets)]
    svcs = [s for s in services(minutes) if s["n"] > 0]
    n = sum(s["n"] for s in svcs)
    err = sum(s["err"] for s in svcs)
    ins = insights(minutes)
    return {
        "totals": {"requests": n, "errors": err, "err_pct": round(100 * err / n, 2) if n else 0,
                   "services": len(svcs), "rate": n / (minutes * 60),
                   "p95_avg": sum(s["p95"] for s in svcs) / len(svcs) if svcs else 0,
                   **_home_log_totals(minutes, t0)},
        "timeseries": ts,
        "top_errors": sorted(svcs, key=lambda s: -s["err_pct"])[:5],
        "top_latency": sorted(svcs, key=lambda s: -s["p95"])[:5],
        "insight_counts": {k: sum(1 for i in ins if i["severity"] == k) for k in ("critical", "warning", "info")},
        "events": alert_events()[:6],
        "slos": slos_list(),
    }


@app.get("/api/search")
def search(q_: str = "", limit: int = 6):
    """Command-palette search: services, trace ids / root names, metric names."""
    s = q_.strip()
    if not s:
        return {"services": [], "traces": [], "metrics": []}
    like = f"%{s}%"
    return {
        "services": q("SELECT DISTINCT service name FROM services_seen WHERE service LIKE ? LIMIT ?", (like, limit)),
        "traces": _search_traces(s, like, limit),
        "metrics": ([{"name": k} for k in BUILTIN_METRICS if s.lower() in k.lower()]
                    + q("SELECT DISTINCT name FROM metric_names_seen WHERE name LIKE ? LIMIT ?", (like, limit)))[:limit],
    }


def _search_traces(s, like, limit):
    """A trace-id fragment used to scan 24 h of spans with LIKE '%x%' (it rarely matches, so it read everything and timed
    out, once per keystroke). Hex input is now a prefix range on the trace_id index; words match operation names from
    op_rollup and fetch one recent trace for each from the minute it was last seen."""
    if re.fullmatch(r"[0-9a-fA-F]{4,32}", s):
        lo = s.lower()
        return q("SELECT trace_id, name, service, dur_ms FROM spans WHERE trace_id>=? AND trace_id<? AND parent_id='' LIMIT ?",
                 (lo, lo + "g", limit))
    out, seen = [], set()
    for svc, nm, m in qrows("SELECT service, name, MAX(minute) FROM op_rollup WHERE minute>=? AND name LIKE ? "
                            "GROUP BY service, name ORDER BY MAX(minute) DESC LIMIT ?",
                            (since_ns(24 * 60) // 60_000_000_000, like, limit)):
        for r in q("SELECT trace_id, name, service, dur_ms FROM spans WHERE service=? AND start_ns>=? AND start_ns<? AND name=? "
                   "ORDER BY CASE WHEN parent_id='' THEN 0 ELSE 1 END LIMIT 1",
                   (svc, int(m) * 60_000_000_000, (int(m) + 1) * 60_000_000_000, nm)):
            if r["trace_id"] not in seen:          # the same trace can match through several operations
                seen.add(r["trace_id"]); out.append(r)
    return out


@ttl_cached
def _saturation_rows(minutes: float = 60):
    """Highest CPU/memory-style metrics of the newest 15 minutes for Ask NebuLens. Cached and kept warm: it reads up to 12
    metric names through the (name, ts_ns) index (1.6 s on a cold cache at 9M spans)."""
    names = [r[0] for r in qrows("SELECT DISTINCT name FROM metric_names_seen WHERE hr>=? AND (name LIKE '%cpu%' OR "
                                 "name LIKE '%memory%' OR name LIKE '%utiliz%' OR name LIKE '%usage%') "
                                 "AND name NOT LIKE '%available%' AND name NOT LIKE '%free%' AND name NOT LIKE '%limit' LIMIT 12",
                                 (since_ns(minutes) // 3_600_000_000_000,))]
    t0m = since_ns(min(minutes, 15))
    rows = []
    for nm in names:
        rows += q("SELECT CASE WHEN service IN ('', 'unknown') THEN COALESCE(json_extract(res, '$.\"k8s.pod.name\"'), service) "
                  "ELSE service END service, name, MAX(value) mx, AVG(value) av FROM metrics WHERE name=? AND ts_ns>=? GROUP BY 1, name",
                  (nm, t0m))
    return sorted(rows, key=lambda r: -(r["mx"] or 0))[:8]


def _home_log_totals(minutes, t0):
    if _env_ready(minutes):
        n, e = qrows("SELECT SUM(logs), SUM(log_err) FROM env_rollup WHERE minute>=?", (t0 // 60_000_000_000,))[0]
        return {"logs": int(n or 0), "error_logs": int(e or 0)}
    return {"logs": q("SELECT COUNT(*) n FROM logs WHERE ts_ns>=?", (t0,))[0]["n"],
            "error_logs": q("SELECT COUNT(*) n FROM logs WHERE ts_ns>=? AND severity>=17", (t0,))[0]["n"]}


@app.get("/api/investigate")
@ttl_cached
def investigate(q_: str = "", minutes: float = 60):
    """Rule-based 'Ask NebuLens': maps a question to an analysis over the stored signals."""
    text = q_.lower().strip()
    svcs = [s for s in services(minutes) if s["n"] > 0]
    findings, suggestions = [], []
    if any(k in text for k in ("slow", "latenc", "p95", "p99", "duration")):
        intent, title = "slow_services", "Slowest services"
        for s in sorted(svcs, key=lambda s: -s["p95"])[:5]:
            findings.append({"label": s["service"], "value": f"p95 {s['p95']:.0f}ms · p99 {s['p99']:.0f}ms",
                             "severity": "warning" if s["p95"] > 500 else "info", "link": f"#/services/{s['service']}"})
        # Example slow traces from the slowest service only, recent and above its p95 (was: sort every root span in the window).
        top = max(svcs, key=lambda s: s["p95"]) if svcs else None
        slow = q("""SELECT trace_id, name, service, dur_ms FROM spans WHERE service=? AND start_ns>=? AND parent_id='' AND dur_ms>=?
                    ORDER BY dur_ms DESC LIMIT 3""", (top["service"], since_ns(min(minutes, 15)), top["p95"])) if top else []
        for t in slow:
            findings.append({"label": f"Slow trace {t['name']}", "value": f"{t['dur_ms']:.0f}ms in {t['service']}",
                             "severity": "info", "link": f"#/traces?id={t['trace_id']}"})
        summary = (f"{findings[0]['label']} has the highest p95 latency ({findings[0]['value']})." if findings else "No traffic in this window.")
    elif any(k in text for k in ("saturat", "cpu", "memory", "mem ", "disk", "resource")):
        intent, title = "saturation", "Resource saturation"
        # Names from the registry first, then each through the (name, ts_ns) index (was: GROUP BY over every metric point).
        rows = _saturation_rows(minutes)
        for r in rows:
            findings.append({"label": f"{r['service']} · {r['name']}", "value": f"max {r['mx']:.3g} · avg {r['av']:.3g}",
                             "severity": "info", "link": f"#/metrics?name={r['name']}&service={r['service']}"})
        summary = "Highest utilisation-style metrics shown." if rows else "No CPU/memory-style metrics found in this window."
        if not rows:
            suggestions.append("Enable the bundled collector's hostmetrics/kubeletstats receivers to get CPU and memory.")
    elif any(k in text for k in ("log",)):
        intent, title = "error_logs", "Top error logs"
        rows = q("""SELECT service, substr(body,1,120) msg, COUNT(*) n FROM logs WHERE ts_ns>=? AND severity>=17
                    GROUP BY service, msg ORDER BY n DESC LIMIT 6""", (since_ns(min(minutes, 15)),))   # newest 15 min: fast at any volume
        for r in rows:
            findings.append({"label": r["msg"], "value": f"{r['n']}× in {r['service']}", "severity": "warning",
                             "link": f"#/logs?service={r['service']}&sev=17"})
        span = "in the last 15 minutes" if minutes > 15 else "in this window"
        summary = (f"{sum(r['n'] for r in rows)} error logs {span}, grouped by message." if rows else f"No error logs {span}.")
    elif any(k in text for k in ("error", "broke", "fail", "wrong", "down", "issue", "problem", "5xx", "500")):
        intent, title = "what_broke", "What is failing"
        bad = [s for s in svcs if s["err"] > 0]
        for s in sorted(bad, key=lambda s: -s["err_pct"])[:5]:
            op = (q("""SELECT name, SUM(n) n, SUM(err) e FROM op_rollup WHERE service=? AND minute>=?
                       GROUP BY name HAVING SUM(err)>0 ORDER BY e DESC LIMIT 1""", (s["service"], since_ns(minutes) // 60_000_000_000))
                  if _ops_ready(minutes) else [])
            msg = q("""SELECT substr(body,1,140) b FROM logs WHERE ts_ns>=? AND severity>=17 AND service=?
                       GROUP BY b ORDER BY COUNT(*) DESC LIMIT 1""", (since_ns(min(minutes, 15)), s["service"]))
            val = f"{s['err_pct']:.1f}% errors ({s['err']}/{s['n']})"
            if op:
                val += f" · worst op {op[0]['name']}"
            if msg:
                val += f" · \"{msg[0]['b']}\""
            findings.append({"label": s["service"], "value": val, "severity": "critical" if s["err_pct"] >= 20 else "warning",
                             "link": f"#/traces?service={s['service']}&errors=1"})
        summary = (f"{len(bad)} service(s) have failing requests; {findings[0]['label']} is the worst." if findings
                   else "No failing requests in this window. All services look healthy.")
    else:
        intent, title = "overview", "Overview"
        ins = insights(minutes)
        for i in ins[:6]:
            findings.append({"label": i["title"], "value": i["detail"], "severity": i["severity"], "link": i["link"]})
        summary = (f"{len(ins)} active insight(s) across {len(svcs)} service(s)." if ins
                   else f"{len(svcs)} service(s) reporting and nothing needs attention.")
        suggestions = ["Which services are slow?", "What is failing right now?", "Show top error logs", "Any resource saturation?"]
    return {"question": q_, "intent": intent, "title": title, "summary": summary, "findings": findings,
            "suggestions": suggestions or ["Which services are slow?", "What is failing right now?", "Show top error logs"]}


ALERT_TEMPLATES = [
    {"id": "high-error-rate", "category": "Availability", "name": "High error rate", "description": "More than 5% of requests fail over 5 minutes.",
     "signal": "error_rate", "expr": "", "op": ">", "threshold": 5, "window_s": 300},
    {"id": "critical-error-rate", "category": "Availability", "name": "Critical error rate", "description": "More than 20% of requests fail over 2 minutes.",
     "signal": "error_rate", "expr": "", "op": ">", "threshold": 20, "window_s": 120},
    {"id": "p95-latency-500", "category": "Latency", "name": "p95 latency above 500ms", "description": "Slow tail latency over 5 minutes.",
     "signal": "p95_latency", "expr": "", "op": ">", "threshold": 500, "window_s": 300},
    {"id": "p95-latency-1s", "category": "Latency", "name": "p95 latency above 1s", "description": "Severely slow requests over 5 minutes.",
     "signal": "p95_latency", "expr": "", "op": ">", "threshold": 1000, "window_s": 300},
    {"id": "traffic-drop", "category": "Availability", "name": "Traffic dropped to zero", "description": "No requests for 5 minutes - service may be down.",
     "signal": "request_rate", "expr": "", "op": "<=", "threshold": 0, "window_s": 300},
    {"id": "traffic-surge", "category": "Traffic", "name": "Traffic surge", "description": "More than 100 requests/second sustained.",
     "signal": "request_rate", "expr": "", "op": ">", "threshold": 100, "window_s": 120},
    {"id": "error-log-burst", "category": "Logs", "name": "Error log burst", "description": "More than 20 error logs in 5 minutes.",
     "signal": "log_errors", "expr": "", "op": ">", "threshold": 20, "window_s": 300},
    {"id": "db-errors", "category": "Logs", "name": "Database errors in logs", "description": "Error logs mentioning 'database' in 5 minutes.",
     "signal": "log_errors", "expr": "database", "op": ">", "threshold": 3, "window_s": 300},
    {"id": "timeout-errors", "category": "Logs", "name": "Timeouts in logs", "description": "Error logs mentioning 'timeout'.",
     "signal": "log_errors", "expr": "timeout", "op": ">", "threshold": 3, "window_s": 300},
    {"id": "synthetic-down", "category": "Synthetics", "name": "Synthetic check failing", "description": "Any failed synthetic run in 5 minutes.",
     "signal": "synthetic_down", "expr": "", "op": ">", "threshold": 0, "window_s": 300},
    {"id": "cpu-high", "category": "Saturation", "name": "CPU utilisation above 85%", "description": "Average CPU utilisation (system.cpu.utilization).",
     "signal": "metric_avg", "expr": "system.cpu.utilization", "op": ">", "threshold": 0.85, "window_s": 300},
    {"id": "mem-high", "category": "Saturation", "name": "Memory utilisation above 90%", "description": "Peak memory utilisation (system.memory.utilization).",
     "signal": "metric_max", "expr": "system.memory.utilization", "op": ">", "threshold": 0.9, "window_s": 300},
    {"id": "k8s-cpu-limit", "category": "Kubernetes", "name": "Container CPU near limit", "description": "k8s.container.cpu_limit_utilization above 90%.",
     "signal": "metric_max", "expr": "k8s.container.cpu_limit_utilization", "op": ">", "threshold": 0.9, "window_s": 300},
    {"id": "k8s-mem-limit", "category": "Kubernetes", "name": "Container memory near limit", "description": "k8s.container.memory_limit_utilization above 90%.",
     "signal": "metric_max", "expr": "k8s.container.memory_limit_utilization", "op": ">", "threshold": 0.9, "window_s": 300},
    {"id": "pod-restarts", "category": "Kubernetes", "name": "Pod restarts", "description": "k8s.container.restarts increased.",
     "signal": "metric_max", "expr": "k8s.container.restarts", "op": ">", "threshold": 3, "window_s": 600},
]


@app.get("/api/alerts/templates")
def alert_templates():
    return ALERT_TEMPLATES


# ---- Claude-backed assistant ("Ask NebuLens")
# Read-only tool use over NebuLens's own query functions. The rule-based /api/investigate
# stays as the fallback: the UI calls it whenever this endpoint reports disabled/error.
AI_MAX_ITERATIONS = 8
AI_MAX_TOOL_CHARS = 12000
AI_MAX_MESSAGES = 20
AI_MAX_MSG_CHARS = 4000

AI_SYSTEM = (
    "You are the observability analyst built into this NebuLens instance (an OpenTelemetry-native "
    "platform holding traces, logs and metrics). Answer the user's question using ONLY the read-only "
    "tools provided; never guess numbers. Cite concrete evidence: service names, trace ids (full hex "
    "ids), error counts, percentages and latencies. If the data needed is missing or empty, say so "
    "plainly instead of speculating. Be concise: short paragraphs or bullet lists, lead with the "
    "answer, then the evidence, then a suggested next step. Use markdown (bold, lists, `code`)."
)

_MIN = {"type": "number", "description": "Look-back window in minutes (default 60)."}
AI_TOOLS = [
    {"name": "list_services", "description": "Services with request count, error count/%, rate and p50/p95/p99 latency (RED).",
     "input_schema": {"type": "object", "properties": {"minutes": _MIN}}},
    {"name": "service_health", "description": "Health score (0-100), state and reason for one service.",
     "input_schema": {"type": "object", "properties": {"service": {"type": "string"}, "minutes": _MIN}, "required": ["service"]}},
    {"name": "search_traces", "description": "Search recent traces (newest first, max 20). Filter by service, errors only, minimum duration (ms) or free text.",
     "input_schema": {"type": "object", "properties": {"service": {"type": "string"}, "errors": {"type": "boolean"},
                                                       "min_ms": {"type": "number"}, "text": {"type": "string"},
                                                       "minutes": _MIN, "limit": {"type": "integer"}}}},
    {"name": "get_trace", "description": "All spans (and correlated logs) of one trace by its full trace id.",
     "input_schema": {"type": "object", "properties": {"trace_id": {"type": "string"}}, "required": ["trace_id"]}},
    {"name": "search_logs", "description": "Search logs (newest first, max 30). min_severity is OTel severity number (9=INFO, 13=WARN, 17=ERROR).",
     "input_schema": {"type": "object", "properties": {"service": {"type": "string"}, "min_severity": {"type": "integer"},
                                                       "text": {"type": "string"}, "trace_id": {"type": "string"},
                                                       "minutes": _MIN, "limit": {"type": "integer"}}}},
    {"name": "metric_names", "description": "Names of all metrics NebuLens has seen (with services).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "metric_query", "description": "Time series for one metric. agg is avg|max|min|sum; group_by is an attribute key.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "service": {"type": "string"}, "minutes": _MIN,
                                                       "agg": {"type": "string", "enum": ["avg", "max", "min", "sum"]},
                                                       "group_by": {"type": "string"}}, "required": ["name"]}},
    {"name": "list_insights", "description": "Rule-based Live Insights: current problems (error spikes, latency drift, log bursts, silent services, SLO burn).",
     "input_schema": {"type": "object", "properties": {"minutes": _MIN}}},
    {"name": "list_alerts", "description": "Alert rules with current state, and recent alert events.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_slos", "description": "SLOs with attainment and error-budget status.",
     "input_schema": {"type": "object", "properties": {}}},
]


# ---- AI provider. The assistant works with any LLM:
#   NEBULENS_AI_PROVIDER=anthropic   Claude, through the official Anthropic SDK (key: NEBULENS_AI_API_KEY or ANTHROPIC_API_KEY)
#   NEBULENS_AI_PROVIDER=openai      any endpoint that speaks the OpenAI chat-completions API with tool calling: OpenAI, Azure
#                                  OpenAI, Ollama, vLLM, LM Studio, Groq, Mistral, OpenRouter, Together, Gemini's compatible
#                                  endpoint... Set NEBULENS_AI_BASE_URL (default https://api.openai.com/v1) and NEBULENS_AI_MODEL;
#                                  the key (NEBULENS_AI_API_KEY or OPENAI_API_KEY) is optional for local servers such as Ollama.
# Unset provider: ANTHROPIC_API_KEY alone keeps working as before (Claude); otherwise OPENAI_API_KEY or NEBULENS_AI_BASE_URL
# selects the OpenAI-compatible path. NEBULENS_AI_NAME sets the name shown in the UI (e.g. "Llama 3.1 on Ollama").
_AI_OPENAI_ALIASES = ("openai", "openai-compatible", "openai_compatible", "compatible", "azure", "azure-openai", "ollama", "vllm",
                      "lmstudio", "lm-studio", "groq", "mistral", "openrouter", "together", "gemini", "deepseek", "local")


def _ai_provider():
    # Hosted mode serves many tenants from one process: never forward their telemetry to an outside model
    # with the platform's key unless the operator explicitly opts in.
    if HOSTED and os.environ.get("NEBULENS_HOSTED_AI") != "1":
        return ""
    p = os.environ.get("NEBULENS_AI_PROVIDER", "").strip().lower()
    if p in ("anthropic", "claude"):
        return "anthropic"
    if p in _AI_OPENAI_ALIASES:
        return "openai"
    if p in ("off", "none", "rules", "disabled"):
        return ""
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY", "").strip() or os.environ.get("NEBULENS_AI_BASE_URL", "").strip():
        return "openai"
    return ""


def _ai_key():
    prov = _ai_provider()
    if not prov:
        return ""
    k = os.environ.get("NEBULENS_AI_API_KEY", "").strip()
    return k or os.environ.get("ANTHROPIC_API_KEY" if prov == "anthropic" else "OPENAI_API_KEY", "").strip()


def _ai_base_url():
    return (os.environ.get("NEBULENS_AI_BASE_URL", "").strip() or "https://api.openai.com/v1").rstrip("/")


def _ai_model():
    m = os.environ.get("NEBULENS_AI_MODEL", "").strip()
    if m:
        return m
    return "claude-sonnet-5-5" if _ai_provider() == "anthropic" else ""


def _ai_ready():
    """(enabled, reason). Anthropic needs a key; an OpenAI-compatible endpoint needs a model name, and a key unless it is a
    local / self-hosted server (a base URL other than api.openai.com)."""
    prov = _ai_provider()
    if not prov:
        return False, "no AI provider configured"
    if prov == "anthropic":
        return (True, "") if _ai_key() else (False, "NEBULENS_AI_PROVIDER=anthropic but no API key is set")
    if urllib.parse.urlparse(_ai_base_url()).scheme not in ("http", "https"):
        return False, "NEBULENS_AI_BASE_URL must be an http(s) URL"
    if not _ai_model():
        return False, "set NEBULENS_AI_MODEL to the model name your endpoint serves"
    if not _ai_key() and "api.openai.com" in _ai_base_url():
        return False, "set NEBULENS_AI_API_KEY (or NEBULENS_AI_BASE_URL for a local server such as Ollama)"
    return True, ""


def _ai_name():
    n = os.environ.get("NEBULENS_AI_NAME", "").strip()
    if n:
        return n
    if _ai_provider() == "anthropic":
        return "Claude"
    host = urllib.parse.urlparse(_ai_base_url()).hostname or ""
    return "OpenAI" if host == "api.openai.com" else "AI model"


# Community gets a daily allowance of AI answers so people can try the real thing; after that the UI offers Pro. 0 = AI answers
# are Pro-only. Pro (and the trial) is unlimited. Customers bring their own model, so the allowance costs the vendor nothing.
COMMUNITY_AI_DAILY = max(0, int(os.environ.get("NEBULENS_COMMUNITY_AI_DAILY", "30") or 0))


def _ai_quota():
    """None on Pro (unlimited); otherwise {limit, used, remaining} for today (UTC)."""
    if IS_PRO:
        return None
    day = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        r = q("SELECT n FROM ai_usage WHERE day=?", (day,))
        used = int(r[0]["n"]) if r else 0
    except Exception:
        used = 0
    return {"limit": COMMUNITY_AI_DAILY, "used": used, "remaining": max(0, COMMUNITY_AI_DAILY - used), "resets": "00:00 UTC"}


def _ai_take():
    """Count one AI answer against today's Community allowance. Returns False when none is left."""
    qt = _ai_quota()
    if qt is None:
        return True
    if qt["remaining"] <= 0:
        return False
    with db() as c:
        c.execute("INSERT INTO ai_usage(day, n) VALUES(?, 1) ON CONFLICT(day) DO UPDATE SET n=ai_usage.n+1",
                  (time.strftime("%Y-%m-%d", time.gmtime()),))
        c.execute("DELETE FROM ai_usage WHERE day<?", (time.strftime("%Y-%m-%d", time.gmtime(time.time() - 40 * 86400)),))
    return True


def _ai_client():
    """Build the SDK client lazily so NebuLens runs fine (rules mode) without the package/key."""
    import anthropic
    return anthropic.Anthropic(api_key=_ai_key(), timeout=60.0, max_retries=1)


def _clip(obj, limit=AI_MAX_TOOL_CHARS):
    """JSON-encode a tool result, shrinking long lists before falling back to a hard cut."""
    def shrink(o, n):
        if isinstance(o, list):
            return [shrink(x, n) for x in o[:n]]
        if isinstance(o, dict):
            return {k: shrink(v, n) for k, v in o.items()}
        if isinstance(o, str) and len(o) > 500:
            return o[:500] + "…"
        return o
    for n in (50, 25, 12, 6):
        out = json.dumps(shrink(obj, n), default=str)
        if len(out) <= limit:
            return out
    return json.dumps(shrink(obj, 3), default=str)[:limit] + "…[truncated]"


def _num(v, default, lo, hi):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def _tool_get_trace(a):
    d = trace(str(a.get("trace_id", "")))
    spans = [{"span_id": x["span_id"], "parent_id": x["parent_id"], "service": x["service"], "name": x["name"],
              "dur_ms": round(x["dur_ms"], 2), "error": x["status"] == 2,
              "attrs": dict(list((x["attrs"] or {}).items())[:8])} for x in d["spans"][:40]]
    return {"span_count": len(d["spans"]), "spans": spans, "logs": d["logs"][:15]}


def _tool_dispatch(name, a):
    """Execute one read-only tool. Raises on unknown tool / bad input; the caller reports it to the model."""
    m = _num(a.get("minutes"), 60, 1, 10080)
    if name == "list_services":
        return [r for r in services(m) if r["n"] > 0]
    if name == "service_health":
        return service_health(str(a.get("service", "")), m)
    if name == "search_traces":
        return traces(m, str(a.get("service", "")), bool(a.get("errors", False)), _num(a.get("min_ms"), 0, 0, 1e9),
                      str(a.get("text", "")), int(_num(a.get("limit"), 10, 1, 20)))
    if name == "get_trace":
        return _tool_get_trace(a)
    if name == "search_logs":
        return logs(m, str(a.get("service", "")), int(_num(a.get("min_severity"), 0, 0, 24)), str(a.get("text", "")),
                    str(a.get("trace_id", "")), int(_num(a.get("limit"), 15, 1, 30)))
    if name == "metric_names":
        return metric_names()
    if name == "metric_query":
        agg = a.get("agg") if a.get("agg") in ("avg", "max", "min", "sum") else "avg"
        return metric_query(str(a.get("name", "")), str(a.get("service", "")), m, 30, agg, str(a.get("group_by", "")))
    if name == "list_insights":
        return insights(m)
    if name == "list_alerts":
        return {"rules": alerts(), "events": alert_events()[:20]}
    if name == "list_slos":
        return slos_list()
    raise ValueError(f"unknown tool {name}")


class AssistantMsg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=AI_MAX_MSG_CHARS)


class AssistantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: list[AssistantMsg] = Field(min_length=1, max_length=AI_MAX_MESSAGES)
    stream: bool = False

    @field_validator("messages")
    @classmethod
    def _last_is_user(cls, v):
        if v[-1].role != "user":
            raise ValueError("last message must have role 'user'")
        return v


def _safe_err(e):
    """Error text for clients/logs with the API key scrubbed out."""
    msg = f"{type(e).__name__}: {e}"
    k = _ai_key()
    return msg.replace(k, "***") if k else msg


def _openai_tools():
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in AI_TOOLS]


def _openai_chat(payload):
    """One chat-completions call to the configured OpenAI-compatible endpoint. Standard library only, so no extra package is
    needed and it works air-gapped against a local model server."""
    headers = {"Content-Type": "application/json"}
    key = _ai_key()
    if key:
        hk = os.environ.get("NEBULENS_AI_KEY_HEADER", "").strip()     # e.g. "api-key" for Azure OpenAI; default Bearer
        headers[hk or "Authorization"] = key if hk else "Bearer " + key
    req = urllib.request.Request(_ai_base_url() + "/chat/completions", data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        # Reviewed: operator-configured endpoint; _ai_ready() requires http(s).
        with urllib.request.urlopen(req, timeout=float(os.environ.get("NEBULENS_AI_TIMEOUT", "60"))) as r:  # nosec B310
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"{_ai_name()} endpoint returned HTTP {e.code}: {body}") from None


def _assistant_events_openai(messages):
    """The same read-only tool loop as the Claude path, over the OpenAI chat-completions tool-calling format."""
    msgs = [{"role": "system", "content": AI_SYSTEM}] + [{"role": m["role"], "content": m["content"]} for m in messages]
    used, model = [], _ai_model()
    try:
        for it in range(AI_MAX_ITERATIONS):
            resp = _openai_chat({"model": model, "messages": msgs, "tools": _openai_tools(), "tool_choice": "auto", "max_tokens": 2048})
            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            text = msg.get("content") or ""
            calls = [c for c in (msg.get("tool_calls") or []) if c.get("type", "function") == "function"]
            if not calls:
                for i in range(0, len(text), 48):
                    yield {"type": "text", "text": text[i:i + 48]}
                yield {"type": "done", "model": model, "tools": used, "iterations": it + 1}
                return
            msgs.append({"role": "assistant", "content": text or None, "tool_calls": calls})
            for c in calls:
                fn = c.get("function") or {}
                name = fn.get("name", "")
                try:
                    inp = json.loads(fn.get("arguments") or "{}")
                    inp = inp if isinstance(inp, dict) else {}
                    yield {"type": "tool", "name": name, "input": inp}
                    out, ok = _clip(_tool_dispatch(name, inp)), True
                except Exception as e:  # bad arguments or a failing tool: report it to the model, never crash the loop
                    out, ok = f"error: {e}", False
                used.append(name)
                yield {"type": "tool_result", "name": name, "ok": ok}
                msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out})
        yield {"type": "error", "message": f"assistant stopped after {AI_MAX_ITERATIONS} tool rounds without a final answer"}
    except Exception as e:
        yield {"type": "error", "message": _safe_err(e)}


def _assistant_events(messages):
    """Run the tool loop on the configured provider, yielding UI events: tool, tool_result, text, done, error."""
    if _ai_provider() == "openai":
        yield from _assistant_events_openai(messages)
        return
    try:
        cl = _ai_client()
    except Exception as e:  # missing package etc.
        yield {"type": "error", "message": _safe_err(e)}
        return
    msgs = [{"role": m["role"], "content": m["content"]} for m in messages]
    used = []
    try:
        for it in range(AI_MAX_ITERATIONS):
            resp = cl.messages.create(model=_ai_model(), max_tokens=2048, system=AI_SYSTEM, tools=AI_TOOLS, messages=msgs)
            blocks = list(resp.content)
            text = "".join(b.text for b in blocks if b.type == "text")
            calls = [b for b in blocks if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not calls:
                for i in range(0, len(text), 48):
                    yield {"type": "text", "text": text[i:i + 48]}
                yield {"type": "done", "model": _ai_model(), "tools": used, "iterations": it + 1}
                return
            msgs.append({"role": "assistant", "content": [
                {"type": "text", "text": b.text} if b.type == "text" else
                {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input} for b in blocks]})
            results = []
            for c in calls:
                inp = c.input if isinstance(c.input, dict) else {}
                yield {"type": "tool", "name": c.name, "input": inp}
                try:
                    out, ok = _clip(_tool_dispatch(c.name, inp)), True
                except Exception as e:  # report to the model, never crash the loop
                    out, ok = f"error: {e}", False
                used.append(c.name)
                yield {"type": "tool_result", "name": c.name, "ok": ok}
                results.append({"type": "tool_result", "tool_use_id": c.id, "content": out, **({} if ok else {"is_error": True})})
            msgs.append({"role": "user", "content": results})
        yield {"type": "error", "message": f"assistant stopped after {AI_MAX_ITERATIONS} tool rounds without a final answer"}
    except Exception as e:
        yield {"type": "error", "message": _safe_err(e)}


@app.get("/api/assistant/status")
def assistant_status():
    ready, reason = _ai_ready()
    # Pro / trial: unlimited AI answers. Community: a daily allowance, then the UI offers Pro (rule-based analysis is always free).
    # /api/assistant enforces it; this lets the UI say so up front.
    qt = _ai_quota()
    allowed = qt is None or qt["remaining"] > 0
    on = ready and allowed
    if ready and not allowed:
        reason = ("AI answers are a Pro feature" if qt["limit"] == 0
                  else f"today's {qt['limit']} free AI answers are used up (resets {qt['resets']})")
    return {"enabled": on, "model": _ai_model() if on else None, "mode": "llm" if on else "rules",
            "provider": _ai_provider() or None, "name": _ai_name() if on else None,
            "pro": IS_PRO, "trial": bool(_TRIAL_ACTIVE), "quota": qt, "locked": ready and not allowed,
            **({} if on else {"reason": reason})}


@app.post("/api/assistant")
def assistant(body: AssistantIn):
    on, reason = _ai_ready()
    if not on:
        return JSONResponse({"error": "assistant_disabled", "mode": "rules", "fallback": "/api/investigate",
                             "message": f"No AI model is configured ({reason}); use the rule-based /api/investigate."}, status_code=503)
    if not IS_PRO and COMMUNITY_AI_DAILY == 0:
        _tier_gate("AI assistant")
    if not _ai_take():
        raise HTTPException(403, {"error": "tier_limit", "feature": "AI answers", "limit": COMMUNITY_AI_DAILY, "tier": "community",
                                  "message": f"You've used today's {COMMUNITY_AI_DAILY} free AI answers. They reset at 00:00 UTC; "
                                             "Pro has unlimited AI answers. The rule-based analysis is still free.",
                                  "upgrade_url": PRICING_URL})
    msgs = [m.model_dump() for m in body.messages]
    if body.stream:
        def sse():
            for ev in _assistant_events(msgs):
                yield f"data: {json.dumps(ev)}\n\n"
        return StreamingResponse(sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    text, used, err = "", [], None
    for ev in _assistant_events(msgs):
        if ev["type"] == "text":
            text += ev["text"]
        elif ev["type"] == "tool":
            used.append(ev["name"])
        elif ev["type"] == "error":
            err = ev["message"]
    if err:
        return JSONResponse({"error": "assistant_error", "mode": "rules", "fallback": "/api/investigate", "message": err}, status_code=502)
    return {"mode": "llm", "provider": _ai_provider(), "name": _ai_name(), "model": _ai_model(), "answer": text, "tools": used}


# ================================================================ deployment mode + hosted (multi-tenant)
# Everything below is inert unless NEBULENS_MODE=hosted, except GET /api/mode and the SSRF helpers.
#
# Model: organization -> projects (each project is its own SQLite file = a hard data silo) -> API keys.
#   ingest key (project)   : POST /v1/*                       (what exporters/collectors use)
#   read key   (project)   : GET /api/*                       (dashboards, read-only API consumers)
#   admin key  (org-wide)  : all /api/* (pick project with x-nebulens-project) + /org/* self-service
# Platform operator: NEBULENS_ADMIN_TOKEN guards /admin/* (create orgs, quotas, keys, usage).
# Keys are stored as sha256 hashes; the full key is shown exactly once at creation.
DEFAULT_DAILY_CAP = int(os.environ.get("NEBULENS_DEFAULT_DAILY_CAP", "5000000"))  # spans+logs+metric points per org per UTC day
KEY_TYPES = {"ingest": "p0i_", "read": "p0r_", "admin": "p0a_"}
FAIL_LIMIT = int(os.environ.get("NEBULENS_AUTH_FAIL_LIMIT", "20"))  # failed auths per IP per minute
FAIL_WINDOW = 60

# ---- hosted-mode self-serve signup + Stripe billing (inert unless HOSTED; also needs NEBULENS_STRIPE_SECRET_KEY
# for the /billing/* endpoints specifically -- signup/login work without Stripe configured, orgs just stay "free").
# Paid tiers' quotas reuse the same orgs.daily_cap/retention_hours columns the rest of hosted mode already reads,
# so nothing else needs to change when a webhook moves an org between tiers.
STRIPE_SECRET_KEY = os.environ.get("NEBULENS_STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("NEBULENS_STRIPE_WEBHOOK_SECRET", "")
try:
    # {"price_xxx": "pro", "price_yyy": "enterprise"} -- which Stripe Price a tier's Checkout button uses
    STRIPE_PRICE_TIERS = json.loads(os.environ.get("NEBULENS_STRIPE_PRICE_TIERS", "") or "{}")
except (json.JSONDecodeError, ValueError):
    print("warning: NEBULENS_STRIPE_PRICE_TIERS is not valid JSON; ignoring it")
    STRIPE_PRICE_TIERS = {}
HOSTED_TIER_QUOTAS = {                              # 0 = unlimited, same convention OrgIn already uses
    "free": {"daily_cap": DEFAULT_DAILY_CAP, "retention_hours": RETENTION_HOURS},
    "pro": {"daily_cap": 0, "retention_hours": 24 * 30},
    "enterprise": {"daily_cap": 0, "retention_hours": 24 * 365},
}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _hash_password(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$" + salt.hex() + "$" + h.hex()


def _verify_password(pw, stored):
    try:
        algo, salt_hex, hash_hex = (stored or "").split("$")
        if algo != "scrypt":
            return False
        h = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(h.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# A fixed dummy hash scrypt'd once at import, so /login can always pay the same scrypt cost whether or not the
# email exists -- otherwise a nonexistent email skips hashing entirely and answers measurably faster than a
# wrong password for a real one (~30ms gap, confirmed exploitable for email enumeration in testing).
_DUMMY_PASSWORD_HASH = _hash_password(secrets.token_hex(32))


def _stripe_request(path, form=None, method="POST"):
    """Minimal form-encoded Stripe REST client (every Stripe endpoint accepts that). Tests replace this with a
    fake Stripe, the same way _idp_request is replaced with a fake identity provider."""
    if not STRIPE_SECRET_KEY:
        raise HTTPException(503, {"error": "billing_not_configured", "message": "Billing is not set up on this install."})
    data = urllib.parse.urlencode(form or {}, doseq=True).encode()
    req = urllib.request.Request("https://api.stripe.com/v1/" + path, data=data if method == "POST" else None,
                                  headers={"Authorization": "Bearer " + STRIPE_SECRET_KEY}, method=method)
    try:
        # Reviewed: URL is a fixed https://api.stripe.com/v1/ prefix, never user input.
        with urllib.request.urlopen(req, timeout=15) as r:  # nosec B310
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except (json.JSONDecodeError, ValueError):
            body = {}
        raise HTTPException(502, {"error": "stripe_error", "message": body.get("error", {}).get("message", str(e))})


def _verify_stripe_signature(payload, sig_header, secret):
    """https://docs.stripe.com/webhooks#verify-manually -- HMAC-SHA256 over "{timestamp}.{payload}", and the
    timestamp must be recent (replay protection)."""
    try:
        parts = dict(p.split("=", 1) for p in (sig_header or "").split(","))
        t, v1 = parts["t"], parts["v1"]
        expected = hmac.new(secret.encode(), f"{t}.{payload.decode()}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, v1) and abs(time.time() - int(t)) < 300
    except (KeyError, ValueError):
        return False


def _issue_org_session(resp, account, request):
    resp.set_cookie("nl_org_session",
                     _sign({"aid": account["id"], "org_id": account["org_id"], "email": account["email"],
                            "exp": time.time() + 30 * 86400}),
                     max_age=30 * 86400, httponly=True, samesite="lax", secure=_secure_cookie(request), path="/")


def _org_session(request):
    return _unsign(request.cookies.get("nl_org_session", ""))

_PLAT_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS orgs(
  id TEXT PRIMARY KEY, name TEXT UNIQUE, created INTEGER, daily_cap INTEGER, retention_hours REAL);
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY, org_id TEXT, name TEXT, created INTEGER, UNIQUE(org_id, name));
CREATE TABLE IF NOT EXISTS api_keys(
  id TEXT PRIMARY KEY, org_id TEXT, project_id TEXT, type TEXT, name TEXT, prefix TEXT,
  hash TEXT UNIQUE, created INTEGER, last_used INTEGER, revoked_at INTEGER);
CREATE TABLE IF NOT EXISTS usage(
  project_id TEXT, day TEXT, spans INTEGER DEFAULT 0, logs INTEGER DEFAULT 0, metrics INTEGER DEFAULT 0,
  rejected INTEGER DEFAULT 0, PRIMARY KEY(project_id, day));
CREATE TABLE IF NOT EXISTS accounts(
  id TEXT PRIMARY KEY, org_id TEXT, email TEXT UNIQUE, password_hash TEXT, created INTEGER, last_login INTEGER);
"""
# Columns added after some installs' platform.db already existed. Same ALTER-TABLE-if-missing pattern as
# _migrate_env_column above, just against the hosted platform database instead of a tenant's own.
_PLAT_ORGS_NEW_COLUMNS = (("tier", "TEXT NOT NULL DEFAULT 'free'"), ("stripe_customer_id", "TEXT"),
                          ("stripe_subscription_id", "TEXT"))


def _migrate_platform_schema(conn):
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(orgs)")}
        for name, decl in _PLAT_ORGS_NEW_COLUMNS:
            if cols and name not in cols:
                conn.execute(f"ALTER TABLE orgs ADD COLUMN {name} {decl}")
    except Exception as e:
        print("platform schema migration:", e)


_plat_lock = threading.Lock()
_plat = None
if HOSTED:
    os.makedirs(os.path.join(DATA_DIR, "projects"), exist_ok=True)
    _plat = sqlite3.connect(os.path.join(DATA_DIR, "platform.db"), check_same_thread=False)
    _plat.row_factory = sqlite3.Row
    _plat.executescript(_PLAT_SCHEMA)
    _migrate_platform_schema(_plat)
    _plat.commit()


def pq(sql, args=()):
    with _plat_lock:
        return [dict(r) for r in _plat.execute(sql, args).fetchall()]


def pexec(sql, args=()):
    with _plat_lock:
        cur = _plat.execute(sql, args)
        _plat.commit()
        return cur.rowcount


def _new_id(prefix):
    return f"{prefix}_{secrets.token_hex(6)}"


def _project_path(pid):
    if not re.fullmatch(r"prj_[0-9a-f]{12}", str(pid)):
        raise ValueError("bad project id")
    return os.path.join(DATA_DIR, "projects", pid + ".db")


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _seconds_to_midnight():
    t = time.gmtime()
    return 86400 - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec)


def _hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


def _mint_key(org_id, ktype, name, project_id=None):
    key = KEY_TYPES[ktype] + secrets.token_urlsafe(30)
    kid = _new_id("key")
    pexec("INSERT INTO api_keys(id,org_id,project_id,type,name,prefix,hash,created) VALUES(?,?,?,?,?,?,?,?)",
          (kid, org_id, project_id, ktype, name, key[:10], _hash_key(key), int(time.time())))
    return {"id": kid, "key": key, "prefix": key[:10], "type": ktype, "name": name, "project_id": project_id,
            "note": "Copy this key now. It is stored hashed and cannot be shown again."}


_last_touch = {}


def _verify_key(key):
    """Return the key row (+ its org's quota fields) for a valid, unrevoked key, else None."""
    if not key or len(key) > 200:
        return None
    h = _hash_key(key)
    rows = pq("SELECT k.*, o.daily_cap, o.retention_hours, o.name org_name FROM api_keys k "
              "JOIN orgs o ON o.id=k.org_id WHERE k.hash=? AND k.revoked_at IS NULL", (h,))
    if not rows or not hmac.compare_digest(rows[0]["hash"], h):
        return None
    r, now = rows[0], time.time()
    if now - _last_touch.get(r["id"], 0) > 60:
        _last_touch[r["id"]] = now
        pexec("UPDATE api_keys SET last_used=? WHERE id=?", (int(now), r["id"]))
    return r


def _extract_key(request):
    k = request.headers.get("x-nebulens-key") or request.headers.get("x-oscilyx-key") or request.headers.get("x-pulse0-key")   # old header name still accepted
    if k:
        return k.strip()
    a = request.headers.get("authorization", "")
    return a[7:].strip() if a[:7].lower() == "bearer " else ""


def _admin_ok(request):
    tok = _extract_key(request)
    return bool(ADMIN_TOKEN) and bool(tok) and hmac.compare_digest(tok.encode(), ADMIN_TOKEN.encode())


_FAILS = collections.defaultdict(collections.deque)


def _client_ip(request):
    """Caller IP, recorded on every audit record. X-Forwarded-For is trusted only behind a proxy
    you control (NEBULENS_TRUST_PROXY=1), because any client can send that header."""
    if os.environ.get("NEBULENS_TRUST_PROXY") == "1":
        xf = request.headers.get("x-forwarded-for", "") or request.headers.get("x-real-ip", "")
        if xf:
            return xf.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "?")[:64]


def _auth_fail(request, **extra):
    """401, or 429 once an IP has failed too often. Valid keys are never throttled, so a noisy
    attacker behind a shared proxy IP cannot lock real users out."""
    if len(_FAILS) > 10000:
        _FAILS.clear()
    d, now = _FAILS[_client_ip(request)], time.time()
    while d and now - d[0] > FAIL_WINDOW:
        d.popleft()
    if len(d) >= FAIL_LIMIT:
        return JSONResponse({"error": "too_many_attempts"}, status_code=429, headers={"Retry-After": str(FAIL_WINDOW)})
    d.append(now)
    return JSONResponse({"error": "unauthorized", **extra}, status_code=401)


def _org_used_today(org_id):
    return pq("SELECT COALESCE(SUM(u.spans+u.logs+u.metrics),0) n FROM usage u JOIN projects p ON p.id=u.project_id "
              "WHERE p.org_id=? AND u.day=?", (org_id, _today()))[0]["n"]


def _otlp_precount(d, kind):
    """How many rows the matching ingest_* call will produce, without doing the insert -- counted the same way
    each ingest_* function counts its own `len(rows)` return value (one row per span / log record / metric
    data point), so this always matches what actually gets stored."""
    if kind == "traces":
        return sum(len(ss.get("spans", [])) for rs in d.get("resourceSpans", []) for ss in rs.get("scopeSpans", []))
    if kind == "logs":
        return sum(len(sl.get("logRecords", [])) for rl in d.get("resourceLogs", []) for sl in rl.get("scopeLogs", []))
    if kind == "metrics":
        n = 0
        for rm in d.get("resourceMetrics", []):
            for sm in rm.get("scopeMetrics", []):
                for m in sm.get("metrics", []):
                    for mtype in ("gauge", "sum", "histogram", "exponentialHistogram", "summary"):
                        if mtype in m:
                            n += len(m[mtype].get("dataPoints", []))
        return n
    return 0


_OTLP_KIND_COL = {"traces": "spans", "logs": "logs", "metrics": "metrics"}


def _reserve_usage(ident, kind, n):
    """Atomically check the org's daily cap against (today's usage + n) and, if it fits, immediately commit n
    against today's usage -- in one _plat_lock-held step, called BEFORE the batch is inserted (with n from
    _otlp_precount, not the final count).
    Found in a security audit: the previous design checked the cap in middleware before the request body was
    even read, then recorded usage only after the whole batch was parsed and inserted (in _otlp, well after
    the check). Concurrent requests all read the same stale "used today" in the gap between those two points;
    a 20-way concurrent burst against a cap of 50 landed 210 items (4.2x over) before any of the 429s caught up.
    Reserving the estimated count up front, atomically with the admission check, closes that window: the cap
    is now enforced against committed reservations, not a snapshot that's already stale by the time it's read."""
    if not ident or not ident.get("project_id") or not n:
        return True, 0
    col = _OTLP_KIND_COL[kind]
    cap, pid, org_id, day = ident.get("daily_cap"), ident["project_id"], ident["org_id"], _today()
    with _plat_lock:
        if cap is not None:
            used = _plat.execute("SELECT COALESCE(SUM(spans+logs+metrics),0) FROM usage u JOIN projects p "
                                  "ON p.id=u.project_id WHERE p.org_id=? AND u.day=?", (org_id, day)).fetchone()[0]
            if used + n > cap:
                return False, 0
        _plat.execute(f"INSERT INTO usage(project_id,day,{col}) VALUES(?,?,?) ON CONFLICT(project_id,day) "
                      f"DO UPDATE SET {col}={col}+excluded.{col}", (pid, day, n))
        _plat.commit()
        return True, n


def _usage_reconcile(ident, kind, reserved, actual):
    """The reservation used an estimate (_otlp_precount, computed before insertion); true up the small, rare
    mismatch against what actually got inserted (e.g. a partially malformed batch that stored fewer rows).
    Erring toward the reservation when they differ is deliberately safe: at worst an org's recorded usage is
    briefly a few rows higher than what's actually stored, never lower than what's enforced against its cap."""
    delta = (actual or 0) - reserved
    if not delta or not ident or not ident.get("project_id"):
        return
    col = _OTLP_KIND_COL[kind]
    pexec(f"UPDATE usage SET {col}={col}+? WHERE project_id=? AND day=?", (delta, ident["project_id"], _today()))


def _resolve_project(ident, request):
    """The project a /api/* request reads: the key's own project, or (org admin key) the one named by
    x-nebulens-project / ?project=, or the org's only project."""
    if ident["project_id"]:
        return ident["project_id"], None
    projs = pq("SELECT id,name FROM projects WHERE org_id=? ORDER BY created", (ident["org_id"],))
    sel = (request.headers.get("x-nebulens-project") or request.headers.get("x-pulse0-project")
           or request.query_params.get("project") or "")
    if sel:
        for p in projs:
            if sel in (p["id"], p["name"]):
                return p["id"], None
        return None, JSONResponse({"error": "unknown_project", "message": f"no project '{sel}' in this organization"}, status_code=404)
    if len(projs) == 1:
        return projs[0]["id"], None
    return None, JSONResponse({"error": "project_required", "projects": projs,
                               "message": "choose a project with the x-nebulens-project header"}, status_code=400)


_READ_ONLY_POSTS = ("/api/assistant", "/api/investigate")


async def _hosted_auth(request: Request, call_next):
    path, method = request.url.path, request.method
    if method == "OPTIONS":  # CORS preflight carries no credentials
        return await call_next(request)
    if path.startswith("/admin"):
        if not _admin_ok(request):
            return _auth_fail(request)
        return await call_next(request)
    if path in ("/api/mode", "/api/auth/config") or not (path.startswith("/v1/") or path.startswith("/api/") or path.startswith("/org/")):
        return await call_next(request)
    ident = _verify_key(_extract_key(request))
    if not ident:
        return _auth_fail(request)
    kt = ident["type"]
    request.state.ident = ident
    tok = None
    if path.startswith("/v1/"):
        if kt != "ingest":
            return JSONResponse({"error": "forbidden", "message": "an ingest key is required for /v1/*"}, status_code=403)
        cap = ident["daily_cap"]
        if cap is not None and _org_used_today(ident["org_id"]) >= cap:
            pexec("INSERT INTO usage(project_id,day,rejected) VALUES(?,?,1) ON CONFLICT(project_id,day) "
                  "DO UPDATE SET rejected=rejected+1", (ident["project_id"], _today()))
            return JSONResponse({"error": "quota_exceeded", "message": "this organization's daily ingest cap is reached"},
                                status_code=429, headers={"Retry-After": str(_seconds_to_midnight())})
        tok = _cur_db.set(_project_path(ident["project_id"]))
    elif path.startswith("/org/"):
        if kt != "admin":
            return JSONResponse({"error": "forbidden", "message": "an organization admin key is required"}, status_code=403)
    else:
        if kt not in ("read", "admin"):
            return JSONResponse({"error": "forbidden", "message": "a read or admin key is required for /api/*"}, status_code=403)
        if method not in ("GET", "HEAD") and kt != "admin" and path not in _READ_ONLY_POSTS:
            return JSONResponse({"error": "forbidden", "message": "read keys cannot modify data"}, status_code=403)
        pid, err = _resolve_project(ident, request)
        if err:
            return err
        request.state.project_id = pid
        tok = _cur_db.set(_project_path(pid))
    try:
        return await call_next(request)
    finally:
        if tok is not None:
            _cur_db.reset(tok)


# ---- outbound-call guard (webhooks + synthetics). Tenants must not be able to point NebuLens at the
# cluster network, loopback or the cloud metadata service.
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_NOREDIR = urllib.request.build_opener(_NoRedirect)


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """Self-hosted: follow redirects (http -> https is common for synthetic checks), but only to destinations that pass
    _blocked_dest, so a public URL cannot bounce a request to the cloud metadata service."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _blocked_dest(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_CHECKEDREDIR = urllib.request.build_opener(_CheckedRedirect)


def _urlopen(req, timeout):
    return (_NOREDIR if HOSTED else _CHECKEDREDIR).open(req, timeout=timeout)


def _blocked_dest(url):
    """Why this URL may not be called, or None.
    Every mode: only http(s), and never link-local addresses (169.254.0.0/16, fe80::/10: the cloud metadata service, which
    hands out instance credentials), multicast or unspecified. Self-hosted still allows the private network, because checking
    internal services is what synthetic checks are for. Hosted mode also blocks private, loopback and reserved ranges."""
    u = urlparse(url or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        return "only http(s) URLs are allowed"
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except OSError:
        return None  # unresolvable: the request itself will fail
    for i in infos:
        ip = ipaddress.ip_address(i[4][0].split("%")[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            return f"destination {ip} is not allowed (link-local / cloud metadata)"
        if HOSTED and (ip.is_private or ip.is_loopback or ip.is_reserved):
            return f"destination {ip} is not allowed in hosted mode"
    return None


# ---- usage + overview helpers
def _db_bytes(pid):
    base, total = _project_path(pid), 0
    for sfx in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(base + sfx)
        except OSError:
            pass
    return total


def _org_usage(org_id):
    org = pq("SELECT * FROM orgs WHERE id=?", (org_id,))
    if not org:
        raise HTTPException(404, "organization not found")
    o = org[0]
    days = [time.strftime("%Y-%m-%d", time.gmtime(time.time() - i * 86400)) for i in range(7)]
    projects = []
    for p in pq("SELECT id,name,created FROM projects WHERE org_id=? ORDER BY created", (org_id,)):
        rows = {r["day"]: r for r in pq("SELECT * FROM usage WHERE project_id=?", (p["id"],))}
        zero = {"spans": 0, "logs": 0, "metrics": 0, "rejected": 0}
        today = {k: (rows.get(_today()) or zero)[k] for k in zero}
        week = {k: sum((rows.get(d) or zero)[k] for d in days) for k in zero}
        projects.append({**p, "today": today, "last7d": week, "db_bytes": _db_bytes(p["id"])})
    return {"day": _today(), "org": {"id": o["id"], "name": o["name"], "daily_cap": o["daily_cap"] or 0,
                                     "used_today": _org_used_today(org_id), "retention_hours": o["retention_hours"]},
            "projects": projects}


def _key_rows(org_id):
    return pq("SELECT id,project_id,type,name,prefix,created,last_used,revoked_at FROM api_keys WHERE org_id=? ORDER BY created", (org_id,))


def _create_project(org_id, name):
    pid = _new_id("prj")
    try:
        pexec("INSERT INTO projects(id,org_id,name,created) VALUES(?,?,?,?)", (pid, org_id, name, int(time.time())))
    except _IntegrityError:
        raise HTTPException(409, f"project '{name}' already exists in this organization")
    _open_db(_project_path(pid)).close()  # create the silo eagerly
    return {"id": pid, "name": name, "org_id": org_id}


def _delete_project(pid):
    pexec("DELETE FROM api_keys WHERE project_id=?", (pid,))
    pexec("DELETE FROM usage WHERE project_id=?", (pid,))
    pexec("DELETE FROM projects WHERE id=?", (pid,))
    path = _project_path(pid)
    with _tenants_guard:
        h = _tenants.pop(path, None)
    if h:  # close under its lock so the file can be removed (Windows keeps open files locked)
        with h[1]:
            h[0].close()
    base = os.path.realpath(os.path.join(DATA_DIR, "projects"))
    for sfx in ("", "-wal", "-shm"):
        target = os.path.realpath(path + sfx)
        if os.path.commonpath([base, target]) != base:
            continue
        try:
            os.remove(target)
        except OSError as e:
            print("could not remove", target, e)


def _need_org(org_id):
    r = pq("SELECT * FROM orgs WHERE id=?", (org_id,))
    if not r:
        raise HTTPException(404, "organization not found")
    return r[0]


def _need_project(org_id, pid):
    r = pq("SELECT * FROM projects WHERE id=? AND org_id=?", (pid, org_id))
    if not r:
        raise HTTPException(404, "project not found")
    return r[0]


def _make_key(org_id, body):
    if body.type in ("ingest", "read"):
        if not body.project_id:
            raise HTTPException(400, f"a {body.type} key must be bound to a project (project_id)")
        _need_project(org_id, body.project_id)
        return _mint_key(org_id, body.type, body.name, body.project_id)
    return _mint_key(org_id, "admin", body.name, None)


def _revoke(key_id, org_id=None, forbid_id=None):
    r = pq("SELECT * FROM api_keys WHERE id=?" + (" AND org_id=?" if org_id else ""), (key_id, org_id) if org_id else (key_id,))
    if not r:
        raise HTTPException(404, "key not found")
    if forbid_id and r[0]["id"] == forbid_id:
        raise HTTPException(400, "you cannot revoke the key you are signed in with")
    pexec("UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (int(time.time()), key_id))
    return {"id": key_id, "revoked": True}


# ---- request models
_NAME = r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,62}$"


class OrgIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=64, pattern=_NAME)
    daily_cap: int = Field(default=DEFAULT_DAILY_CAP, ge=0)  # 0 = unlimited
    retention_hours: float = Field(default=RETENTION_HOURS, ge=1, le=24 * 365)
    with_admin_key: bool = True


class OrgPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, min_length=2, max_length=64, pattern=_NAME)
    daily_cap: Optional[int] = Field(default=None, ge=0)
    retention_hours: Optional[float] = Field(default=None, ge=1, le=24 * 365)


class ProjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64, pattern=_NAME)


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["ingest", "read", "admin"]
    name: str = Field(min_length=1, max_length=64)
    project_id: Optional[str] = Field(default=None, pattern=r"^prj_[0-9a-f]{12}$")


class SignupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8, max_length=200)
    org_name: str = Field(min_length=2, max_length=64, pattern=_NAME)


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=1, max_length=200)


class CheckoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price_id: str = Field(min_length=1, max_length=100)
    success_url: str = Field(min_length=1, max_length=2000)
    cancel_url: str = Field(min_length=1, max_length=2000)


def _hosted_only():
    if not HOSTED:
        raise HTTPException(404, "Not Found")


from fastapi import APIRouter

admin = APIRouter(prefix="/admin", dependencies=[Depends(_hosted_only)])
org = APIRouter(prefix="/org", dependencies=[Depends(_hosted_only)])


# ---- platform operator API (NEBULENS_ADMIN_TOKEN)
@admin.post("/orgs")
def admin_org_create(body: OrgIn):
    oid = _new_id("org")
    try:
        pexec("INSERT INTO orgs(id,name,created,daily_cap,retention_hours) VALUES(?,?,?,?,?)",
              (oid, body.name, int(time.time()), body.daily_cap or None, body.retention_hours))
    except _IntegrityError:
        raise HTTPException(409, f"organization '{body.name}' already exists")
    out = {"id": oid, "name": body.name, "daily_cap": body.daily_cap, "retention_hours": body.retention_hours}
    if body.with_admin_key:
        out["admin_key"] = _mint_key(oid, "admin", "initial admin key")
    return out


@admin.get("/orgs")
def admin_org_list():
    out = []
    for o in pq("SELECT * FROM orgs ORDER BY created"):
        out.append({"id": o["id"], "name": o["name"], "created": o["created"], "daily_cap": o["daily_cap"] or 0,
                    "retention_hours": o["retention_hours"], "used_today": _org_used_today(o["id"]),
                    "projects": pq("SELECT COUNT(*) n FROM projects WHERE org_id=?", (o["id"],))[0]["n"],
                    "active_keys": pq("SELECT COUNT(*) n FROM api_keys WHERE org_id=? AND revoked_at IS NULL", (o["id"],))[0]["n"]})
    return out


@admin.patch("/orgs/{org_id}")
def admin_org_patch(org_id: str, body: OrgPatch):
    _need_org(org_id)
    try:
        if body.name is not None:
            pexec("UPDATE orgs SET name=? WHERE id=?", (body.name, org_id))
        if body.daily_cap is not None:
            pexec("UPDATE orgs SET daily_cap=? WHERE id=?", (body.daily_cap or None, org_id))
        if body.retention_hours is not None:
            pexec("UPDATE orgs SET retention_hours=? WHERE id=?", (body.retention_hours, org_id))
    except _IntegrityError:
        raise HTTPException(409, "organization name already exists")
    return _org_usage(org_id)["org"]


@admin.delete("/orgs/{org_id}")
def admin_org_delete(org_id: str):
    _need_org(org_id)
    for p in pq("SELECT id FROM projects WHERE org_id=?", (org_id,)):
        _delete_project(p["id"])
    pexec("DELETE FROM api_keys WHERE org_id=?", (org_id,))
    pexec("DELETE FROM orgs WHERE id=?", (org_id,))
    return {"deleted": org_id}


@admin.post("/orgs/{org_id}/projects")
def admin_project_create(org_id: str, body: ProjectIn):
    _need_org(org_id)
    return _create_project(org_id, body.name)


@admin.get("/orgs/{org_id}/projects")
def admin_project_list(org_id: str):
    _need_org(org_id)
    return pq("SELECT id,name,created FROM projects WHERE org_id=? ORDER BY created", (org_id,))


@admin.delete("/projects/{project_id}")
def admin_project_delete(project_id: str):
    if not re.fullmatch(r"prj_[0-9a-f]{12}", project_id) or not pq("SELECT 1 FROM projects WHERE id=?", (project_id,)):
        raise HTTPException(404, "project not found")
    _delete_project(project_id)
    return {"deleted": project_id}


@admin.post("/orgs/{org_id}/keys")
def admin_key_create(org_id: str, body: KeyIn):
    _need_org(org_id)
    return _make_key(org_id, body)


@admin.get("/orgs/{org_id}/keys")
def admin_key_list(org_id: str):
    _need_org(org_id)
    return _key_rows(org_id)


@admin.delete("/keys/{key_id}")
def admin_key_revoke(key_id: str):
    return _revoke(key_id)


@admin.get("/usage")
def admin_usage_all():
    return [_org_usage(o["id"]) for o in pq("SELECT id FROM orgs ORDER BY created")]


@admin.get("/orgs/{org_id}/usage")
def admin_usage_org(org_id: str):
    return _org_usage(org_id)


# ---- organization self-service API (org admin key)
@org.get("/me")
def org_me(request: Request):
    ident = request.state.ident
    u = _org_usage(ident["org_id"])
    return {**u, "key": {"id": ident["id"], "type": ident["type"], "prefix": ident["prefix"]},
            "projects": u["projects"], "keys": _key_rows(ident["org_id"])}


@org.get("/projects")
def org_projects(request: Request):
    return _org_usage(request.state.ident["org_id"])["projects"]


@org.post("/projects")
def org_project_create(body: ProjectIn, request: Request):
    return _create_project(request.state.ident["org_id"], body.name)


@org.delete("/projects/{project_id}")
def org_project_delete(project_id: str, request: Request):
    if not re.fullmatch(r"prj_[0-9a-f]{12}", project_id):
        raise HTTPException(404, "project not found")
    _need_project(request.state.ident["org_id"], project_id)
    _delete_project(project_id)
    return {"deleted": project_id}


@org.get("/keys")
def org_keys(request: Request):
    return _key_rows(request.state.ident["org_id"])


@org.post("/keys")
def org_key_create(body: KeyIn, request: Request):
    return _make_key(request.state.ident["org_id"], body)


@org.delete("/keys/{key_id}")
def org_key_revoke(key_id: str, request: Request):
    i = request.state.ident
    return _revoke(key_id, org_id=i["org_id"], forbid_id=i["id"])


@org.get("/usage")
def org_usage(request: Request):
    return _org_usage(request.state.ident["org_id"])


app.include_router(admin)
app.include_router(org)


# ---- self-serve signup, login and billing (hosted mode). These sit outside /admin, /org and /v1, so the
# _hosted_auth middleware above passes them straight through untouched; each does its own auth: a signed
# nl_org_session cookie (same _sign/_unsign HMAC the self-hosted SSO cookie already uses) for signup/login/
# billing, and a verified Stripe-Signature header for the webhook. An /org/* admin API key is unrelated to this
# session and still works exactly as before for CLI/CI use.
def _apply_org_tier(org_id, tier):
    q = HOSTED_TIER_QUOTAS.get(tier, HOSTED_TIER_QUOTAS["free"])
    pexec("UPDATE orgs SET tier=?, daily_cap=?, retention_hours=? WHERE id=?",
          (tier, q["daily_cap"] or None, q["retention_hours"], org_id))


@app.post("/signup")
def signup(body: SignupIn, request: Request, response: Response):
    _hosted_only()
    email = body.email.strip().lower()
    if pq("SELECT 1 FROM accounts WHERE email=?", (email,)):
        raise HTTPException(409, {"error": "email_taken", "message": "An account with this email already exists."})
    oid, aid = _new_id("org"), _new_id("acct")
    q = HOSTED_TIER_QUOTAS["free"]
    now = int(time.time())
    # Both inserts in one real transaction (not two separate pexec() auto-commits): two requests for the same
    # email can both pass the SELECT check above before either commits (TOCTOU), so the accounts insert's own
    # UNIQUE constraint is the real guard -- and when it loses that race, the org insert just before it must
    # roll back too, or the loser leaves a permanently orphaned, unusable org name behind (found live: the org
    # insert was committed on its own and never cleaned up when the following account insert then raised,
    # unhandled, as a 500).
    with _plat_lock:
        try:
            _plat.execute("INSERT INTO orgs(id,name,created,daily_cap,retention_hours,tier) VALUES(?,?,?,?,?,?)",
                          (oid, body.org_name, now, q["daily_cap"] or None, q["retention_hours"], "free"))
            _plat.execute("INSERT INTO accounts(id,org_id,email,password_hash,created,last_login) VALUES(?,?,?,?,?,?)",
                          (aid, oid, email, _hash_password(body.password), now, now))
            _plat.commit()
        except _IntegrityError as e:
            _plat.rollback()
            if "accounts" in str(e):
                raise HTTPException(409, {"error": "email_taken", "message": "An account with this email already exists."})
            raise HTTPException(409, {"error": "org_name_taken", "message": f"An organization called '{body.org_name}' already exists."})
    admin_key = _mint_key(oid, "admin", "initial admin key")
    _issue_org_session(response, {"id": aid, "org_id": oid, "email": email}, request)
    return {"org_id": oid, "org_name": body.org_name, "email": email, "admin_key": admin_key}


@app.post("/login")
def login(body: LoginIn, request: Request, response: Response):
    _hosted_only()
    email = body.email.strip().lower()
    rows = pq("SELECT * FROM accounts WHERE email=?", (email,))
    stored_hash = rows[0]["password_hash"] if rows else _DUMMY_PASSWORD_HASH
    ok = _verify_password(body.password, stored_hash)   # always run, even for a nonexistent email (see _DUMMY_PASSWORD_HASH)
    if not rows or not ok:
        raise HTTPException(401, {"error": "invalid_credentials", "message": "Wrong email or password."})
    acct = rows[0]
    pexec("UPDATE accounts SET last_login=? WHERE id=?", (int(time.time()), acct["id"]))
    _issue_org_session(response, acct, request)
    org = _need_org(acct["org_id"])
    return {"org_id": acct["org_id"], "org_name": org["name"], "email": email}


@app.post("/logout")
def logout(response: Response):
    _hosted_only()
    response.delete_cookie("nl_org_session", path="/")
    return {"ok": True}


def _account_session(request):
    """The verified nl_org_session for /account and /billing endpoints, or 401."""
    sess = _org_session(request)
    if not sess:
        raise HTTPException(401, {"error": "signed_out", "message": "Sign in again."})
    return sess


@app.get("/account/me")
def account_me(request: Request):
    _hosted_only()
    sess = _account_session(request)
    org = _need_org(sess["org_id"])
    u = _org_usage(sess["org_id"])
    return {**u, "email": sess["email"], "tier": org["tier"], "billing_active": bool(org["stripe_subscription_id"]),
            "keys": _key_rows(sess["org_id"])}


@app.post("/billing/checkout")
def billing_checkout(body: CheckoutIn, request: Request):
    """Start a Stripe subscription Checkout for the signed-in org. The tier the price maps to (from
    NEBULENS_STRIPE_PRICE_TIERS) travels in the session's own metadata, so the webhook doesn't need a second
    lookup to know which plan was bought."""
    _hosted_only()
    sess = _account_session(request)
    org = _need_org(sess["org_id"])
    tier = STRIPE_PRICE_TIERS.get(body.price_id)
    if not tier:
        # Fail closed, not silently wrong: defaulting an unmapped price to "pro" would mean an operator who
        # adds a new Enterprise price in Stripe but forgets to update NEBULENS_STRIPE_PRICE_TIERS silently
        # grants Enterprise customers Pro-only quotas instead, with nothing ever surfacing the mismatch.
        raise HTTPException(400, {"error": "unknown_price", "message":
                                  "This price_id is not mapped to a tier in NEBULENS_STRIPE_PRICE_TIERS."})
    form = {"mode": "subscription", "client_reference_id": sess["org_id"], "customer_email": sess["email"],
            "success_url": body.success_url, "cancel_url": body.cancel_url,
            "line_items[0][price]": body.price_id, "line_items[0][quantity]": 1,
            "metadata[org_id]": sess["org_id"], "metadata[tier]": tier}
    if org["stripe_customer_id"]:
        form["customer"] = org["stripe_customer_id"]
        del form["customer_email"]
    out = _stripe_request("checkout/sessions", form)
    return {"url": out["url"]}


@app.post("/billing/portal")
def billing_portal(request: Request):
    _hosted_only()
    sess = _account_session(request)
    org = _need_org(sess["org_id"])
    if not org["stripe_customer_id"]:
        raise HTTPException(400, {"error": "no_subscription", "message": "This organization has no billing account yet."})
    out = _stripe_request("billing_portal/sessions", {"customer": org["stripe_customer_id"]})
    return {"url": out["url"]}


@app.post("/billing/webhook")
async def billing_webhook(request: Request):
    _hosted_only()
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(503, {"error": "billing_not_configured", "message": "No webhook secret is configured."})
    payload = await request.body()
    if not _verify_stripe_signature(payload, request.headers.get("stripe-signature", ""), STRIPE_WEBHOOK_SECRET):
        raise HTTPException(400, {"error": "bad_signature", "message": "Stripe signature did not verify."})
    try:
        event = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(400, {"error": "bad_payload", "message": "Webhook body is not valid JSON."})
    if not isinstance(event, dict):
        raise HTTPException(400, {"error": "bad_payload", "message": "Webhook body must be a JSON object."})
    etype, obj = event.get("type", ""), event.get("data", {}).get("object", {})
    if etype == "checkout.session.completed":
        org_id = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("org_id")
        tier = (obj.get("metadata") or {}).get("tier", "pro")
        if org_id and pq("SELECT 1 FROM orgs WHERE id=?", (org_id,)):
            pexec("UPDATE orgs SET stripe_customer_id=?, stripe_subscription_id=? WHERE id=?",
                  (obj.get("customer"), obj.get("subscription"), org_id))
            _apply_org_tier(org_id, tier)
    elif etype in ("customer.subscription.deleted", "customer.subscription.updated") and obj.get("status") in (
            None, "canceled", "unpaid", "incomplete_expired"):
        rows = pq("SELECT id FROM orgs WHERE stripe_customer_id=?", (obj.get("customer"),))
        if rows:
            # Keep stripe_customer_id (so a later resubscribe reuses it and billing_portal still works), but
            # clear stripe_subscription_id: otherwise /account/me's billing_active stayed true forever after a
            # cancellation, since it was only ever set, never cleared, by this handler.
            pexec("UPDATE orgs SET stripe_subscription_id=NULL WHERE id=?", (rows[0]["id"],))
            _apply_org_tier(rows[0]["id"], "free")
    return {"received": True}


_disc = {"at": 0.0, "doc": None}


def _idp_request(url, form=None, headers=None):
    """GET (or form-POST) JSON from the identity provider. Tests replace this with a fake IdP."""
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError("IdP url must be http(s)")
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, headers={"Accept": "application/json", **(headers or {})})
    # Reviewed: scheme checked to be http(s) just above.
    with urllib.request.urlopen(req, timeout=10) as r:  # nosec B310
        return json.loads(r.read())


def _discovery():
    if _disc["doc"] and time.time() - _disc["at"] < 3600:
        return _disc["doc"]
    doc = _idp_request(OIDC["issuer"] + "/.well-known/openid-configuration")
    if not doc.get("authorization_endpoint") or not doc.get("token_endpoint"):
        raise HTTPException(502, "identity provider discovery document is missing endpoints")
    _disc.update(at=time.time(), doc=doc)
    return doc


def _need_sso():
    if not IS_PRO:
        raise HTTPException(403, {"error": "pro_required", "message": "SSO is available on the Pro tier.",
                                  "tier": "community", "upgrade_url": PRICING_URL})
    if not sso_enabled():
        raise HTTPException(404, "SSO is not configured")


def _redirect_uri(request):
    return OIDC["redirect_url"] or str(request.base_url).rstrip("/") + "/auth/callback"


def _secure_cookie(request):
    return _redirect_uri(request).startswith("https://") or request.url.scheme == "https"


def _safe_next(n):
    n = n or "/"
    return n if n.startswith("/") and not n.startswith("//") and "\\" not in n else "/"


def _login_page(msg, status):
    import html
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>Sign-in failed</title><body style='font:15px system-ui;"
                        f"max-width:520px;margin:15vh auto;padding:0 16px'><h2>Sign-in failed</h2><p>{html.escape(msg)}</p>"
                        f"<p><a href='/auth/login'>Try again</a></p></body>", status_code=status)


def _claim_path(claims, path):
    cur = claims
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _role_claim_value(claims):
    name = OIDC["role_claim"]
    return claims[name] if name in claims else _claim_path(claims, name)  # also supports nested paths like realm_access.roles


def _claim_groups(claims):
    v = _role_claim_value(claims)
    return sorted({x for x in re.split(r"[,\s]+", v) if x} if isinstance(v, str) else {str(x) for x in (v or [])})


def _role_from_claims(claims):
    vals = set(_claim_groups(claims))
    for role, key in (("admin", "admin_groups"), ("editor", "editor_groups"), ("viewer", "viewer_groups")):
        if vals & set(OIDC[key]):
            return role
    return OIDC["default_role"] or None


@app.get("/auth/login")
def auth_login(request: Request, next: str = "/"):
    _need_sso()
    disc = _discovery()
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    params = {"response_type": "code", "client_id": OIDC["client_id"], "redirect_uri": _redirect_uri(request),
              "scope": OIDC["scopes"], "state": state, "nonce": nonce,
              "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
    ep = disc["authorization_endpoint"]
    resp = RedirectResponse(ep + ("&" if "?" in ep else "?") + urllib.parse.urlencode(params), status_code=302)
    resp.set_cookie("nl_oidc", _sign({"s": state, "n": nonce, "v": verifier, "next": _safe_next(next), "exp": time.time() + 600}),
                    max_age=600, httponly=True, samesite="lax", secure=_secure_cookie(request), path="/auth")
    return resp


@app.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    _need_sso()
    if error:
        return _login_page(f"The identity provider returned an error: {error}", 400)
    st = _unsign(request.cookies.get("nl_oidc", ""))
    if not st or not code or not state or not hmac.compare_digest(str(st.get("s", "")), state):
        return _login_page("The sign-in session expired or was tampered with. Please start again.", 400)
    try:
        disc = _discovery()
        tok = _idp_request(disc["token_endpoint"], {
            "grant_type": "authorization_code", "code": code, "redirect_uri": _redirect_uri(request),
            "client_id": OIDC["client_id"], "client_secret": OIDC["client_secret"], "code_verifier": st["v"]})
        claims = json.loads(_unb64(tok["id_token"].split(".")[1]))  # received over TLS straight from the issuer's token endpoint
        if _role_claim_value(claims) is None and disc.get("userinfo_endpoint") and tok.get("access_token"):
            claims = {**_idp_request(disc["userinfo_endpoint"], headers={"Authorization": "Bearer " + tok["access_token"]}), **claims}
    except HTTPException:
        raise
    except Exception as e:
        print("oidc callback error:", e)
        return _login_page("Could not complete sign-in with the identity provider.", 502)
    aud = claims.get("aud")
    aud = aud if isinstance(aud, list) else [aud]
    if str(claims.get("iss", "")).rstrip("/") != OIDC["issuer"]:
        return _login_page("The identity token was issued by an unexpected issuer.", 403)
    if OIDC["client_id"] not in aud:
        return _login_page("The identity token was not issued for this application.", 403)
    if float(claims.get("exp", 0)) < time.time() - 30:
        return _login_page("The identity token has expired.", 403)
    if not hmac.compare_digest(str(claims.get("nonce", "")), st["n"]):
        return _login_page("The identity token does not match this sign-in attempt.", 403)
    email = str(claims.get("email") or claims.get("preferred_username") or claims.get("sub") or "")
    if not email:
        return _login_page("The identity provider did not return an email or user name.", 403)
    if claims.get("email_verified") is False:
        return _login_page("Your email address is not verified with the identity provider.", 403)
    if OIDC["allowed_domains"] and email.rsplit("@", 1)[-1].lower() not in OIDC["allowed_domains"]:
        return _login_page("Your email domain is not allowed to sign in here.", 403)
    role = _role_from_claims(claims)
    if not role:
        _audit(dict(actor=email, role="none", via="session"), "LOGIN", "/auth/callback", 403, "no role mapped")
        return _login_page("Your account has no NebuLens role. Ask an administrator to add you to a NebuLens group.", 403)
    ttl = OIDC["session_ttl_h"] * 3600
    resp = RedirectResponse(_safe_next(st.get("next")), status_code=302)
    resp.set_cookie("nl_session", _sign({"sub": str(claims.get("sub", "")), "email": email, "name": str(claims.get("name", "")),
                                         "role": role, "grp": _claim_groups(claims)[:40], "exp": time.time() + ttl}),
                    max_age=int(ttl), httponly=True, samesite="lax", secure=_secure_cookie(request), path="/")
    resp.delete_cookie("nl_oidc", path="/auth")
    _audit(dict(actor=email, role=role, via="session"), "LOGIN", "/auth/callback", 302)
    return resp


@app.post("/auth/logout")
def auth_logout(request: Request):
    origin = request.headers.get("origin")
    if origin and urllib.parse.urlparse(origin).netloc != request.headers.get("host", ""):
        raise HTTPException(403, "cross-origin request refused")
    sess = _unsign(request.cookies.get("nl_session", ""))
    if sess:
        _audit(dict(actor=sess.get("email", "?"), role=sess.get("role", "?"), via="session"), "LOGOUT", "/auth/logout", 200)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("nl_session", path="/")
    return resp


@app.get("/api/auth/config")
def auth_config():
    """Public: what the sign-in screen needs before anyone is authenticated."""
    return {"sso": {"enabled": sso_enabled(), "provider": OIDC["provider_name"], "login_url": "/auth/login"},
            "token_required": bool(AUTH_TOKEN or READ_TOKEN) or HOSTED}


@app.get("/api/me")
def api_me(request: Request):
    who = getattr(request.state, "who", None)
    if who:
        return {k: who.get(k, "") for k in ("actor", "name", "role", "via")}
    return {"actor": "hosted-key", "name": "", "role": None, "via": "hosted"}


@app.get("/api/audit")
def api_audit(limit: int = 200, actor: str = "", method: str = "", path_prefix: str = "",
              since_minutes: float = 0, status: int = 0):
    """Who did what (admin role, Pro). Mutating API calls plus sign-ins; also reads when
    NEBULENS_AUDIT_READS=1."""
    _tier_gate("Audit log")
    where, args = ["1=1"], []
    if actor:
        where.append("actor=?"); args.append(actor)
    if method:
        where.append("method=?"); args.append(method.upper())
    if path_prefix:
        where.append("path LIKE ?"); args.append(path_prefix + "%")
    if status:
        where.append("status=?"); args.append(int(status))
    if since_minutes:
        where.append("ts>=?"); args.append(since_ns(since_minutes))
    return q(f"SELECT * FROM audit WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT ?",
             (*args, max(1, min(limit, 1000))))


@app.get("/api/audit/verify")
def api_audit_verify(limit: int = 100000):
    """Re-compute the hash chain and report the first row that does not match. An auditor can run
    this to show that no record was altered or removed. Rows written before chaining existed are
    reported as 'unchained' rather than as tampered with."""
    _tier_gate("Audit log")
    rows = q("SELECT * FROM audit ORDER BY id ASC LIMIT ?", (max(1, min(limit, 1000000)),))
    prev, checked, unchained = None, 0, 0

    def broken(r, why):
        return {"ok": False, "checked": checked, "unchained": unchained,
                "broken_at_id": r["id"], "broken_at_ts": r["ts"], "message": why}

    for r in rows:
        if not r.get("hash"):
            unchained += 1          # written before chaining existed; nothing to verify
            continue
        link = r.get("prev_hash", "") or ""
        # Each row stores the hash of the one before it. If that no longer matches the row we
        # actually just verified, a record between them was removed.
        if prev is not None and link != prev:
            return broken(r, "Gap in the audit chain: a record appears to have been deleted.")
        if _audit_row_hash(link, r) != r["hash"]:
            return broken(r, "An audit record was altered after it was written.")
        prev = r["hash"]; checked += 1
    return {"ok": True, "checked": checked, "unchained": unchained,
            "message": f"{checked} audit records verified; the chain is intact."
                       + (f" {unchained} older records predate hash chaining." if unchained else "")}


@app.get("/api/audit/export")
def api_audit_export(since_minutes: float = 0, limit: int = 100000):
    """Full audit trail as CSV, for handing to an auditor or loading into a SIEM."""
    _tier_gate("Audit log")
    where, args = ["1=1"], []
    if since_minutes:
        where.append("ts>=?"); args.append(since_ns(since_minutes))
    rows = q(f"SELECT * FROM audit WHERE {' AND '.join(where)} ORDER BY ts ASC LIMIT ?",
             (*args, max(1, min(limit, 1000000))))
    cols = ["id", "ts", "actor", "role", "via", "method", "path", "status", "detail", "ip", "req_id", "envs", "hash"]

    def esc(v):
        v = "" if v is None else str(v)
        return '"' + v.replace('"', '""') + '"' if any(ch in v for ch in ',"\n') else v

    def gen():
        yield ",".join(cols) + "\n"
        for r in rows:
            yield ",".join(esc(r.get(c, "")) for c in cols) + "\n"
    return StreamingResponse(gen(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=nebulens-audit.csv"})


@app.get("/api/license")
def license_info():
    """Plan, licensed footprint and what is in use. Over the node limit shows a banner; ingest is never refused."""
    return {"tier": TIER, "trial": bool(_TRIAL_ACTIVE) and not _LICENSE_VALID,
            "expires": (_LICENSE or {}).get("exp") or None, "customer": (_LICENSE or {}).get("customer") or None,
            **_license_usage(), "pricing_url": PRICING_URL}


@app.get("/api/mode")
def api_mode():
    """Which deployment mode this server runs in (public: the UI needs it before sign-in)."""
    trial = None
    if _TRIAL_START and not _LICENSE_VALID:
        trial = {"active": _TRIAL_ACTIVE, "days_left": _TRIAL_DAYS_LEFT,
                 "total_days": TRIAL_DAYS, "expired": not _TRIAL_ACTIVE and _TRIAL_DAYS_LEFT == 0}
    return {"mode": MODE, "tier": TIER, "is_pro": IS_PRO,
            "limits": TIER_LIMITS if not IS_PRO else None,
            "trial": trial,
            "features": {"multi_tenant": HOSTED, "organizations": HOSTED, "quotas": HOSTED,
                         "admin_api": HOSTED, "ai_assistant": _ai_ready()[0] and (IS_PRO or COMMUNITY_AI_DAILY > 0),
                         "sso": IS_PRO, "rbac": IS_PRO, "audit_log": IS_PRO, "postgres": IS_PRO or USE_PG,
                         "unlimited_dashboards": IS_PRO, "unlimited_alerts": IS_PRO,
                         "unlimited_slos": IS_PRO, "unlimited_services": IS_PRO}}


# ---- background workers
def _tenant_silos():
    """(db path or None, retention hours) for every database the background worker must service."""
    if not HOSTED:
        return [(None, RETENTION_HOURS)]
    return [(_project_path(r["pid"]), r["rh"] or RETENTION_HOURS)
            for r in pq("SELECT p.id pid, o.retention_hours rh FROM projects p JOIN orgs o ON o.id=p.org_id")]


def worker_tick(state, now=None):
    """One maintenance pass (alert rules, synthetics, retention) over every tenant silo.
    A failure in one tenant is logged and does not stop the others."""
    now = now or time.time()
    do_alert = now - state["last_alert"] >= 15
    do_prune = now - state["last_prune"] >= 600
    for path, retention in _tenant_silos():
        tok = _cur_db.set(path)
        try:
            if do_alert:
                evaluate_rules()
            try:
                _edge_rollup_step()
            except Exception as e:
                print("edge rollup error:", e)
            for ch in q("SELECT * FROM synthetics WHERE enabled=1"):
                r = q("SELECT MAX(ts) t FROM synthetic_runs WHERE check_id=?", (ch["id"],))[0]["t"] or 0
                if now - r >= ch["interval_s"]:
                    run_check(ch)
            if do_prune:
                cut = time.time_ns() - int(retention * 3600e9)
                mret = min(retention, METRICS_RETENTION_HOURS) if METRICS_RETENTION_HOURS > 0 else retention
                mcut = time.time_ns() - int(mret * 3600e9)
                # In small batches, each its own transaction, so a large backlog never holds the write
                # lock (and stalls ingest) for minutes; capped per tick, the rest follows on the next one.
                _prune_table("spans", "start_ns", cut); _prune_table("logs", "ts_ns", cut); _prune_table("metrics", "ts_ns", mcut)
                with db() as c:
                    if AUDIT_DAYS > 0:  # 0 = keep the audit trail forever
                        c.execute("DELETE FROM audit WHERE ts<?", (time.time_ns() - int(AUDIT_DAYS * 86400e9),))
                    c.execute("DELETE FROM synthetic_runs WHERE ts<?", (int(time.time() - retention * 3600),))
                    c.execute("DELETE FROM metric_names_seen WHERE hr<?", (int(mcut // 3_600_000_000_000),))
                    c.execute("DELETE FROM span_rollup WHERE minute<?", (int(cut // 60_000_000_000),))
                    c.execute("DELETE FROM edge_rollup WHERE minute<?", (int(cut // 60_000_000_000),))
                    c.execute("DELETE FROM resource_seen WHERE last_seen<?", (int(cut // 1_000_000_000),))
                    c.execute("DELETE FROM env_rollup WHERE minute<?", (int(cut // 60_000_000_000),))
        except Exception as e:
            print("worker error:", e)
        finally:
            _cur_db.reset(tok)
    if do_alert:
        state["last_alert"] = now
    if do_prune:
        state["last_prune"] = now


def _prune_table(table, col, cut, batch=20000, max_batches=100):
    """Delete rows older than cut, batch by batch."""
    if USE_PG:
        with db() as c:
            c.execute(f"DELETE FROM {table} WHERE {col}<?", (cut,))
        return
    for _ in range(max_batches):
        with db() as c:
            n = c.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {col}<? LIMIT ?)", (cut, batch)).rowcount
        if n < batch:
            return
        time.sleep(0.05)  # let ingest in between batches


def _rollup_set_from(v):
    with db() as c:
        c.execute("UPDATE rollup_state SET value=? WHERE key='from_minute'", (int(v),))


def _op_backfill(chunk_min=30, pause=0.2):
    """Fill op_rollup for spans stored before it existed, newest first; 'op_from' moves back per chunk and is 0 when done."""
    M = 60_000_000_000
    _t_start = time.time()
    if _marker("op_from") == 0:
        print("rollups ready: operations", flush=True)
        return
    try:
        oldest = qrows("SELECT MIN(start_ns) FROM spans")[0][0]
        floor_m = int(oldest // M) if oldest is not None else 1 << 62
        while True:
            hi = _marker("op_from")
            if oldest is None or hi <= floor_m:
                with db() as c:
                    c.execute("UPDATE rollup_state SET value=0 WHERE key='op_from'")
                print(f"rollups ready: operations (history converted in {time.time() - _t_start:.0f}s)", flush=True)
                return
            lo = max(floor_m, hi - chunk_min)
            rows = qrows("SELECT service, env, name, start_ns, status, dur_ms FROM spans "
                         "WHERE start_ns>=? AND start_ns<? AND (parent_id='' OR kind IN (2,5))", (lo * M, hi * M))
            with db() as c:
                ops = _op_aggregate(rows)
                if ops:
                    c.executemany(_OP_UPSERT, ops)
                c.execute("UPDATE rollup_state SET value=? WHERE key='op_from'", (lo,))
            time.sleep(pause)
    except Exception as e:
        print("operations rollup backfill stopped (will resume on next start):", e)


def _ops_ready(minutes):
    try:
        return (time.time_ns() - int(minutes * 60e9)) // 60_000_000_000 >= _marker("op_from")
    except Exception:
        return False


def _env_backfill(chunk_min=60, pause=0.2):
    """Fill env_rollup for spans and logs stored before it existed, newest first, one chunk per transaction; 'env_from' moves back
    with each committed chunk (live ingest only writes newer minutes, so nothing is counted twice) and is 0 when done."""
    M = 60_000_000_000
    _t_start = time.time()
    if _marker("env_from") == 0:
        print("rollups ready: environment counts", flush=True)
        return
    try:
        olds = [qrows(f"SELECT MIN({c}) FROM {t}")[0][0] for t, c in (("spans", "start_ns"), ("logs", "ts_ns"))]
        olds = [o for o in olds if o is not None]
        if not olds:
            with db() as c:
                c.execute("UPDATE rollup_state SET value=0 WHERE key='env_from'")
            print("rollups ready: environment counts (no history to convert)", flush=True)
            return
        floor_m = int(min(olds) // M)
        while True:
            hi = _marker("env_from")
            if hi <= floor_m:
                with db() as c:
                    c.execute("UPDATE rollup_state SET value=0 WHERE key='env_from'")
                print(f"rollups ready: environment counts (history converted in {time.time() - _t_start:.0f}s)", flush=True)
                return
            lo = max(floor_m, hi - chunk_min)
            acc = {}
            for m, env, svc, n, e in qrows("SELECT start_ns/60000000000, env, service, COUNT(*), SUM(CASE WHEN status=2 THEN 1 ELSE 0 END) "
                                           "FROM spans WHERE start_ns>=? AND start_ns<? GROUP BY 1, 2, 3", (lo * M, hi * M)):
                acc.setdefault((int(m), env or "", svc), [0, 0, 0, 0])[0:2] = [int(n), int(e or 0)]
            for m, env, svc, n, e in qrows("SELECT ts_ns/60000000000, env, service, COUNT(*), SUM(CASE WHEN severity>=17 THEN 1 ELSE 0 END) "
                                           "FROM logs WHERE ts_ns>=? AND ts_ns<? GROUP BY 1, 2, 3", (lo * M, hi * M)):
                acc.setdefault((int(m), env or "", svc), [0, 0, 0, 0])[2:4] = [int(n), int(e or 0)]
            with db() as c:
                rows = [(*k, *v) for k, v in sorted(acc.items())]
                if rows:
                    c.executemany(_ENV_UPSERT, rows)
                c.execute("UPDATE rollup_state SET value=? WHERE key='env_from'", (lo,))
            time.sleep(pause)
    except Exception as e:
        print("environment rollup backfill stopped (will resume on next start):", e)


def _res_backfill(hours=24):
    """Fill resource_seen from rows stored before it existed: the distinct resources of the last `hours`, one grouped query per
    table. Until it finishes, windows fall back to the old sampled scan; afterwards windows up to `hours` long use the registry."""
    _t_start = time.time()
    try:
        t_from = time.time_ns() - int(hours * 3600e9)
        for table, tcol, sig in (("spans", "start_ns", "spans"), ("logs", "ts_ns", "logs"), ("metrics", "ts_ns", "metrics")):
            rows = qrows(f"SELECT service, env, res, MAX({tcol}) FROM {table} WHERE {tcol}>=? GROUP BY service, env, res", (t_from,))
            batch = {}
            for svc, env, res, last in rows:
                if table == "logs" and svc == "unknown":
                    pass
                d = _as_dict(res)
                a = d.get("attributes") if isinstance(d.get("attributes"), dict) else d
                if not (svc and isinstance(a, dict)):
                    continue
                g = lambda k: str(a.get(k) or "")
                key = (sig, svc, g("k8s.cluster.name"), str(a.get("k8s.namespace.name") or a.get("service.namespace") or ""),
                       g("k8s.node.name"), g("k8s.pod.name"), g("k8s.deployment.name"), g("telemetry.sdk.language"),
                       g("telemetry.sdk.version"), g("service.version"), env or "")
                batch[key] = max(batch.get(key, 0), int(last // 1_000_000_000))
            with db() as c:
                c.executemany(_RES_UPSERT, [(*k, ts) for k, ts in sorted(batch.items())])
        with db() as c:
            c.execute("UPDATE rollup_state SET value=? WHERE key='res_from'", (int(t_from // _MIN_NS),))
        print(f"rollups ready: Kubernetes resource registry (last {hours:g} h, {time.time() - _t_start:.0f}s)", flush=True)
    except Exception as e:
        print("resource registry backfill stopped (the Kubernetes page keeps using the slower scan):", e)


def _rollup_backfill(chunk_min=15, pause=0.2):
    """Fill span_rollup for spans stored before rollups existed, newest first, one chunk per transaction.
    'from_minute' moves back with each committed chunk, so recent windows start using the rollup right away and a
    restart resumes where it stopped (no double counting: live ingest only writes minutes >= from_minute)."""
    _t_start = time.time()
    if _rollup_from() == 0:
        print("rollups ready: span rollup", flush=True)
        return
    try:
        oldest = qrows("SELECT MIN(start_ns) FROM spans")[0][0]
        if oldest is None:
            _rollup_set_from(0)     # nothing stored yet, so the rollup is complete for every window
            print("rollups ready: span rollup (no history to convert)", flush=True)
            return
        floor_m = int(oldest // 60_000_000_000)
        while True:
            hi = _rollup_from()
            if hi <= floor_m:
                _rollup_set_from(0)     # no spans older than this exist, so older windows are empty anyway
                print(f"rollups ready: span rollup (history converted in {time.time() - _t_start:.0f}s)", flush=True)
                return
            lo = max(floor_m, hi - chunk_min)
            rows = qrows("SELECT service, env, start_ns, status, dur_ms FROM spans "
                         "WHERE start_ns>=? AND start_ns<? AND (parent_id='' OR kind=2)",
                         (lo * 60_000_000_000, hi * 60_000_000_000))
            with db() as c:
                _rollup_write(c, _rollup_aggregate(rows))
                c.execute("UPDATE rollup_state SET value=? WHERE key='from_minute'", (lo,))
            time.sleep(pause)
    except Exception as e:
        print("rollup backfill stopped (will resume on next start):", e)


def _ensure_metrics_time_index():
    """metrics had no index starting with the timestamp, so every time-windowed read and the retention
    delete scanned the whole table. Created in the background on its own connection so a large existing
    table can never block startup (a blocked start would trip the readiness probe and restart the pod,
    which would then roll the build back and never finish). Instant on a new or small database."""
    try:
        c = sqlite3.connect(DB_PATH, timeout=900)
        t = time.time()
        c.execute("CREATE INDEX IF NOT EXISTS ix_metrics_ts ON metrics(ts_ns)")
        # Trace detail looked up its logs by trace_id with no index (a full logs scan per trace click), and "errors only"
        # trace lists walked every span of the window to find the rare failed ones.
        c.execute("CREATE INDEX IF NOT EXISTS ix_logs_trace ON logs(trace_id) WHERE trace_id<>''")
        c.execute("CREATE INDEX IF NOT EXISTS ix_spans_err ON spans(start_ns) WHERE status=2")
        c.execute("CREATE INDEX IF NOT EXISTS ix_spans_svc_err ON spans(service, start_ns) WHERE status=2")
        c.commit(); c.close()
        if time.time() - t > 2:
            print(f"built index ix_metrics_ts in {time.time() - t:.0f}s")
    except Exception as e:
        print("metrics index build failed (will retry next start):", e)


def _ensure_pg_indexes():
    """Postgres: the same lookup indexes, built CONCURRENTLY so ingest keeps writing while they build."""
    try:
        with psycopg.connect(DATABASE_URL, autocommit=True) as c:
            for ddl in (f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_logs_trace ON {PG_SCHEMA}.logs(trace_id) WHERE trace_id<>''",
                        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_spans_err ON {PG_SCHEMA}.spans(start_ns) WHERE status=2",
                        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_spans_svc_err ON {PG_SCHEMA}.spans(service, start_ns) WHERE status=2"):
                c.execute(ddl)
    except Exception as e:
        print("index build failed (will retry next start):", e)


def worker():
    state = {"last_alert": 0, "last_prune": 0}
    while True:
        try:
            worker_tick(state)
        except Exception as e:
            print("worker error:", e)
        time.sleep(5)


def _warm_jobs():
    """The heavy views kept warm, each with the exact arguments the UI sends (the cache key includes every argument)."""
    jobs = []
    for m in (15.0, 60.0, 1440.0):     # 24 h too: demos widen the range, and the first wide request paid 1-3 s
        jobs += [lambda m=m: services.refresh(m), lambda m=m: insights.refresh(m), lambda m=m: home.refresh(m, 60),
                 lambda m=m: service_map.refresh(m), lambda m=m: logs_hist.refresh(m, "", 60),
                 lambda m=m: logs_facets.refresh(m, sort="errors"), lambda m=m: logs_facets.refresh(m)]
    jobs.append(lambda: _saturation_rows.refresh(60.0))
    return jobs


def cache_warmer():
    """Self-hosted only: while someone has used the dashboards in the last 5 minutes, keep the heavy aggregates warm
    so page loads are instant. Idle instances do no extra work. Starts warm so the first visit after a restart is fast."""
    _last_use[0] = time.time()
    while True:
        busy = 0.0
        if CACHE_TTL > 0 and time.time() - _last_use[0] < 300:
            t = time.time(); _warming.on = True
            try:
                for job in _warm_jobs():
                    try:
                        job()
                    except Exception as e:   # one failing view must not stop the others from being warmed
                        print("cache warmer error:", getattr(job, "__name__", job), e, flush=True)
            finally:
                _warming.on = False
            busy = time.time() - t
        time.sleep(max(8.0, busy))  # never more than ~50% duty cycle


def _warn_if_open():
    if not HOSTED and not (AUTH_TOKEN or READ_TOKEN or sso_enabled()):
        print("WARNING: no NEBULENS_TOKEN (and no SSO) is set: anyone who can reach this port has ADMIN access to NebuLens and its "
              "data. Set NEBULENS_TOKEN (Helm: --set auth.token=$(openssl rand -hex 24)).", flush=True)


def _start():
    _warn_if_open()
    threading.Thread(target=worker, daemon=True).start()
    if not USE_PG:
        threading.Thread(target=_ensure_metrics_time_index, daemon=True).start()
    else:
        threading.Thread(target=_ensure_pg_indexes, daemon=True).start()
    if not HOSTED:
        threading.Thread(target=_rollup_backfill, daemon=True).start()
        threading.Thread(target=_res_backfill, daemon=True).start()
        threading.Thread(target=_env_backfill, daemon=True).start()
        threading.Thread(target=_op_backfill, daemon=True).start()
        threading.Thread(target=cache_warmer, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/sdk/nebulens-rum.js")
@app.get("/sdk/oscilyx-rum.js")
@app.get("/sdk/pulse0-rum.js")   # the old URL, so pages that already embed it keep reporting
def rum_sdk():
    """The browser RUM SDK, served from its own URL so a <script src> tag is short and stable."""
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(STATIC, "sdk", "nebulens-rum.js"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "public, max-age=3600"})

if __name__ == "__main__":
    import uvicorn
    # TLS straight in the process, for sites with no ingress or service mesh to terminate it.
    #   NEBULENS_TLS_CERT / NEBULENS_TLS_KEY   serve HTTPS
    #   NEBULENS_TLS_CLIENT_CA               also require a client certificate (mTLS), so only
    #                                      collectors holding a cert signed by that CA can send data
    opts = {}
    cert, key = os.environ.get("NEBULENS_TLS_CERT", ""), os.environ.get("NEBULENS_TLS_KEY", "")
    if cert and key:
        opts.update(ssl_certfile=cert, ssl_keyfile=key)
        kp = os.environ.get("NEBULENS_TLS_KEY_PASSWORD", "")
        if kp:
            opts["ssl_keyfile_password"] = kp
        ca = os.environ.get("NEBULENS_TLS_CLIENT_CA", "")
        if ca:
            opts.update(ssl_ca_certs=ca, ssl_cert_reqs=ssl.CERT_REQUIRED)
            print("TLS enabled with mutual authentication (client certificate required)")
        else:
            print("TLS enabled")
    elif cert or key:
        raise RuntimeError("NEBULENS_TLS_CERT and NEBULENS_TLS_KEY must both be set")
    uvicorn.run(app, host=os.environ.get("NEBULENS_HOST", "127.0.0.1"),
                port=int(os.environ.get("NEBULENS_PORT", "4318")), **opts)
