# NebuLens on-premises

NebuLens runs entirely inside your network. By default it stores everything in one SQLite file, so there is no database to install.

| You have | Use | Section |
|---|---|---|
| A Kubernetes cluster (k3s, kubeadm, Rancher, OpenShift) | Helm chart | [Kubernetes](#kubernetes-bare-metal) |
| One Linux VM or server, Docker allowed | Docker Compose | [VM with Docker](#vm-with-docker) |
| One Linux VM, no Docker | systemd | [VM without Docker](#vm-without-docker) |

Every path ends with the same check: [Verify](#verify).

## 1. Get the image onto your machines

Kubernetes and Docker both need the `nebulens` image. Pick the one that matches your site.

**Private registry (most common).** On a machine with Docker:

```bash
docker build -t registry.example.com/nebulens:0.1.0 .
docker push registry.example.com/nebulens:0.1.0
```

**Air-gapped, no registry.** Export once, copy the file, import on each node:

```bash
docker build -t nebulens:0.1.0 .
docker save nebulens:0.1.0 | gzip > nebulens-0.1.0.tar.gz

# on every Kubernetes node (or the Docker VM):
gunzip -c nebulens-0.1.0.tar.gz | sudo k3s ctr images import -            # k3s
gunzip -c nebulens-0.1.0.tar.gz | sudo ctr -n k8s.io images import -      # kubeadm with containerd
gunzip -c nebulens-0.1.0.tar.gz | docker load                              # plain Docker
```

The OpenTelemetry Collector image (`otel/opentelemetry-collector-contrib:0.111.0`) comes from Docker Hub. In an air-gapped site, mirror it the same way and set `collector.image` in the Helm values.

## Kubernetes (bare metal)

Prerequisites: `kubectl` and `helm` pointed at your cluster, and a storage class (`kubectl get storageclass`).

**1. Choose storage and exposure.** The commented options are in `helm/nebulens/values-baremetal.yaml`. Storage defaults to `local-path` (the k3s default); change `persistence.storageClass` if you use Longhorn, OpenEBS, Rook-Ceph or NFS. The UI is exposed as a `NodePort` by default; switch to `LoadBalancer` if you run MetalLB, or enable `ingress`.

**2. Install.**

```bash
helm install nebulens ./helm/nebulens -n observability --create-namespace \
  -f helm/nebulens/values-baremetal.yaml \
  --set image.repository=registry.example.com/nebulens \
  --set-string image.tag=0.1.0 \
  --set auth.token=$(openssl rand -hex 24)
```

- Air-gapped, image imported on every node: use `--set image.repository=nebulens --set image.pullPolicy=Never`.
- Private registry that needs a login:
  ```bash
  kubectl -n observability create secret docker-registry regcred \
    --docker-server=registry.example.com --docker-username=<user> --docker-password=<password>
  # then add to the helm command:
  --set imagePullSecrets[0].name=regcred
  ```
- Always pass the tag with `--set-string`. A tag made only of digits is otherwise read as a number and breaks the image name.
- NebuLens uses SQLite and must stay at one replica. On a multi-node cluster, pin it to one node with `nodeSelector` (see the values file).

**3. Reach the UI.**

```bash
kubectl -n observability get svc nebulens                  # NodePort: open http://<any-node-ip>:<nodePort>
kubectl -n observability port-forward svc/nebulens 4318:4318   # quick look: http://localhost:4318
```

The bundled collector DaemonSet collects pod logs and node and cluster metrics automatically. Point your applications at `http://nebulens-collector.observability.svc.cluster.local:4319` (OTLP/HTTP).

**Tracing without code changes (optional).** NebuLens can switch on distributed tracing for Java, .NET, Python, Node.js and Go services with no code or image changes, using the OpenTelemetry Operator.

```bash
# Install the Operator together with NebuLens (add this to the helm install/upgrade command above):
#   --set opentelemetry-operator.enabled=true --wait --timeout 5m
# Do not enable it if the Operator is already installed on the cluster.

kubectl get instrumentation -n observability             # should list "nebulens"

# Switch tracing on per workload (pods restart; use --remove to switch it off again):
scripts/instrument.sh java   -n <namespace> <deployment>
scripts/instrument.sh dotnet -n <namespace> <deployment> --musl   # --musl only for Alpine images
```

Traces appear under Traces within a minute. The Operator and agent images come from `ghcr.io`; in an air-gapped site, mirror them to your registry and set `manager.image.repository` under `opentelemetry-operator:` and the agent images in `helm/nebulens/templates/instrumentation.yaml`. If an app already ships another APM agent (for example Elastic APM), turn that agent off first.

**4. Upgrade and remove.**

```bash
helm upgrade nebulens ./helm/nebulens -n observability -f helm/nebulens/values-baremetal.yaml \
  --set image.repository=registry.example.com/nebulens --set-string image.tag=0.1.1 \
  --set auth.token=<same token>
helm uninstall nebulens -n observability      # the data volume is kept until you delete the PVC
```

Pass the full set of values on upgrade rather than `--reuse-values`, which keeps stale defaults across chart versions.

## OpenShift (OCP 4.x, OKD, ROSA, ARO)

```bash
oc new-project nebulens
helm install nebulens ./helm/nebulens -n nebulens -f helm/nebulens/values-openshift.yaml \
  --set image.repository=<your-registry>/nebulens --set auth.token=<CHOOSE_A_TOKEN>
oc get route nebulens -n nebulens          # the UI URL
```

OpenShift differs from vanilla Kubernetes in three ways, all handled by that values file:

- **No fixed UID.** `restricted-v2` assigns each pod a UID from the project's range, so the chart's default `runAsUser`/`fsGroup` are cleared. (Clearing needs `null`, not `{}` — Helm merges maps, so an empty map would leave the defaults in place.)
- **Container logs need an SCC.** Reading `/var/log/pods` is a hostPath mount, which `restricted-v2` forbids, so the chart ships its own SCC for the collector's ServiceAccount (`openshift.scc.enabled`). It allows a read-only `/var/log` host path and nothing else: no privileged container, no added capabilities, no host network or PID. Installing an SCC needs cluster-admin once. To avoid it entirely, set `collector.enabled=false` and send OTLP straight to NebuLens — you then lose container-log collection and node metrics.
- **Routes replace Ingress.** `openshift.route.enabled` creates one with edge TLS.

For zero-code tracing, install the **Red Hat build of OpenTelemetry** from OperatorHub (it is the same Operator), leave `opentelemetry-operator.enabled=false`, and use `scripts/instrument.sh`.

## AWS (EKS) and Azure (AKS)

```bash
helm install nebulens ./helm/nebulens -n observability --create-namespace \
  -f helm/nebulens/values-eks.yaml --set auth.token=<CHOOSE_A_TOKEN>     # or values-aks.yaml
```

Edit the values file first: it ships placeholders for the image registry (ECR or ACR), the storage class (`gp3` / `managed-csi-premium`), the ingress (ALB with an ACM certificate on EKS, nginx with cert-manager on AKS) and the hostname. The collector's `resourcedetection` processor adds cloud attributes (region, instance, cluster) automatically on both, with no configuration.

## VM with Docker

Needs Docker with the Compose plugin. Files are in `deploy/vm/`.

```bash
cd deploy/vm
cp .env.example .env          # set NEBULENS_TOKEN (openssl rand -hex 24); set NEBULENS_IMAGE if you use a registry
docker compose up -d
docker compose ps             # both services should be "running" / "healthy"
```

Open `http://<vm-ip>:4318`. Applications send OTLP to `http://<vm-ip>:4318` directly, or through the host collector on `:4317` (gRPC) and `:4319` (HTTP). The collector also reports the VM's CPU, memory, disk, network and load as the service `host-metrics`.

Data lives in the `nebulens-data` Docker volume and survives restarts and upgrades.

Upgrade: load or pull the new image, set `NEBULENS_IMAGE`, then `docker compose up -d`.

## VM without Docker

Needs Python 3.12 and root access.

```bash
sudo useradd --system --home /opt/nebulens --shell /usr/sbin/nologin nebulens
sudo mkdir -p /opt/nebulens /etc/nebulens /var/lib/nebulens
sudo cp -r server.py integrations.py static requirements.txt /opt/nebulens/
sudo python3.12 -m venv /opt/nebulens/venv
sudo /opt/nebulens/venv/bin/pip install -r /opt/nebulens/requirements.txt
sudo cp deploy/vm/nebulens.env.example /etc/nebulens/nebulens.env      # edit NEBULENS_TOKEN
sudo chmod 600 /etc/nebulens/nebulens.env
sudo chown -R nebulens:nebulens /opt/nebulens /var/lib/nebulens
sudo cp deploy/vm/nebulens.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now nebulens
sudo systemctl status nebulens
```

`NEBULENS_HOST=0.0.0.0` in the env file is required for other machines to connect; the default is localhost only. For an air-gapped site, download the wheels with `pip download -r requirements.txt` on a connected machine and install with `pip install --no-index --find-links`.

## Optional: use your own Postgres

Set `NEBULENS_DATABASE_URL=postgresql://user:password@host:5432/dbname?sslmode=require`. NebuLens creates its own schema (`nebulens`, change with `NEBULENS_PG_SCHEMA`) and tables on first start, so you only need an empty database and a user that can create schemas. SQLite is simpler and faster for a single instance, so use Postgres only if you need it. Use a Postgres with real disks and memory; a small shared or free-tier instance makes the dashboards slow.

In Helm: create a secret holding the URL, then `--set postgres.enabled=true --set postgres.existingSecret=<secret> --set persistence.enabled=false`.

## Environments (namespaces, clusters, deployment environments)

The **Environments** page lists every connected slice of telemetry with its services, span volume, error rate and log errors. Nothing to configure: an environment registers itself the first time data arrives. Each span, log and metric is tagged, resolved in this order:

1. `deployment.environment` — a service that labels itself `staging` or `production` wins.
2. the cluster — `k8s.cluster.name` from telemetry, or `env.NEBULENS_CLUSTER_NAME` from the chart.

Namespaces are listed **inside** each environment, not treated as environments themselves. On a single cluster with no cluster name set, everything rolls up into one environment called `default`; set `NEBULENS_CLUSTER_NAME` to name it after your cluster (e.g. `staging-baremetal`).

The bundled collector already adds the namespace. To label a service explicitly, set `OTEL_RESOURCE_ATTRIBUTES=deployment.environment=production` on it.

**Connecting more clusters to one console.** One NebuLens can show any number of clusters. Each cluster runs a small agent (the collector, no server) that sends to this console with its **own token**:

1. In the console open **Clusters → Add cluster**, give it a name, and copy the token. It is shown once; NebuLens keeps only a hash.
2. The dialog shows the commands for the new cluster. They create a Secret with the token and install the chart in agent-only mode:

```bash
kubectl create namespace observability
kubectl -n observability create secret generic nebulens-cluster-token --from-literal=token=<TOKEN>
helm upgrade --install nebulens-agent ./helm/nebulens -n observability -f helm/nebulens/values-agent.yaml \
  --set collector.clusterName=<cluster-id> --set collector.remote.endpoint=http://<console-host>:<port>
```

3. The cluster shows as **live** on the Clusters page within a minute and as its own environment everywhere else. Every page can be filtered by it (`?env=<cluster-id>`), and the Kubernetes page has a cluster selector.

What the console guarantees:

- **The token decides the cluster.** Everything sent with a cluster's token is stored under that cluster, whatever `k8s.cluster.name` or `deployment.environment` the sender claims. A cluster cannot write into another cluster's environment.
- **Each token is its own credential.** Rotate it, disable it or remove it without touching the other clusters. A rotated, disabled or removed token stops working within about 10 seconds.
- **Only administrators** can list, create, rotate or remove cluster tokens (`/api/clusters`). Creating, rotating and removing are written to the audit trail; the token itself is never logged.
- **Make the console refuse anonymous senders.** Set `auth.ingestToken` (`NEBULENS_INGEST_TOKEN`). Without any global token the console still accepts data without one, and the Clusters page warns about it. The console's own collector then needs `auth.token`.
- **The cluster must be able to reach the console.** Use the console's NodePort, LoadBalancer or ingress address. For HTTPS with a self-signed certificate set `collector.remote.insecureSkipVerify=true`, or better, give the console a certificate the cluster trusts.
- An agent-only install can still run the OpenTelemetry Operator and auto-injection (`opentelemetry-operator.enabled=true`); the injected agents send to the cluster's own collector, which forwards to the console.

Hosted (multi-tenant) mode manages data separation with organizations and projects instead; the Clusters page is for self-hosted consoles.

**Per-environment access control (Pro).** Limit an SSO group to certain environments:

```bash
curl -X PUT http://<nebulens>/api/environments/access -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'content-type: application/json' -d '{"grp":"dev-team","slugs":["cluster-a/application"]}'
```

Rules apply to SSO sessions only, matched against the group claim in `NEBULENS_OIDC_ROLE_CLAIM`. Behaviour worth knowing:

- With no rules configured, everyone sees every environment.
- Once any rule exists, a user whose groups match no rule sees nothing (deny by default).
- **Admins are always exempt**, so a bad rule can never lock every operator out.
- A restricted user asking for another environment gets `403 env_forbidden`, and pages that cannot apply the filter are refused rather than silently showing everything.
- API tokens (`NEBULENS_TOKEN`, `NEBULENS_READ_TOKEN`) are not restricted. Use SSO for per-environment access.

## Security defaults

- **The API is same-origin only.** `/api/*` sends no CORS headers, so a page on another site (or another host under the same parent domain) cannot read NebuLens data with a signed-in user's session. Only `/v1/*` (OTLP ingest) accepts cross-origin browser requests, for browser RUM, and it still requires the ingest token.
- **Response headers:** `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, a restrictive `Permissions-Policy`, and `Strict-Transport-Security` over HTTPS (also behind a proxy when `NEBULENS_TRUST_PROXY=1` and it sends `X-Forwarded-Proto: https`). Set `NEBULENS_ALLOW_FRAMING=1` only if you embed NebuLens in another page. A Content-Security-Policy is not set yet: the UI builds styles inline, so a policy that actually protects needs testing in a real browser first.
- **The container runs as uid 1000**, not root, and has a health check. Dependencies are pinned in `requirements.lock`.

## Integrations catalog (v9)

A new `/integrations` page lists every data source NebuLens knows about: Kubernetes, Java/Spring Boot, Node.js, Python, PostgreSQL, Redis, Kafka, nginx, Browser RUM. Each one bundles its own dashboards, alert templates, saved views, and a Setup tab with the exact commands to connect it.

**Install:** one click creates the dashboards and views for that integration. Re-installing is a no-op; uninstall removes exactly what the integration added (hand-made rules you built from a template are kept).

**Detection:** the detail page shows a green "● Receiving data" banner when the required attributes or metric names (listed on the Detection tab) are seen in the last hour. If dashboards look empty, this tab tells you which attribute is missing instead of leaving you to guess.

**Kubernetes integration** wraps the OpenTelemetry Operator approach — the Setup tab has the `helm upgrade --set opentelemetry-operator.enabled=true` command and the pod annotations for each language.

API: `/api/integrations`, `/api/integrations/{id}`, `/api/integrations/{id}/install`, `/api/integrations/{id}/uninstall`. Dashboards and views created this way sit alongside user-made ones; they are tracked by a per-integration manifest so uninstall never removes something a user added.

## Saved views, Errors page, PromQL, Browser RUM (v8)

**Saved views.** Every page that supports views has a `Views` button in its toolbar. Click a view to apply its filters. Pin views to the top with the star. Create your own with "Save current filters as view". NebuLens ships eight built-in views (all-errors, warn-and-up, pod-logs, kubernetes-events, noisy-services, recurring, latest-errors, frequent-errors). Deleting a built-in is remembered across restarts, so hiding it is a one-time choice. API: `/api/views`.

**Errors page.** A new page at `/errors` groups error-level logs by message pattern, showing count, affected services, first and last seen, and a sparkline per group. One row per *kind* of error, not per occurrence, so a flood of similar failures reads as one line. Click a row for the drawer with the example message and jump-to-logs or jump-to-trace buttons. Sort by most frequent, latest, first seen, or "new in the last 24 hours". API: `/api/errors`.

**PromQL (subset).** NebuLens answers a usable subset of PromQL at `/api/promql/query`, `/api/promql/query_range`, `/api/promql/labels` and `/api/promql/label/{name}/values`. Add NebuLens as a Prometheus data source in Grafana (URL: `http://nebulens.<namespace>.svc.cluster.local:4318/api/promql`) and dashboards work. Supported: selectors with matchers (`=`, `!=`, `=~`, `!~`), range vectors (`[5m]`), `sum|avg|min|max|count` with `by` and `without`, `rate()`, `deriv()`, binary and comparison operators. Not yet supported: `histogram_quantile`, `topk`/`bottomk`, subqueries, `absent()`, string functions, the `@` modifier.

**Browser RUM.** Drop `<script src="<nebulens>/sdk/nebulens-rum.js" data-endpoint="<nebulens>" data-service="<name>"></script>` into a page and NebuLens starts receiving page views, Core Web Vitals (LCP, CLS, INP, FID), uncaught errors, and slow `fetch`/XHR calls. The SDK is 8 KB, has no dependencies, and uses `navigator.sendBeacon` where available so data arrives even if the page unloads. A new page at `/browser` shows the summary. Send custom events with `nebulens.event("name", {attrs})`.

## Audit trail (Pro)

Every change, sign-in and sign-out is recorded with the actor, role, how they authenticated, the request, the response status, **the source IP** and the request id. Settings that regulated buyers ask about:

| Variable | Default | What it does |
|---|---|---|
| `NEBULENS_AUDIT_RETENTION_DAYS` | `90` | How long records are kept. **`0` keeps them forever** — use this where a retention period is mandated. |
| `NEBULENS_AUDIT_READS` | off | Also record every read (`GET /api/*`), so "who viewed production logs" is answerable. Adds one row per API call. |
| `NEBULENS_AUDIT_SYSLOG` | off | Mirror each record to a SIEM as RFC 5424 syslog, e.g. `siem.internal:514/udp` or `siem:601/tcp`. Best effort: if the SIEM is down, requests still succeed. |
| `NEBULENS_TRUST_PROXY` | off | Set to `1` behind an ingress so the real client IP is recorded instead of the proxy's. Only enable behind a proxy you control — any client can forge the header. |

**Tamper evidence.** Each record stores a SHA-256 hash of its own fields plus the previous record's hash. Editing or deleting any record breaks every hash after it:

```bash
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" https://<nebulens>/api/audit/verify
# {"ok": true, "checked": 48213, "message": "48213 audit records verified; the chain is intact."}
```

A failure reports the exact record id and whether it was altered or removed. Records written by a NebuLens older than this release have no hash and are reported as `unchained` rather than as tampered with.

**Export for an auditor or SIEM import:**

```bash
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" "https://<nebulens>/api/audit/export?since_minutes=43200" -o audit.csv
```

Filter the API with `actor`, `method`, `path_prefix`, `status` and `since_minutes`.

Two limits worth stating plainly: the chain proves records were not changed *in the database*, but an administrator with write access could rebuild the whole chain, so ship records to a SIEM (`NEBULENS_AUDIT_SYSLOG`) for independent retention. Audit rows live in the same database as telemetry, so they are included in its backup, not a separate one.

## Automatic severity (no configuration)

Many services write the level into the log text but leave the OTLP severity field empty, so every record would show as UNSET. NebuLens infers the level at ingest, the same approach Dash0's "Log AI" takes:

- Reads the level from the body: Java/Logback, .NET (`fail:`/`warn:`), Python, JSON `level`, logfmt `level=`, klog (`E1110 …`), and bare `ERROR`/`WARN`/`INFO` prefixes.
- Understands the Kafka client format (`%4|…|REQTMOUT|…`), bracketed mixed-case levels (`[Error]`), and Spring/Elastic notices.
- Recognises common framework noise: Spring Boot banner and JVM/agent startup lines as INFO; `Error:` / `Missing required environment variable` / `+-> Exception:` / `failed to …` as ERROR.
- Treats an uncaught Java stack trace or a `... warning:` line as ERROR / WARN.
- For access logs, infers from the HTTP status code: 2xx/3xx INFO, 4xx WARN, 5xx ERROR.
- Never overrides a severity the service already set, and tags inferred records with `nebulens.severity.inferred` (shown as a small `~` next to the level).

The Logs histogram shows the full breakdown (Error, Warn, Info, Debug, Unset), and each pattern shows its own severity mix, so "which services have errors" is visible at a glance rather than hidden in a single "Other" bucket.

When a log is a multi-line stack trace, the table row shows only its first line with a `+ stack` badge, so the list stays scannable. Clicking any log opens a slide-in detail panel with the full message, its attributes, resource metadata, and a link to the correlated trace.

## Logs: Table, Patterns and Groups

The Logs page has three views over the same filters:

- **Table** — the raw entries, with live tail.
- **Patterns** — near-identical messages collapsed into one row. Timestamps, ids, IP addresses, paths and numbers are replaced with placeholders, so a flood of 40 errors that differ only by a job id becomes a single `ERROR Export failed for job <n> after <qty>` row with a count. Click a pattern to drill into its entries.
- **Groups** — counts by any attribute, resource attribute or built-in field (service, severity, environment), with error counts per value. Click a value to filter by it.

`CSV` downloads the current view. The same views are available on the API: `/api/logs/patterns`, `/api/logs/groupby?field=…`, `/api/logs/fields` and `/api/logs/export`, each accepting the usual `minutes`, `service`, `min_severity`, `q_` and `env` filters.

## Redacting sensitive data before it is stored

Logs and span attributes routinely carry card numbers, national IDs and credentials. NebuLens strips them **at ingest**, so the raw value never reaches the database or any backup of it. Each match becomes `<card:redacted>`, keeping the line readable.

| Variable | Default | What it does |
|---|---|---|
| `NEBULENS_REDACT` | `card,ssn,iban,jwt,apikey` | Rules to apply. Also available: `email`, `phone`. `off` disables redaction. |
| `NEBULENS_REDACT_CUSTOM` | — | Your own patterns, `name=<regex>` separated by `;`, e.g. `acct=\bACC-\d{8}\b` |

Card numbers must pass the Luhn check **and** start with a real card-issuer prefix (Visa, Mastercard, Amex, Discover, JCB, Diners, UnionPay, Maestro), so order ids and 19-digit nanosecond timestamps are left alone while real card numbers are removed. Blank log lines are discarded at ingest (the count is in `/api/redaction`). `GET /api/redaction` reports the active rules and how much each has removed, which is useful evidence for a reviewer. Redaction applies to new data only; anything stored before a rule was enabled is unchanged.

## TLS and mutual TLS

NebuLens can terminate TLS itself, for sites with no ingress or service mesh:

```bash
NEBULENS_TLS_CERT=/certs/tls.crt
NEBULENS_TLS_KEY=/certs/tls.key
NEBULENS_TLS_KEY_PASSWORD=...        # only if the key is encrypted
NEBULENS_TLS_CLIENT_CA=/certs/ca.crt # also require a client certificate (mTLS)
```

With `NEBULENS_TLS_CLIENT_CA` set, only clients holding a certificate signed by that CA can connect, so an approved collector can send telemetry and nothing else on the network can — a common requirement for ingest endpoints in regulated networks. Mount the certificates with `extraVolumes` / `extraVolumeMounts` or a cert-manager Secret.

## High availability

The default SQLite storage is single-writer, so `replicaCount` must stay at 1. For HA, move to Postgres:

```bash
kubectl -n observability create secret generic nebulens-db \
  --from-literal=database-url='postgresql://user:pass@host:5432/nebulens?sslmode=require'

helm upgrade --install nebulens ./helm/nebulens -n observability \
  --set postgres.enabled=true --set postgres.existingSecret=nebulens-db \
  --set persistence.enabled=false --set replicaCount=3 \
  --set highAvailability.podDisruptionBudget.enabled=true \
  --set highAvailability.antiAffinity=hard --set highAvailability.topologySpreadZones=true
```

The chart **refuses** more than one replica on SQLite rather than silently splitting your telemetry across replicas. HA then depends on your Postgres being highly available too, so use a managed or replicated instance and back it up; the audit trail lives in the same database.

## Air-gapped installation

```bash
scripts/airgap-bundle.sh --with-operator            # on a machine with internet access
# carry nebulens-airgap-<date>.tar.gz across, then inside the network:
tar -xzf nebulens-airgap-<date>.tar.gz && cd airgap && ./load.sh registry.internal:5000
```

The bundle holds every container image, the chart, the docs and a `sha256sum` manifest that `load.sh` verifies before loading. NebuLens makes no outbound connection you have not configured — no licence check-in, no update ping, no usage telemetry — so it runs indefinitely with no internet access. A licence key is validated offline. The only outbound traffic is what you configure: alert webhooks, synthetic checks, your own identity provider, and the AI assistant if you enable it.

## Supply-chain evidence

`.github/workflows/supply-chain.yml` produces, on every tagged release: an SBOM in CycloneDX and SPDX, a Trivy vulnerability report as SARIF, SLSA build provenance, and a keyless Sigstore signature over the image. A reviewer can verify the signature with:

```bash
cosign verify ghcr.io/<org>/<repo>:<tag> \
  --certificate-identity-regexp '^https://github.com/<org>/<repo>/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Sizing, retention and storage

**Metrics are the largest table by far.** Every scrape of every node writes hundreds of rows, so a small cluster can produce tens of millions of metric rows a week. Three settings control it:

| Setting | Default | Effect |
|---|---|---|
| `collector.metricsInterval` | `60s` | How often node, kubelet and Prometheus metrics are scraped. `30s` doubles the volume. |
| `env.NEBULENS_METRICS_RETENTION_HOURS` | same as retention | Keep metrics for less time than logs and traces. On a busy cluster `24` keeps the database small. |
| `env.NEBULENS_RETENTION_HOURS` | `72` | Retention for logs and traces. |
| `collector.hostScrapers` | all six | Host metrics to collect. `[cpu, memory, filesystem, load]` drops disk and network (the disk scraper alone writes four metrics per device on every scrape). |
| `collector.hostUtilization` | `true` | Collects host `system.cpu.utilization` and `system.memory.utilization` (the collector leaves them off by default). |
| `collector.kubeletMetrics` | `[k8s.pod.cpu.usage, k8s.container.cpu_limit_utilization, k8s.container.memory_limit_utilization]` | Kubelet metrics to collect on top of the collector's defaults. Names must exist in the collector version, otherwise it does not start. Limit and request utilisation only appear for containers that have limits or requests. |
| `collector.metrics.namespaces` | every namespace | Keep pod and container metrics only from the listed namespaces. Node and host metrics are always kept. Namespaces left out no longer appear on the Kubernetes page. |

**Logs: store only what you need.** Verbose Java and .NET applications can write hundreds of thousands of DEBUG and TRACE lines an hour, which fills the database and makes pages slow. Three settings keep the stored volume down:

| Setting | Default | Effect |
|---|---|---|
| `env.NEBULENS_MIN_LOG_SEVERITY` | `0` (store everything) | Lowest severity stored: `9` INFO, `13` WARN, `17` errors only. Applied after NebuLens has read the level from the log text, so it also covers container logs that arrive with no severity. |
| `env.NEBULENS_LOG_UNKNOWN_SEVERITY` | `drop` | With a minimum set: `drop` discards lines whose level cannot be determined, `keep` stores them. |
| `collector.logs.namespaces` | every namespace | Read container logs only from the listed namespaces, so system and operator namespaces never reach NebuLens. |
| `collector.logs.minSeverity` | `0` | Drops records that already carry a severity below this at the collector, before they are sent. |

Dropped records are not stored and are not reported as rejected, so exporters do not retry. `/api/redaction` shows `logs_below_min_severity_dropped_since_start`, which is how you can confirm the setting is working. Data stored before you set a minimum stays until retention removes it.

Old data is removed in small batches so a large backlog never stalls ingest, and the table has a timestamp index (built in the background on first start of an older database, so startup is never blocked).

**Storage matters.** The default database is SQLite. On network storage such as NFS every read and write crosses the network, so large scans are many times slower than on a local disk. For anything beyond evaluation use local or SSD-backed storage for the volume, or move to Postgres (see High availability). If pages become slow as data grows, shorten retention first, then check the storage class.

## Editions: Community and Pro

Every install starts with a 14-day Pro trial. After that it runs as Community unless a Pro licence is configured (`NEBULENS_LICENSE_KEY`).

| | Community | Pro |
|---|---|---|
| Traces, logs, metrics, service map, dashboards, alerts, SLOs, synthetics | Yes | Yes |
| Services sending traces | 10 | Unlimited |
| Dashboards | 10 | Unlimited |
| Alert rules | 10 | Unlimited |
| SLOs | 3 | Unlimited |
| Synthetic checks | 3 | Unlimited |
| Data retention | Up to 7 days | Configurable |
| Admin, read-only and ingest API tokens | Yes | Yes |
| SSO (OIDC) with viewer / editor / admin roles | No | Yes |
| Per-environment access control | No | Yes |
| Audit log: source IP, read auditing, tamper-evident hash chain, CSV export, syslog/SIEM feed | No | Yes |
| AI assistant (optional, with your own model: Claude, OpenAI, or a self-hosted model via any OpenAI-compatible endpoint) | No | Yes |

Over a limit, the API answers `403` with `"error": "tier_limit"` and says which limit was hit. Telemetry from services already counted keeps flowing. The audit log is still recorded on Community, so the history is there when you upgrade.

## Production checklist

- **Token.** Set a strong `NEBULENS_TOKEN`. It protects `/v1/*` and `/api/*`.
- **HTTPS.** Put a reverse proxy in front of port 4318. With Caddy, a two-line `Caddyfile` does it: `nebulens.example.com { reverse_proxy 127.0.0.1:4318 }`. Apps then send OTLP to `https://nebulens.example.com`.
- **Firewall.** Open only 4318 (or 443 behind the proxy), plus 4317 and 4319 if you use the host collector from other machines.
- **Sizing.** Allow roughly 0.3 to 1 GB of disk per million spans at default settings, and raise retention only as far as disk allows. Give the process memory roughly equal to the database size for fast dashboards.
- **Backups.** SQLite: `sqlite3 /var/lib/nebulens/nebulens.db ".backup /backup/nebulens.db"` works while running. Docker: `docker exec` the same command against `/data/nebulens.db`. Kubernetes: snapshot the PVC.

## Verify

From any machine that can reach NebuLens:

```bash
python scripts/smoke.py http://<host>:4318 --token <your token>
```

It sends a test service over OTLP and checks the UI and each main API read it back. It prints PASS or FAIL per check and ends with a line like `35/35 checks passed`; it exits non-zero if anything failed. The first FAIL line tells you where to look.

Quick manual check: `curl http://<host>:4318/healthz` returns `{"status":"ok"}`, and `curl http://<host>:4318/readyz` confirms the database is reachable.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Pod stuck in `ImagePullBackOff` or `InvalidImageName` | image not on the node or registry login missing (see section 1); a numeric tag passed without `--set-string` |
| Pod `Pending` | no storage class, or none marked default; set `persistence.storageClass` |
| UI unreachable from other machines (VM) | `NEBULENS_HOST` still `127.0.0.1`, or the firewall blocks 4318 |
| `401` from the API or collector | token missing or different from `NEBULENS_TOKEN` |
| New services not appearing | Community tier allows 10 services; the 14-day Pro trial or a license lifts that |
