#!/usr/bin/env python3
"""Create the "Cluster - CPU & Memory" dashboard in Oscilyx and check that every panel has data.
Usage:  python3 create-dashboards.py [--url http://<oscilyx-host>:4318] [--token TOKEN]
        (default http://localhost:4318, e.g. with: kubectl -n observability port-forward svc/oscilyx 4318:4318)
The token defaults to $OSCILYX_TOKEN, or is read from the oscilyx-auth Secret with kubectl. Safe to run again (it updates the
dashboard by name)."""
import argparse, json, os, subprocess, sys, urllib.parse, urllib.request

NAME = "Cluster - CPU & Memory"
P = lambda i, title, metric, agg, group, typ="area": {"id": f"cm-{i}", "type": typ, "source": "metric", "title": title,
                                                      "w": 6, "h": 3, "metric": metric, "agg": agg, "group_by": group}
PANELS = [
    P(1, "Node CPU utilisation (1 = 100%)", "k8s.node.cpu.utilization", "avg", "k8s.node.name"),
    P(2, "Node memory in use (bytes)", "k8s.node.memory.working_set", "avg", "k8s.node.name"),
    P(3, "CPU by pod (cores)", "k8s.pod.cpu.usage", "avg", "k8s.pod.name"),
    P(4, "Memory by pod (bytes)", "k8s.pod.memory.working_set", "avg", "k8s.pod.name"),
    P(5, "Container CPU, share of its limit (1 = 100%)", "k8s.container.cpu_limit_utilization", "max", "k8s.container.name", "line"),
    P(6, "Container memory, share of its limit (1 = 100%)", "k8s.container.memory_limit_utilization", "max", "k8s.container.name", "line"),
    P(7, "Container restarts", "k8s.container.restarts", "max", "k8s.pod.name", "bar"),
    P(8, "Host CPU utilisation (by state)", "system.cpu.utilization", "avg", "state"),
    P(9, "Host memory utilisation (by state)", "system.memory.utilization", "avg", "state"),
    P(10, "Java CPU utilisation (by service)", "jvm.cpu.recent_utilization", "avg", "", "line"),
]

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--url", default="http://localhost:4318"); ap.add_argument("--token", default=os.environ.get("OSCILYX_TOKEN", ""))
    a = ap.parse_args(); base = a.url.rstrip("/"); token = a.token
    if not token:
        try:
            token = subprocess.run(["sh", "-c", "kubectl -n observability get secret oscilyx-auth -o jsonpath='{.data.token}' | base64 -d"],
                                   capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            token = ""
    def call(path, body=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
        try:
            # Reviewed: the operator's own Oscilyx URL from --url.
            with urllib.request.urlopen(req, timeout=60) as r:  # nosec B310
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            sys.exit(f"{path}: HTTP {e.code} {e.read().decode()[:200]}")
        except Exception as e:
            sys.exit(f"{path}: {e}")
    call("/api/dashboards", {"name": NAME, "panels": PANELS})
    d = next(x for x in call("/api/dashboards") if x["name"] == NAME)
    print(f"Dashboard saved: {base}/#/dashboards/{d['id']}\n")
    print(f"{'PANEL':52} RESULT")
    missing = 0
    for p in PANELS:
        qs = urllib.parse.urlencode({"name": p["metric"], "minutes": 60, "buckets": 40, "agg": p["agg"], "group_by": p["group_by"]})
        s = call("/api/metrics/query?" + qs).get("series", {})
        n = sum(1 for v in s.values() if any(x is not None for x in v))
        missing += n == 0
        print(f"{p['title'][:50]:52} " + (f"OK, {n} series" if n else f"NO DATA in the last hour  ({p['metric']})"))
    print("\nPanels showing NO DATA: the metric is not being collected (yet). Wait 2-3 minutes after enabling it, and check the namespace filter.\n" if missing else "\nAll panels have data.")

if __name__ == "__main__":
    main()
