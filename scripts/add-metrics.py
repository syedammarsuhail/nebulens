#!/usr/bin/env python3
"""Turn on extra CPU / memory metrics in the running Oscilyx collector (kubeletstats + hostmetrics, collector 0.111.0).
Edits the ConfigMap oscilyx-collector in place and prints the restart command. Safe to run twice."""
import json, re, subprocess, sys

NS, CM = "observability", "oscilyx-collector"
EXTRA = [  # all exist in otel-collector-contrib 0.111.0 and are off by default
    "container.cpu.usage", "k8s.pod.cpu.usage", "k8s.node.cpu.usage",                      # CPU in cores (not cumulative seconds)
    "k8s.container.cpu_limit_utilization", "k8s.container.memory_limit_utilization",       # % of the container's limit
    "k8s.container.cpu_request_utilization", "k8s.container.memory_request_utilization",   # % of the container's request
    "k8s.pod.cpu_limit_utilization", "k8s.pod.memory_limit_utilization",
]

def patch(cfg):
    lines = cfg.split("\n")
    try:
        start = next(i for i, l in enumerate(lines) if re.match(r"^\s+kubeletstats:\s*$", l))
    except StopIteration:
        sys.exit("kubeletstats receiver not found in the collector config; nothing changed")
    base = len(lines[start]) - len(lines[start].lstrip())
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > base):
        end += 1
    block = lines[start:end]
    pad = " " * (base + 2)
    mi = next((i for i, l in enumerate(block) if re.match(rf"^{pad}metrics:\s*$", l)), None)
    have = {m.group(1) for l in block if (m := re.match(rf"^{pad}  ([a-z0-9_.]+):\s*$", l))} if mi is not None else set()
    add = [m for m in EXTRA if m not in have]
    if not add:
        return cfg, []
    new = [f"{pad}  {m}:\n{pad}    enabled: true" for m in add]
    if mi is None:
        last = max(i for i, l in enumerate(block) if l.strip())
        block[last + 1:last + 1] = [f"{pad}metrics:"] + [x for n in new for x in n.split("\n")]
    else:
        block[mi + 1:mi + 1] = [x for n in new for x in n.split("\n")]
    return "\n".join(lines[:start] + block + lines[end:]), add

HOST = {"cpu": "system.cpu.utilization", "memory": "system.memory.utilization"}   # off by default in hostmetrics

def patch_host(cfg):
    """Turn `cpu: {}` / `memory: {}` under the hostmetrics scrapers into blocks that enable the utilization metrics."""
    out, added, host_indent = [], [], None
    for l in cfg.split("\n"):
        indent = len(l) - len(l.lstrip())
        if host_indent is None:
            if re.match(r"^\s+hostmetrics:\s*$", l):
                host_indent = indent
        elif l.strip() and indent <= host_indent:          # left the hostmetrics block
            host_indent = None
        m = re.match(r"^(\s+)(cpu|memory): \{\}\s*$", l) if host_indent is not None else None
        if m:
            ind, scraper = m.group(1), m.group(2)
            out += [f"{ind}{scraper}:", f"{ind}  metrics:", f"{ind}    {HOST[scraper]}:", f"{ind}      enabled: true"]
            added.append(HOST[scraper])
        else:
            out.append(l)
    return "\n".join(out), added

def sh(*a):
    r = subprocess.run(a, capture_output=True, text=True)
    if r.returncode: sys.exit(r.stderr.strip() or "command failed: " + " ".join(a))
    return r.stdout

if __name__ == "__main__":
    cfg = sh("kubectl", "-n", NS, "get", "cm", CM, "-o", "jsonpath={.data.config\\.yaml}")
    new, added = patch(cfg)
    new, host_added = patch_host(new)
    added += host_added
    if not added:
        print("Already enabled. Nothing to do."); sys.exit(0)
    sh("kubectl", "-n", NS, "patch", "cm", CM, "--type", "merge", "-p", json.dumps({"data": {"config.yaml": new}}))
    print("Enabled:", *added, sep="\n  ")
    print(f"\nNow restart the collector:\n  kubectl -n {NS} rollout restart ds/{CM}")
