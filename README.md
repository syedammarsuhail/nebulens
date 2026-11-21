# NebuLens

**A clear lens on your microservices.** Self-hosted, OpenTelemetry-native observability: traces, logs and metrics
from every service, shown as live signals so you see traffic waves, latency spikes and error bursts the moment they happen.

**Website:** https://nebulens.ai/ ([developers](https://nebulens.ai/developers.html) · [enterprise](https://nebulens.ai/enterprise.html) · [product tour](https://nebulens.ai/tour.html) · [AI](https://nebulens.ai/ai.html))

One OTLP endpoint (`:4318`), one web UI, your own infrastructure. No per-host or per-GB pricing, and your telemetry never leaves
your network.

## What you get

- **Services and health:** request rate, errors and latency (RED) for every service, with a health score per service.
- **Service map:** how traffic flows between services, and where errors and latency spread.
- **Traces, logs and metrics:** search, filter and drill down, from a slow request to its logs and its host.
- **Kubernetes:** clusters, nodes, workloads and pods, with CPU and memory, from a collector the Helm chart installs.
- **Dashboards, alerts, SLOs and synthetic checks,** with a template library.
- **Built-in metrics from spans and logs,** with nothing to configure: request rate, errors, error rate, latency (avg, p50,
  p95, p99), log volume and error logs per service, ready for dashboards and alerts.
- **Live Insights:** anomalies and regressions, found automatically.
- **Ask NebuLens:** an AI assistant that explains what changed and why. Works with Claude, OpenAI or any OpenAI-compatible model,
  and falls back to rule-based analysis with no key.
- **Browser monitoring:** page views, JavaScript errors and slow requests from real users, with a 5 KB script.
- **SSO and roles** (OIDC; viewer, editor, admin) and an audit log.
- **SQLite or Postgres**, on Kubernetes (Helm), a VM with Docker Compose, or a VM with systemd.

## Get started

**New here? Read [GETTING-STARTED.md](GETTING-STARTED.md)**: build, install, first login and getting data in, in about 20 minutes.

| You want to | Read |
|---|---|
| Install on Kubernetes, a VM or a laptop | [GETTING-STARTED.md](GETTING-STARTED.md), every option in [ON-PREM.md](ON-PREM.md) |
| Give or set up a client demo | [DEMO.md](DEMO.md) |
| Upgrade to a new version | [UPGRADE.md](UPGRADE.md) |

Try it on your laptop:

```bash
pip install -r requirements.lock
python server.py                     # UI and OTLP endpoint on http://localhost:4318
```

On Kubernetes:

```bash
docker build -t <your-registry>/nebulens:11.8 . && docker push <your-registry>/nebulens:11.8
helm install nebulens ./helm/nebulens -n observability --create-namespace \
  --set image.repository=<your-registry>/nebulens --set-string image.tag=11.8 \
  --set auth.token=$(openssl rand -hex 24)
```

Then point any OpenTelemetry SDK or collector at `http://<host>:4318`, and check it end to end:

```bash
python scripts/smoke.py http://<host>:4318 --token <your token>
```

## Community and Pro

Community is free to self-host on one cluster of up to 5 nodes, with the full product: traces, logs, metrics, the service map,
eBPF tracing, built-in metrics and Live Insights. Pro adds unlimited nodes and retention, SSO with roles and unlimited AI answers;
Enterprise adds per-environment access, SIEM audit export and air-gapped support. See the [pricing page](https://nebulens.ai/pricing.html).

## Security

Every release passes `scripts/release-check.sh` (tests, dependency audit, static analysis, secret scan, Helm and UI checks), and
CI adds Snyk and SonarCloud (`.github/workflows/security.yml`). Images are published only when all of them pass. Please report a
vulnerability privately through GitHub security advisories, not in a public issue.

## Development

```bash
pip install -r requirements-dev.txt
pytest                                                         # SQLite
NEBULENS_TEST_PG_URL=postgresql://user:pw@host/db pytest         # the same suite on Postgres
bash scripts/release-check.sh                                  # the full release gate
```

Layout: `server.py` (API, ingest and storage), `static/` (the web UI, no build step), `helm/nebulens/` (Kubernetes chart),
`deploy/vm/` (Compose, collector and systemd files), `scripts/` (smoke test, release gate, demo helpers), `tests/`.

## License

Business Source License 1.1, see [LICENSE](LICENSE).
