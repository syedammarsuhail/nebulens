#!/usr/bin/env python3
"""Oscilyx auto-injection.

Annotates workloads so the OpenTelemetry Operator injects the right language agent. Replaces
running scripts/instrument.sh by hand for every deployment.

Design rules:
  * Namespaced RBAC only (get/list/patch on workloads in the listed namespaces). No pods/exec.
  * Language comes from an explicit override first, then from the pod spec. Anything it cannot
    classify with confidence is SKIPPED and reported, never guessed (a wrong agent crash-loops pods).
  * Skips workloads that already carry an inject annotation or another APM agent.
  * Opt-out per workload with the label  oscilyx.io/instrument: "false".
  * Explicit language per workload with the label  oscilyx.io/language: <java|dotnet|python|nodejs|go>.
  * maxChangesPerRun caps how many workloads are touched per run, so restarts are staggered.
  * dryRun (default true) only logs what it would change.

Stdlib only, so it runs in any image that has python3.
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://%s:%s" % (
    os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc"),
    os.environ.get("KUBERNETES_SERVICE_PORT", "443"),
)

CFG = json.loads(os.environ.get("AUTOINJECT_CONFIG", "{}"))
NAMESPACES = CFG.get("namespaces", [])
KINDS = CFG.get("kinds", ["deployments", "statefulsets"])
DRY = bool(CFG.get("dryRun", True))
MAX_CHANGES = int(CFG.get("maxChangesPerRun", 3))
INSTR = CFG.get("instrumentation", "")          # "<namespace>/<name>" of the Instrumentation CR
OVERRIDES = CFG.get("overrides", {})            # {"namespace/name": "dotnet"}
EXCLUDE = set(CFG.get("exclude", []))           # "name" or "namespace/name"

PREFIX = "instrumentation.opentelemetry.io/inject-"
RUNTIME_KEY = "instrumentation.opentelemetry.io/otel-dotnet-auto-runtime"
LANGS = ("java", "dotnet", "python", "nodejs", "go")

try:
    with open(SA + "/token") as f:
        TOKEN = f.read().strip()
    CTX = ssl.create_default_context(cafile=SA + "/ca.crt")
except OSError as exc:  # pragma: no cover - only outside a cluster
    TOKEN, CTX = "", None
    if not os.environ.get("AUTOINJECT_TEST"):
        sys.exit("not running in a cluster: %s" % exc)


def call(method, path, body=None, ctype="application/json"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, method=method, data=data)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, context=CTX, timeout=30) as resp:
        return json.load(resp)


def words(container):
    cmd = (container.get("command") or []) + (container.get("args") or [])
    return set(" ".join(cmd).lower().replace("/", " ").replace("=", " ").split())


def env_names(container):
    return {e.get("name", "").upper() for e in container.get("env") or []}


def detect(container):
    """Return the set of languages the first container looks like. One hit = confident."""
    w, env = words(container), env_names(container)
    image = container.get("image", "").lower().rsplit("/", 1)[-1]
    hits = set()
    if "dotnet" in w or any(k.startswith(("ASPNETCORE_", "DOTNET_")) for k in env) \
            or any(s in image for s in ("dotnet", "aspnet")):
        hits.add("dotnet")
    if w & {"java", "-jar"} or env & {"JAVA_OPTS", "JAVA_HOME", "JDK_JAVA_OPTIONS"} \
            or any(s in image for s in ("jdk", "jre", "java", "temurin", "corretto", "tomcat")):
        hits.add("java")
    if w & {"node", "npm", "yarn", "pnpm"} or env & {"NODE_ENV", "NODE_OPTIONS"} \
            or image.startswith("node"):
        hits.add("nodejs")
    if w & {"python", "python3", "gunicorn", "uvicorn", "flask", "celery"} \
            or env & {"PYTHONPATH", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE"} \
            or image.startswith("python"):
        hits.add("python")
    return hits


AGENT_PREFIXES = ("ELASTIC_APM", "DD_AGENT", "DD_TRACE", "NEW_RELIC", "NEWRELIC",
                  "APPDYNAMICS", "DYNATRACE", "SW_AGENT", "OTEL_")


def has_other_agent(container):
    for e in container.get("env") or []:
        name = e.get("name", "").upper()
        if name.startswith(AGENT_PREFIXES):
            return True
        if name in ("JAVA_TOOL_OPTIONS", "JAVA_OPTS", "CATALINA_OPTS") \
                and "-javaagent" in (e.get("value") or ""):
            return True
    return False


def decide(ns, workload):
    """Return (patch_annotations | None, reason)."""
    meta = workload["metadata"]
    name = meta["name"]
    key = "%s/%s" % (ns, name)
    labels = meta.get("labels") or {}
    tpl = workload["spec"]["template"]
    ann = (tpl.get("metadata") or {}).get("annotations") or {}
    containers = tpl["spec"].get("containers") or []

    if (labels.get("oscilyx.io/instrument", labels.get("pulse0.io/instrument")) == "false"   # old label still honoured
            or name in EXCLUDE or key in EXCLUDE):
        return None, "opted out"
    present = [k for k in ann if k.startswith(PREFIX)]
    if present:
        return None, "already annotated (%s)" % present[0][len(PREFIX):]
    if not containers:
        return None, "no containers"
    first = containers[0]

    lang = OVERRIDES.get(key) or labels.get("oscilyx.io/language") or labels.get("pulse0.io/language")
    if not lang:
        hits = detect(first)
        if len(hits) == 1:
            lang = next(iter(hits))
        elif not hits:
            return None, "language unknown (set label oscilyx.io/language)"
        else:
            return None, "ambiguous: %s (set label oscilyx.io/language)" % "/".join(sorted(hits))
    if lang not in LANGS:
        return None, "invalid language '%s'" % lang
    if has_other_agent(first):
        return None, "already has another APM/OTel agent"

    patch = {PREFIX + lang: INSTR}
    if lang == "dotnet" and "alpine" in first.get("image", "").lower():
        patch[RUNTIME_KEY] = "linux-musl-x64"
    return patch, "inject-%s" % lang


def main():
    if not INSTR or not NAMESPACES:
        sys.exit("config needs 'instrumentation' and at least one namespace")
    changed = 0
    print("autoinject: namespaces=%s dryRun=%s maxChangesPerRun=%d" % (NAMESPACES, DRY, MAX_CHANGES))
    for ns in NAMESPACES:
        for kind in KINDS:
            try:
                items = call("GET", "/apis/apps/v1/namespaces/%s/%s" % (ns, kind))["items"]
            except urllib.error.HTTPError as exc:
                print("[%s] cannot list %s: HTTP %s" % (ns, kind, exc.code))
                continue
            for w in items:
                name = w["metadata"]["name"]
                patch, reason = decide(ns, w)
                tag = "%s/%s/%s" % (ns, kind, name)
                if patch is None:
                    print("  skip  %-60s %s" % (tag, reason))
                    continue
                if changed >= MAX_CHANGES:
                    print("  defer %-60s %s (maxChangesPerRun reached)" % (tag, reason))
                    continue
                body = {"spec": {"template": {"metadata": {"annotations": patch}}}}
                if DRY:
                    print("  would %-60s %s" % (tag, reason))
                else:
                    call("PATCH", "/apis/apps/v1/namespaces/%s/%s/%s" % (ns, kind, name), body,
                         "application/strategic-merge-patch+json")
                    print("  patch %-60s %s" % (tag, reason))
                changed += 1
    print("autoinject: %d workload(s) %s" % (changed, "would change" if DRY else "changed"))


if __name__ == "__main__":
    main()
