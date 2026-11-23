# Getting started with NebuLens

NebuLens is self-hosted observability built on OpenTelemetry. It gives you one endpoint that receives traces, logs and metrics, and a web
UI to explore them: services with request rate, errors and latency, a service map, traces, logs, metrics, Kubernetes, dashboards,
alerts, SLOs, synthetic checks, website monitoring and an optional AI assistant. Everything runs on your own infrastructure and your
data stays there.

This guide takes you from nothing to a working install with data. It takes about 20 minutes.

| If you want to... | Read |
|---|---|
| Install NebuLens for the first time | this guide |
| See every install option and setting in detail | [ON-PREM.md](ON-PREM.md) |
| Run a demo for a client, or reset one | [DEMO.md](DEMO.md) |
| Upgrade an existing install | [UPGRADE.md](UPGRADE.md) |
| Run NebuLens as a hosted, multi-tenant service for other people's orgs (self-serve signup, Stripe billing) | [HOSTED.md](HOSTED.md) |

---

## 1. What you need

- **Somewhere to run it**, one of:
  - a Kubernetes cluster (k3s, kubeadm, Rancher, OpenShift, EKS, AKS) with `kubectl` and `helm`, **or**
  - one Linux VM or server with Docker and the Compose plugin, **or**
  - a laptop with Docker, just to try it.
