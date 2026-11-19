> **Chart 0.4.2** — **Tracing for every app with no code changes and no restarts (eBPF).** Same image (`nebulens_11_1`).
> - New `ebpf.enabled=true`: one OpenTelemetry eBPF Instrumentation (OBI v1.0) agent per node traces every application in every
>   namespace from the Linux kernel (HTTP, gRPC, SQL, Redis, Kafka and more; Java, .NET, Go, Python, Node.js, Ruby, Rust). Running pods
>   are picked up within seconds, calls between services are joined into one trace, and spans go through the NebuLens collector.
>   NebuLens' own namespace is never traced. Needs Linux 5.8+ (RHEL 8 works) and a privileged DaemonSet.
>   Turn on with `helm upgrade nebulens ./helm/nebulens -n observability --reset-then-reuse-values --set ebpf.enabled=true`.
> - Tested on EKS: apps with no OpenTelemetry code were traced across three services within a minute, without restarting them.
> - `scripts/eks.sh` creates the gp3 StorageClass that new EKS clusters lack.
> - `values-baremetal.yaml` now turns eBPF tracing on by default (set `ebpf.enabled=false` to opt out).
> - New `helm/nebulens/values-full.yaml`: everything on for a full test (collector keeping every trace, eBPF, the OpenTelemetry
>   Operator and automatic agent injection). Use it with your platform overlay; see GETTING-STARTED, "Everything at once".
>
> **v11.1** — **Pulse0 is now NebuLens.** Image tag `nebulens_11_1`, chart `nebulens` `0.4.1` (includes v10.23 and earlier below).
> v11.0 was published briefly under the name Oscilyx; v11.1 replaces it. Everything below that keeps Pulse0 installs working
> applies to Oscilyx installs too (`OSCILYX_*` settings, `oscilyx.db`, the `oscilyx` Postgres schema, `x-oscilyx-*` headers and
> `/sdk/oscilyx-rum.js`).
> - **New name everywhere you see it:** the UI, browser title, logo, the assistant ("Ask NebuLens"), docs, the Helm chart
>   (`helm/nebulens`), the VM files (`deploy/vm/nebulens.env.example`, `nebulens.service`) and the package
>   (`nebulens-onprem-v11_1.tar.gz`).
> - **New, more professional colour theme** (deep-space indigo with a violet nebula accent, in dark and light mode) and a new lens logo.
> - **Existing Pulse0 installs keep working without changes:**
>   - Settings: `NEBULENS_*` are the new names; every old `PULSE0_*` variable is still read (when both are set, `NEBULENS_*` wins).
>   - Data: an existing `pulse0.db` is used while no `nebulens.db` exists beside it; on Postgres the `pulse0` schema is used when the
>     database has no `nebulens` schema yet (unless `NEBULENS_PG_SCHEMA` / `postgres.schema` says otherwise).
>   - Ingest and API: the `x-pulse0-key` / `x-pulse0-project` headers are still accepted next to `x-nebulens-key` /
>     `x-nebulens-project`. The browser script is also served at its old URL `/sdk/pulse0-rum.js`, and `window.pulse0` still works.
>   - Kubernetes auto-instrumentation still honours the `pulse0.io/instrument` and `pulse0.io/language` labels.
>   - Users signed in with SSO sign in once more (the session cookie was renamed).
> - **Helm upgrade of an existing `pulse0` release** (keeps the same StatefulSet and volume):
>   `helm -n observability upgrade pulse0 ./helm/nebulens --reset-then-reuse-values --set nameOverride=pulse0 --set fullnameOverride=pulse0 --set-string image.tag=nebulens_11_1`.
>   Without the two overrides Kubernetes would create new resources with a new, empty volume. New installs need neither.
>
> **v10.23** — image tag `pulse_10_23`, chart `0.3.4` (includes v10.22 and earlier below). Security release; every change below
> was found by scanning (pip-audit, bandit, a manual review of SQL, outbound calls, the UI and the chart).
> - **Chart refuses placeholder admin tokens.** `values-baremetal.yaml` shipped `token: "change-me"` (and the EKS / OpenShift
>   overlays similar text): installing with one of them made a publicly known value the admin password. `helm install` now
>   stops with a message telling you to set `--set auth.token=$(openssl rand -hex 24)`.
> - **No token at all** now prints a loud warning in the server log and the Helm install notes (anyone who can reach the port is
>   an admin). Not blocked, so existing installs keep working.
> - **Synthetic checks and alert webhooks can no longer reach the cloud metadata service** (169.254.169.254 and other link-local
>   addresses, which hand out instance credentials), in every mode, and redirects are re-checked. Private addresses stay allowed
>   on self-hosted installs (checking internal services is what synthetics are for).
> - The AI endpoint (`NEBULENS_AI_BASE_URL`) must be http(s).
> - **New: remove an environment** (Environments page, *Remove* on each card): *Remove from list* (keeps the data; editors) or
>   *Remove and delete its data* (permanently deletes its spans, logs, metrics and summaries, in the background; admins only,
>   recorded in the audit log). API: `DELETE /api/environments/{slug}`, `POST /api/environments/{slug}/purge`.
> - **New: `scripts/release-check.sh`**, the release gate (tests, pip-audit, bandit, secrets, paid-code check, Helm, UI). Run it
>   before every release. **New: `.github/workflows/security.yml`**: the same gate plus Snyk (dependencies and code) and
>   SonarCloud with its quality gate; images are published only on a version tag and only after all of them pass.
> - Scan results for this release: 0 known vulnerabilities in the pinned dependencies; bandit 0 high, every medium reviewed and
>   documented in the code; no SQL injection (all interpolated SQL parts are fixed names or `?` placeholders); no secrets.
>
> **v10.22** — image tag `pulse_10_22`, chart `0.3.3` (includes v10.20 and earlier below). Adds the metrics and dashboard work from a
> parallel "v10.21" build (which was built on an older base and should not be installed: it lacks the v10.8-v10.20 fixes).
> - **Fix: metric charts grouped by `k8s.pod.name`, `k8s.container.name` or `k8s.node.name` showed one merged line.** Those
>   attributes are on the resource and grouping only looked at the data point; it now looks at both. Fixes by-pod / by-container /
>   by-node panels and the Metrics page "group by".
> - **New chart settings:** `collector.hostUtilization` (default true: host `system.cpu.utilization` / `system.memory.utilization`,
>   which the collector leaves off) and `collector.kubeletMetrics` (default: `k8s.pod.cpu.usage`,
>   `k8s.container.cpu_limit_utilization`, `k8s.container.memory_limit_utilization`). Upgrade with `--reset-then-reuse-values` so
>   the new defaults apply (`--reuse-values` ignores keys that are new in the chart).
> - **`scripts/create-dashboards.py`** creates a *Cluster - CPU & Memory* dashboard (10 panels) and reports which panels have data;
>   safe to run again (updates by name). Default address `http://localhost:4318`; pass `--url` and `--token` otherwise.
>   **`scripts/add-metrics.py`** enables the same extra metrics on a running cluster without a Helm upgrade.
> - Checked: the dashboard script against a running server (all 10 panels with data, per-pod and per-node series), a second run
>   updates instead of failing, building a dashboard in the UI (no console errors), and the Community limit (10 dashboards) answers
>   with the upgrade prompt while updating an existing dashboard still works.
>
> **v10.20** — image tag `pulse_10_20`. Same code as v10.19, plus documentation: **GETTING-STARTED.md**, a step-by-step guide for a
> first-time install (download, build, install, first login, data in, AI assistant, editions, upgrade, troubleshooting, security).
> ON-PREM.md: the Community service limit in Troubleshooting now says 10 (it said 5; the limit has been 10).
>
> **v10.19** — image tag `pulse_10_19` (includes v10.18 and earlier below).
> - **AI answers are Pro, with a free daily allowance on Community.** Community gets `NEBULENS_COMMUNITY_AI_DAILY` AI answers per day
>   (default 20, resets 00:00 UTC; `0` = Pro-only); then the panel offers Pro and keeps answering with the free rule-based analysis.
>   Pro and the trial are unlimited and show a `PRO` (or `PRO · trial`) tag. The panel shows "n of N free today", an "AI answers · PRO"
>   card on opening and the moment the last free answer is used, and Settings → AI assistant shows the plan and the allowance.
>   `/api/assistant/status` adds `pro`, `trial`, `quota`, `locked`.
> - **Service map:** the minimap (bottom right) is off by default; the new button under the zoom controls turns it on (remembered
>   per browser). New calls appear on the map immediately again on small installs and demos (the newest minutes are included
>   automatically while traffic is light, `NEBULENS_MAP_TAIL=auto`; under heavy load the map trails by about 2 minutes as in v10.12).
>
> **v10.18** — image tag `pulse_10_18` (includes v10.17 and earlier below).
> - **The AI assistant works with any LLM, not only Claude.** `NEBULENS_AI_PROVIDER=anthropic` (Claude, official SDK, unchanged) or
>   `NEBULENS_AI_PROVIDER=openai` for any OpenAI-compatible chat-completions endpoint: OpenAI, Azure OpenAI (`NEBULENS_AI_KEY_HEADER=api-key`),
>   Groq, Mistral, OpenRouter, Together, or a self-hosted model (Ollama, vLLM, LM Studio) so nothing leaves your network.
>   Settings: `NEBULENS_AI_BASE_URL`, `NEBULENS_AI_MODEL`, `NEBULENS_AI_API_KEY` (optional for local servers), `NEBULENS_AI_NAME` (UI label).
>   The model must support tool (function) calling. No new dependency: the OpenAI-compatible path uses the standard library.
> - **Existing installs keep working:** `ANTHROPIC_API_KEY` alone still means Claude. In Helm, `ai.provider` defaults to `anthropic`
>   and the old secret name still works; new values `ai.provider`, `ai.baseUrl`, `ai.model`, `ai.name`, `ai.keyHeader`.
> - UI text no longer assumes Claude: the assistant shows the configured model's name, and Settings → AI assistant shows ready-to-copy
>   setup for Claude, OpenAI-compatible providers and self-hosted models, plus why AI is off when it is.
> - `/api/assistant/status` and `/api/assistant` now report `mode: "llm"` (was `"claude"`) plus `provider` and `name`.
>
> **v10.17** — image tag `pulse_10_17` (includes v10.16 and earlier below).
> - **New logo:** the NebuLens mark is now a heartbeat (ECG) line on the brand gradient, in the header and as the browser-tab icon.
>
> **v10.16** — image tag `pulse_10_16` (includes v10.15 and earlier below).
> - **Fix: the "Get Pro" dialog was covered by the trial banner** (the banner sat above dialogs, drawers and the search palette), and
>   the dialog opened scrolled to its bottom. It now opens at the top, behind-the-dialog content is dimmed, and no dialog scrolls
>   away from its top to focus a field.
> - Measured on EKS: the Environments page (v10.15 change) went from 7.0 s to about 20-50 ms.
>
> **v10.15** — image tag `pulse_10_15` (includes v10.14 and earlier below).
> - **Environments page: 7.0 s -> fast.** The page also loads `/api/environments/discover` for the last 24 hours and waits for it;
>   that call scanned a whole day of spans and logs. It now reads the per-minute environment counts.
> - **Startup log says when the background fills are done.** Look for these lines in `kubectl -n observability logs nebulens-0`:
>   `rollups ready: span rollup`, `rollups ready: environment counts`, `rollups ready: Kubernetes resource registry`.
>   Until a line appears, the page it serves uses its older, slower query (never wrong numbers). On NFS this can take a while
>   the first time on a large database; later restarts are instant.
>
> **v10.14** — image tag `pulse_10_14` (includes v10.13 and earlier below).
> - **Services page: 36.5 s -> 0.1 s** (EKS + Postgres, 26 services, 3-hour window). It made two requests per service and each
>   health check scanned raw spans twice; one new call, `/api/services-summary`, returns every service's health score and trend
>   from the rollups (same scoring rules). Older servers and uncovered windows fall back to the per-service calls.
> - **Browser (RUM) is now "Website Monitoring"** ("Website" in the menu). With no website connected it explains the page and gives
>   both ways to connect one, with copy buttons: one line in the HTML, or injection at nginx / ingress with no code change.
> - **Fix:** the newest point on the Home and per-service charts read 0 during the first half of every minute.
>
> **v10.13** — image tag `pulse_10_13` (includes v10.12 and earlier below).
> - **Kubernetes, Environments and Traces pages are fast.** Measured on EKS + Postgres with 3.1M spans: Environments 27.9 s -> 9 ms,
>   Traces list 8.0 s -> 19 ms (errors-only 8.2 s -> 0.43 s), Kubernetes page 5.0 s -> 19 ms.
>   * Kubernetes reads a small registry of distinct resources (`resource_seen`, one row per service/namespace/pod/node/SDK), written at
>     ingest, instead of re-reading 20,000 rows of resource JSON per table on every call.
>   * Environments reads per-minute span and log counts per environment (`env_rollup`) and the registry for namespaces, instead of
>     four full scans of the window.
>   * The Traces list fetches the candidate traces' spans by index and aggregates them in code.
>   * Tables are created automatically. Existing data is filled in by background jobs on first start (the registry covers the last
>     24 h; the environment counts cover all history; about a minute at 3M spans). Until then each page uses its old query.
> - The Kubernetes namespace rows now say which signals are flowing (`spans · logs · metrics flowing`) instead of a capped row count.
>
> **v10.12** — image tag `pulse_10_12`. Complete package (adds the demo apps, load generator and EKS script) plus hardening.
> - **New: `DEMO.md`**: how to run, reset and remove a client demo (laptop, bare metal, AWS EKS). `demo/` (apps and a 22-service
>   simulated shop with deliberate faults, `demo/k8s-sim-platform.yaml`) and `scripts/eks.sh` are now in the package.
> - **Fixes found by a heavy-load test on EKS + Postgres (1.7M+ spans, 12 concurrent senders):**
>   * **Ingest deadlocks on Postgres.** Concurrent batches updated the same rollup rows in different orders and Postgres aborted one;
>     the 400 it returned made senders drop the batch. Rollup rows are now written in primary-key order, a transient database
>     error is retried, and persistent trouble returns a retryable **503** (bad payloads stay 400). SQLite was not affected.
>   * **Service map** no longer joins the newest minutes raw on every request (10 s+ at thousands of spans/s). It now trails live
>     traffic by about 2 minutes (`NEBULENS_EDGE_LAG_SECONDS`); set `NEBULENS_MAP_TAIL=1` for exact-to-the-second on small installs.
>   * **Insights** judges "went silent" from the rollup. Measured at ~1.7M spans, 8 concurrent clients, cache bypassed: map
>     9.9 s -> 0.09 s, insights 1.6 s -> 0.19 s, home 2.0 s -> 0.39 s, services ~0.09 s; rollup and raw spans reconcile exactly.
>   * Not yet improved: the Traces list (~1.5 s at that size).
> - **Security:** `scripts/eks.sh` no longer uses a shared default token; it generates one per cluster (kept in `.eks-token`, removed
>   by teardown) and prints it. The package contains no `enterprise/` (license/keygen) code and no credentials; `.gitignore` now excludes
>   local state files (`.eks-token`, `.eks-rds-state`, logs). VM install steps fixed (copy `integrations.py`; no paid module).
>
> **v10.11** — image tag `pulse_10_11`. **Upgrade from v10.8, v10.9 and v10.10.**
> - **Fixes `/api/services` not using the rollup** in v10.8–v10.10 (a patch hunk landed in the wrong place when the rollups were
>   ported onto v10.7, so the Services page, which also feeds Home totals, kept scanning raw spans). Home chart, Insights, Service
>   Map and the service timeseries were already on the rollups. New tests assert each fast path is the one taken.
>
> **v10.10** — image tag `pulse_10_10` (includes v10.9 and earlier below).
> - **Logs page redesigned for pod logs.** The volume bar is now at the bottom; the log entries get the space. Click a row
>   to expand it in place: full message with the exception line in red and stack frames dimmed, the trace, every attribute,
>   and where it ran (cluster / namespace / pod / container / node). Click a chip to filter to it. ERROR/FATAL rows have a
>   red stripe and tint; the pod name shows on the row. New buttons: *Errors only*, *Expand all*, *Collapse*. Open rows stay
>   open during live tail. *Details* still opens the side drawer.
> - `/api/logs` rows now include a small `k8s` object (the five keys above). The full resource blob is still not returned.
> - Tests: 166.
>
> **v10.9** — image tag `pulse_10_9` (includes v10.8 and earlier below).
> - SSO sessions now survive a restart: without `NEBULENS_SESSION_SECRET`, a generated secret is kept in a 0600 file beside the
>   SQLite database (`<db>.session-secret`). Set the env var explicitly when running more than one replica or on Postgres.
> - The command-palette search reads the `services_seen` / `metric_names_seen` registries and looks back 24h for traces,
>   instead of scanning whole tables.
> - Startup uses FastAPI lifespan (removes the `on_event` deprecation warning). Tests: 165.
>
> **v10.8** — image tag `pulse_10_8` (includes v10.7 and v10.6 below).
> - **Faster dashboards: per-minute rollups.** Services (RED + p50/p95/p99), the Home chart, Insights and the Service Map now
>   read small rollup tables (`span_rollup`, `edge_rollup`, `rollup_state`) instead of scanning every span in the window.
>   500k-span test: services 60m 3.4s -> 0.18s, home 33s -> 0.6s, insights 3.9s -> 0.3s, service map 4.2s -> ms.
>   Tables are created automatically. Existing spans are back-filled in the background (newest first, resumes after a
>   restart); until a window is covered, or for windows under 5 minutes, the old exact queries are used.
>   Percentiles are within ~3% of exact; short-window counts can be up to ~2% high (the first minute is included whole).
> - Map edges are added by the worker once spans are `NEBULENS_EDGE_LAG_SECONDS` (default 120) old so parents are stored.
> - `/api/promql/label/service|env/values` read the small `services_seen` registry; `/api/traces/{id}` is capped at 20,000 spans.
> - Chart: pod memory limit 1Gi -> 2Gi (request 1Gi). Large scans spill to disk when the working set exceeds memory.
> - Tests: 163 (SQLite). Postgres run not done: run with `NEBULENS_TEST_PG_URL` before deploying there.
>
> **v10.7** — image tag `pulse_10_7`, chart `0.3.1` (includes everything in v10.6 below).
> - **Logs page: room for the log lines.** Drag across the chart to zoom into a time window; the entries, patterns, groups,
>   service list, chart and CSV export all cover just that window. *Back* steps out one zoom, *Reset* returns to the page's
>   time range, and the window is kept in the URL. The chart can be hidden or made larger, the filter sidebar can be hidden,
>   and *Focus* hides the page header and banner. These choices are remembered in the browser.
> - **Logs page: problem services first.** The service list shows ERROR/FATAL counts in red next to the total and sorts by
>   errors (switchable to most records). Logs endpoints accept `start_ms` / `end_ms`.
> - Clusters has its own menu icon.
>
> **v10.6** — image tag `pulse_10_6`, chart `0.3.0`.
> - **New: one console for many clusters.** *Clusters* page (admin): add a cluster, get its own ingest token and the install
>   commands. Agent-only install: `helm upgrade --install nebulens-agent ./helm/nebulens -f helm/nebulens/values-agent.yaml`
>   (`server.enabled=false`, `collector.remote.*`). The console stores everything sent with a cluster's token under that
>   cluster, whatever the sender claims. The Kubernetes page has a cluster selector. New table `clusters` is created
>   automatically; no migration step.
> - **New: log and metric volume controls.** `NEBULENS_MIN_LOG_SEVERITY` / `NEBULENS_LOG_UNKNOWN_SEVERITY` (server),
>   `collector.logs.namespaces`, `collector.logs.minSeverity`, `collector.metrics.namespaces`, `collector.hostScrapers`
>   (chart). All default to the previous behaviour.
> - **New: `collector.clusterName`** stamps `k8s.cluster.name` on a cluster's telemetry (falls back to
>   `env.NEBULENS_CLUSTER_NAME`). The console also adds the cluster name to its own cluster's telemetry when it is missing.
> - **Fixes** the Kubernetes → Workloads tab (`parseHash is not defined`).
> - Upgrade: build and push `plasmacomputing/microai:pulse_10_7`, then
>   `helm -n observability upgrade nebulens ./helm/nebulens --reset-then-reuse-values --set image.tag=pulse_10_7`.
>   If the upgrade fails with a webhook certificate error from the OpenTelemetry Operator, restart the operator deployment
>   after the upgrade, or run the upgrade with `--no-hooks`.
>
> **v10.4** = v10.3 + chart fix below (image tag stays `pulse_10_3`; v10.2 and earlier are superseded).
> **v10.3** — image tag `pulse_10_3`.
> - **Fixes** the Integrations / Kubernetes pages not scrolling (long Setup tab was clipped).
> - v10.2 contents below still apply.
>
> - **Fixes** `/api/metrics/names` timing out on large databases: now served from a small hourly registry
>   table (`metric_names_seen`, created automatically; no migration step, no wipe). 3M-row benchmark: 6.7s -> 9 ms.
>   Counts are per whole hour, so a 60-minute window spans the current and previous hour bucket. The table
>   fills as metrics arrive (about a minute with 60s scrapes); nothing is back-filled from the raw table.
> - **Includes** v10.1: Dockerfile copies `integrations.py` (pulse_10 crashed without it).
> - **Kubernetes wizard/integration corrected**: collector HTTP port is 4319 (4318 is NebuLens itself), removed an
>   invented namespace label, chart path is `./helm/nebulens`, per-workload instrumentation uses `scripts/instrument.sh`.
> - Tests: 136 SQLite / 135 Postgres 16 (+1 SQLite-only skipped).
>
> Upgrade: build `plasmacomputing/microai:pulse_10_3`, then
> `helm -n observability upgrade nebulens ./helm/nebulens --reset-then-reuse-values --set image.tag=pulse_10_3`
> then `kubectl -n observability delete pod nebulens-0` only if the rollout does not replace the pod by itself.
> The uid-1000 chown of `/data` is already done; nothing else to repeat.