- **Docker on one machine** to build the image (the package ships the source, not a prebuilt image).
- **Storage on a local disk or SSD.** Do not put the database on NFS or other network storage: pages become many times slower. If you
  must use network storage, use Postgres instead (see [Storage and performance](#9-storage-and-performance)).
- **Memory roughly equal to the database size** for fast dashboards. The Helm chart requests 1 GiB and allows 2 GiB by default.

## 2. Download and unpack

Download the newest `nebulens-onprem-v*.tar.gz` from the repository, then:

```bash
tar -xzf nebulens-onprem-v11_8.tar.gz
cd nebulens-v11_8
```

Run every command below from this folder.

## 3. Get the image

**Use the published image (recommended).** Every release is built in CI, signed with Sigstore and shipped with an SBOM
and a vulnerability report on its [GitHub release](https://github.com/syedammarsuhail/nebulens/releases):

```bash
docker pull ghcr.io/syedammarsuhail/nebulens:11.8
# optional: check the signature before you deploy
cosign verify ghcr.io/syedammarsuhail/nebulens:11.8   --certificate-identity-regexp '^https://github.com/syedammarsuhail/nebulens/'   --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

The Helm chart uses this image by default. To use your own registry instead, mirror it
(`docker tag ghcr.io/syedammarsuhail/nebulens:11.8 registry.example.com/nebulens:11.8 && docker push registry.example.com/nebulens:11.8`)
or build it from this folder (`docker build -t registry.example.com/nebulens:11.8 .`), and pass `--set image.repository=registry.example.com/nebulens`.

No registry (air-gapped)? Save the image to a file and import it on each node: see [ON-PREM.md](ON-PREM.md) section 1.

## 4. Choose a token

NebuLens protects its API and its ingest endpoint with a token. Make a long random one and keep it somewhere safe:

```bash
openssl rand -hex 24
```

Use a different token for every install. Never use a shared or example value.

## 5. Install

Pick **one**.

### A. Kubernetes (recommended)

**Bare metal, every app traced with no restarts (eBPF is on in `values-baremetal.yaml`):**

```bash
TOKEN=<the token from step 4>
helm upgrade --install nebulens ./helm/nebulens -n observability --create-namespace \
  -f helm/nebulens/values-baremetal.yaml \
  --set image.repository=ghcr.io/syedammarsuhail/nebulens \
  --set-string image.tag=11.8 --set image.pullPolicy=Always \
  --set persistence.storageClass=local-path \
  --set auth.token=$TOKEN \
  --set collector.traceSamplingPercent=100 \
  --set-string collector.clusterName=baremetal \
  --wait --timeout 10m
```

Your applications are not restarted or changed: one eBPF agent per node traces them from the kernel. Needs Linux 5.8+ on
every node (`uname -r`; `ls /sys/kernel/btf/vmlinux` must exist). Turn it off with `--set ebpf.enabled=false`.

**Bare metal, everything on (eBPF + OpenTelemetry Operator + automatic agent injection):** same command plus
`values-full.yaml` and your application namespaces. Each workload in those namespaces restarts once to get the language
agent (here 10 every 5 minutes); everything else stays on eBPF with no restart.

```bash
helm upgrade --install nebulens ./helm/nebulens -n observability --create-namespace \
  -f helm/nebulens/values-baremetal.yaml -f helm/nebulens/values-full.yaml \
  --set image.repository=ghcr.io/syedammarsuhail/nebulens \
  --set-string image.tag=11.8 --set image.pullPolicy=Always \
  --set persistence.storageClass=local-path \
  --set auth.token=$TOKEN \
  --set-string collector.clusterName=baremetal \
  --set "autoInstrumentation.autoInject.namespaces={shop,payments}" \
  --set autoInstrumentation.autoInject.maxChangesPerRun=10 \
  --set-string autoInstrumentation.autoInject.schedule="*/5 * * * *" \
  --wait --timeout 10m
```

Leave out the Operator (`--set opentelemetry-operator.enabled=false`) if the cluster already runs one. Apps whose language
cannot be read from the Deployment are skipped (still traced by eBPF); label them `nebulens.io/language=java` (or `dotnet`,
`python`, `nodejs`, `go`).

Check it:

```bash
kubectl -n observability get pods -o wide        # nebulens-0, collectors, one nebulens-ebpf per node
kubectl -n observability logs -l app.kubernetes.io/component=ebpf --tail=-1 | grep -o "type=[a-z]* service=[^ ]*" | sort -u
```

- Use `values-eks.yaml`, `values-aks.yaml` or `values-openshift.yaml` instead of `values-baremetal.yaml` on those platforms.
- Edit the values file first if your cluster has no `local-path` storage class (`kubectl get storageclass`).
- Always pass the image tag with `--set-string`.

Open the UI:

```bash
kubectl -n observability port-forward svc/nebulens 4318:4318      # then http://localhost:4318
# or, with the default NodePort: kubectl -n observability get svc nebulens  ->  http://<node-ip>:<nodePort>
```

### B. One VM with Docker

```bash
cd deploy/vm
cp .env.example .env        # set NEBULENS_TOKEN to your token, and NEBULENS_IMAGE=ghcr.io/syedammarsuhail/nebulens:11.8
docker compose up -d
docker compose ps           # both services should be running / healthy
```

Open `http://<vm-ip>:4318`.

### C. Just try it on a laptop

```bash
cd demo
docker compose up --build
```

Open <http://localhost:4318>. This starts NebuLens together with two small demo services and a load generator, so data appears straight
away. Stop it and delete the data with `docker compose down -v`.

## 6. First login

1. Open the UI. If it asks for a token, go to **Settings → Access**, paste your token and click **Save token**.
2. A new install starts a **14-day Pro trial**: every feature is unlocked. You will see a banner with the days left.
3. The **Get started** page shows the commands to connect data sources from this point on.

## 7. Get data in

**Kubernetes:** the chart already installs an OpenTelemetry Collector on every node. Within a minute or two you will see pod logs, node
and cluster metrics, and the **Kubernetes** page fills, without changing any application.

**Your applications' traces:**

- **Every app, no code changes, no restarts (Kubernetes, eBPF):** one switch traces every application in every namespace
  from the Linux kernel: HTTP, gRPC, SQL, Redis, Kafka and more, for Java, .NET, Go, Python, Node.js, Ruby and Rust.
  Calls between services are joined into one trace, so the service map fills by itself.

  ```bash
  helm upgrade nebulens ./helm/nebulens -n observability --reset-then-reuse-values --set ebpf.enabled=true
  ```

  Running pods are picked up within seconds. Needs Linux 5.8+ (RHEL 8 works) and runs one privileged agent per node.
  NebuLens' own namespace is never traced; skip others with `--set "ebpf.excludeNamespaces={ns1,ns2}"`.
- **Deeper detail for chosen services (language agent):** for spans inside your code (functions, custom attributes),
  enable the OpenTelemetry Operator once, then switch it on per workload:

  ```bash
  helm upgrade nebulens ./helm/nebulens -n observability --reset-then-reuse-values --set opentelemetry-operator.enabled=true --wait --timeout 5m
  scripts/instrument.sh java -n my-namespace my-deployment       # also: dotnet, python, nodejs, go
  ```

  The pod restarts once with the language agent attached, and eBPF then skips it. Do not enable the Operator if it is
  already installed in the cluster.
- **Everything at once (full test):** `helm/nebulens/values-full.yaml` turns on the collector (keeping every trace), eBPF,
  the OpenTelemetry Operator and automatic agent injection. On bare metal:

  ```bash
  helm upgrade --install nebulens ./helm/nebulens -n observability --create-namespace \
    -f ./helm/nebulens/values-baremetal.yaml -f ./helm/nebulens/values-full.yaml \
    --set image.repository=ghcr.io/syedammarsuhail/nebulens --set-string image.tag=11.8 \
    --set persistence.storageClass=local-path \
    --set auth.token=$(openssl rand -hex 24) \
    --set "autoInstrumentation.autoInject.namespaces={shop,payments}" \
    --wait --timeout 10m
  ```

  eBPF traces every app straight away. Every 10 minutes the injector also adds the language agent to up to 3 workloads in
  the listed namespaces (each restarts once) for in-code detail. Use the same token on later upgrades, or keep it in a
  secret (`auth.existingSecret`).
- **Apps that already use OpenTelemetry:** point their OTLP exporter at the collector,
  `http://nebulens-collector.observability.svc.cluster.local:4319` (inside the cluster), or straight at NebuLens on port 4318 with the
  header `Authorization: Bearer <token>`.
- **Docker VM:** apps send to `http://<vm-ip>:4319` (the host collector) or `http://<vm-ip>:4318`.

**Your website (optional):** the **Website** page explains itself and shows the one line to add to your site, with copy buttons.

**CPU and memory dashboard (optional):** the chart already collects host and pod CPU / memory. To get a ready-made
*Cluster - CPU & Memory* dashboard, run `python3 scripts/create-dashboards.py --url http://<host>:4318 --token <token>`. It also
tells you which panels have data yet (allow 2-3 minutes after install).

**Demo traffic (optional):** see [DEMO.md](DEMO.md) for a simulated 22-service shop with realistic faults.

## 8. Check that it works

```bash
python scripts/smoke.py http://<host>:4318 --token <your token>
```

It sends a test service and checks the UI and the main APIs. It ends with a line like `35/35 checks passed`.

On the first start NebuLens also fills its fast summary tables from any data already stored. When that is done the log says so:

```bash
kubectl -n observability logs nebulens-0 | grep "rollups ready"
# rollups ready: span rollup
# rollups ready: environment counts
# rollups ready: Kubernetes resource registry
```

Until those lines appear, some pages use slower queries (never wrong numbers). On a new install they appear within seconds.

## A quick tour

| Page | What it is for |
|---|---|
| **Home** | Requests, error rate, latency and the problems NebuLens found, on one screen |
| **Services** | Every service with request rate, errors, p50/p95/p99 latency, a health score and a trend |
| **Service map** | Which service calls which, built from traces; red means errors |
| **Traces** | Search requests and open one to see its path through every service |
| **Logs** | Search and filter; expand a row to see the full message, stack trace, pod and trace; error rows stand out |
| **Kubernetes** | Namespaces, workloads and pods, and which are instrumented |
| **Environments** | Each cluster or deployment environment with its namespaces |
| **Website** | What real visitors experience in their browsers (needs the website script) |
| **Dashboards, Alerts, SLOs, Synthetics** | Your own charts, alert rules with templates, service-level objectives, uptime checks |
| **Ask NebuLens** | Ask questions in plain English (button at the top) |

## AI assistant

**Ask NebuLens** always works with built-in rules, free, and no data leaves your network. Connect an AI model for AI answers. NebuLens works
with any provider; you bring your own model and key.

| Provider | Helm settings |
|---|---|
| Claude | `--set ai.enabled=true --set ai.provider=anthropic --set ai.existingSecret=nebulens-ai` |
| OpenAI, Azure OpenAI, Groq, Mistral, OpenRouter... | `--set ai.enabled=true --set ai.provider=openai --set ai.baseUrl=<https://.../v1> --set ai.model=<model> --set ai.existingSecret=nebulens-ai` |
| Self-hosted (Ollama, vLLM, LM Studio): nothing leaves your network | `--set ai.enabled=true --set ai.provider=openai --set ai.baseUrl=http://<ollama>:11434/v1 --set ai.model=llama3.1` |

Create the key secret first (not needed for a local model without a key):

```bash
kubectl -n observability create secret generic nebulens-ai --from-literal=api-key=<YOUR_API_KEY>
```

Then restart NebuLens (`kubectl -n observability rollout restart statefulset/nebulens`). The model must support tool (function) calling.
**Settings → AI assistant** shows whether it is on and, if not, why. With a cloud model, snippets of query results (service names, error
messages, log lines) are sent to that provider.

## Plans: Community, Pro and Enterprise

| | Community (free) | Pro | Enterprise |
|---|---|---|---|
| Traces, logs, metrics, service map, Kubernetes, eBPF tracing, built-in metrics, Live Insights, website monitoring | Yes | Yes | Yes |
| PII redaction, mTLS, signed images | Yes | Yes | Yes |
| Footprint | 1 cluster, up to 5 nodes | Licensed nodes, unlimited clusters | Licensed nodes |
| Dashboards / alert rules / SLOs / synthetic checks | 10 / 15 / 3 / 3 | Unlimited | Unlimited |
| AI answers with your own model | 30 per day (`NEBULENS_COMMUNITY_AI_DAILY`); rule-based answers unlimited | Unlimited | Unlimited |
| Retention | Up to 7 days | Your choice | Your choice |
| SSO (OIDC) with viewer / editor / admin roles, audit log | No | Yes | Yes |
| Per-environment access, audit log streamed to a SIEM (syslog) | No | No | Yes |
| Support | Community | Email | Priority, response targets in your contract |

Prices: [pricing page](https://nebulens.dev/pricing.html). A new install gets a 14-day Pro trial; afterwards NebuLens keeps running as Community and
nothing is deleted. Going over your node count shows a banner; data is never refused.

**Licence key.** Pro and Enterprise keys start with `NBL1.` and carry the plan, the node count and the expiry date. They are
checked offline (nothing is sent anywhere) and cannot be created or edited without NebuLens' private signing key:

```bash
kubectl -n observability create secret generic nebulens-license --from-literal=license-key=NBL1....
helm -n observability upgrade nebulens ./helm/nebulens --reset-then-reuse-values --set license.existingSecret=nebulens-license
```

Docker VM: set `NEBULENS_LICENSE_KEY=NBL1....` in `.env`. **Settings → Plan** shows the plan, the licensed nodes and how many
nodes are sending data.

## Upgrading

1. Unpack the new package and build the new image (steps 2 and 3).
2. Read the newest section of [UPGRADE.md](UPGRADE.md).
3. Kubernetes: `helm -n observability upgrade nebulens ./helm/nebulens --reset-then-reuse-values --set-string image.tag=<new tag>`.
   Docker VM: set the new `NEBULENS_IMAGE` in `.env`, then `docker compose up -d`.
4. Reload the browser once with **Ctrl+Shift+R** so it picks up the new UI.

Your data is kept. New tables are created automatically.

## 9. Storage and performance

- **Use local disk or SSD for the database.** NFS makes every page slow; this is the most common cause of a slow install.
- **Or use Postgres:** set `NEBULENS_DATABASE_URL` (Helm: `--set postgres.enabled=true --set postgres.existingSecret=<secret>`), with a
  properly sized Postgres; a small free-tier database is too slow. See [ON-PREM.md](ON-PREM.md).
- **Give it memory:** roughly the size of the database.
- **Retention:** `NEBULENS_RETENTION_HOURS` (default 72). Shorter retention means a smaller, faster database.
- NebuLens is a single instance: keep one replica.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Pages are slow | Storage on NFS? Not enough memory? Wait for the `rollups ready` lines after an upgrade |
| `401` from the UI, API or collector | The token in Settings → Access or in the collector does not match `NEBULENS_TOKEN` |
| Pod `ImagePullBackOff` | The image is not in a registry the nodes can reach, or the tag was not passed with `--set-string` |
| Pod `Pending` | No storage class: set `persistence.storageClass` |
| UI unreachable from other machines (VM) | Firewall blocks port 4318 |
| No data | Run `scripts/smoke.py`; check `kubectl -n observability logs nebulens-0` and the collector pods |
| New services do not appear | Community allows 10 services; the trial or a license lifts that |
| Old-looking page after an upgrade | Reload with Ctrl+Shift+R |
| AI answers not working | Settings → AI assistant shows the reason; the pod must be able to reach the model's address |

## Security checklist

- Scan every build before you deploy it: `bash scripts/release-check.sh` (tests, dependency CVEs, code scan, secrets, chart).
  In a GitHub repository, `.github/workflows/security.yml` also runs Snyk and SonarCloud once you add `SNYK_TOKEN` and `SONAR_TOKEN`.
- A long random `NEBULENS_TOKEN`, different for every install. The chart refuses the placeholder tokens in its values files.
- HTTPS in front of port 4318 (any reverse proxy or ingress).
- Open only the ports you use: 4318 (UI and API), and 4317 / 4319 for the collector if other machines send to it.
- For a website, use an ingest-only token (`NEBULENS_INGEST_TOKEN`), never the admin token: web pages are public.
- Keep API keys in Kubernetes secrets or environment variables, never in values files or in Git.