# NebuLens v10 upgrade — Kubernetes monitoring, Dash0-style

**Deploy target:** `pulse_10` on cluster `cloudstgnewmaster52`, namespace `observability`.
No database wipe. No Helm value changes beyond the image tag.

## What's new in v10

**Kubernetes page gets Dash0's two superpowers:**

1. **Setup wizard with live progress.** The Kubernetes integration's Setup tab now shows four
   numbered steps (Install → Monitor a namespace → Instrument a workload → See traces).
   Each step goes green when NebuLens actually sees the telemetry it expects — no trust-me checkboxes,
   no "did you click yes?". The evidence line tells you what NebuLens saw ("3 nodes reporting",
   "2 instrumented workloads", "14 spans in the last hour").

2. **Namespaces tab.** Every namespace NebuLens has seen in the last hour, with:
   - service count
   - instrumented count + coverage %
   - signals flowing (spans / logs / metrics)
   - language chips (java, python, nodejs, etc.)
   - click → opens the Workloads tab filtered to that namespace.

3. **Workloads tab.** One row per `(service.name, k8s.namespace.name)`:
   - green dot = SDK/agent attached (telemetry.sdk.language present)
   - grey dot = sending data but not instrumented yet (the "what haven't I annotated?" list)
   - deployment name, pod count, node, SDK language + version
   - filter by namespace.

**New endpoints:**
- `GET /api/kubernetes/overview` — nodes, namespaces, workloads, instrumented count, language breakdown
- `GET /api/kubernetes/namespaces` — per-namespace coverage
- `GET /api/kubernetes/workloads?namespace=…` — per-workload status
- `GET /api/kubernetes/wizard` — the four onboarding step checks

**Nav:** A direct **Kubernetes** shortcut now sits under Explore (points at the integration).

## Design notes

Dash0 ships a Go operator with two CRDs (`Dash0OperatorConfiguration`, `Dash0Monitoring`). NebuLens
deliberately doesn't ship one — the existing `opentelemetry-operator` subchart already handles
annotation-driven agent injection, and writing a second operator would duplicate it. What NebuLens
adds instead is the **visibility layer**: you still use standard `instrumentation.opentelemetry.io/inject-*`
annotations, but NebuLens now reports back in real time which namespaces and workloads have been
reached and which haven't. That's the part Dash0 charges for.

## Deploy

On the build machine (LINDISHSTG101):

```bash
cd /home/c2m/pulse/nebulens-v10
docker build -t plasmacomputing/microai:pulse_10 .
docker push plasmacomputing/microai:pulse_10
```

On the master (cloudstgnewmaster52):

```bash
# optional safety: snapshot values in case rollback is needed
helm -n observability get values nebulens > /tmp/nebulens.v9.values.yaml

helm -n observability upgrade nebulens ./nebulens-onprem \
  --reuse-values --set image.tag=pulse_10

kubectl -n observability rollout status sts/nebulens --timeout=120s
```

Verify in the UI: Kubernetes (sidebar) → Setup. Four steps, each green once the matching telemetry
arrives. If your Java agents have already been repointed at NebuLens's collector (from v5's advice),
you should see them show up under Workloads with `language: java`.

## Rollback

```bash
helm -n observability rollback nebulens
```

v10 adds no new tables and no new required env vars. The rollback restores pulse_5 cleanly.

## Tests

- 132 passed on SQLite
- 131 passed, 1 skipped on Postgres 16

## Pending from prior sessions (unchanged)

- Rotate admin token `e57254…0e17`
- Dash0 operator still exports to dead `10.0.4.233:40318` — the v9 setup commands document the repoint
- DB login rejected (~157/hour), License parse errors, SMTP auth, Kafka producer errors — cluster-side

## v10.4 addition — kubelet metrics on clusters where node names don't resolve
The collector scraped kubelets at `<node-name>:10250`; on bare metal where cluster DNS can't resolve node names
this failed every minute (`lookup <node> on 10.96.0.10:53: no such host`) and no node/pod metrics arrived.
It now uses the node IP (`status.hostIP`, new env `K8S_NODE_IP`). Chart-only change (no image rebuild needed);
`helm upgrade` rolls the collector DaemonSet.
