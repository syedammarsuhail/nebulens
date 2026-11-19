"""Integrations catalog: known data sources (Kubernetes, Java, Postgres, ...) and what they ship —
dashboards, alert templates, and saved views. One Python file so a new integration is a dict literal.

Each integration has:
  id                  slug used in URLs and the installed-by table
  name, icon, category, description
  setup               how to connect: "operator" | "sdk" | "agent" | "scrape" | "logs"
  setup_blocks        ordered list of instructions shown on the Setup tab (markdown-lite)
  detect              {attributes: [...], metric_names: [...]} - any match in the last 60 min means "active"
  dashboards          [{slug, name, description, panels: [...]}]
  alerts              [alert_template_id]  - references the keys in ALERT_TEMPLATES
  views               [{slug, name, description, kind, params}]
  docs_url            optional external docs
Nothing here touches the database directly; the server owns all writes and the installed-by bookkeeping.
"""


def _panel(title, type_, source, **extra):
    return {"id": "seed-" + str(abs(hash((title, type_))))[:8], "type": type_, "source": source,
            "title": title, "w": extra.pop("w", 6), "h": extra.pop("h", 3), **extra}


def _m(title, metric, type_="area", agg="avg", group_by="", w=6, h=3):
    """Metric panel."""
    return _panel(title, type_, "metric", metric=metric, agg=agg, group_by=group_by, w=w, h=h)


def _svc(title, signal, type_="area", service="", w=6, h=3):
    """Service traffic panel (built from traces: rate / error rate / avg latency)."""
    return _panel(title, type_, "service", service=service, signal=signal, w=w, h=h)


# --------------------------------------------------------------- Kubernetes ---
K8S_SETUP = [
    {"kind": "intro", "text": "The NebuLens collector (DaemonSet + cluster-collector) already scrapes node, kubelet and pod metrics on install. For *automatic application* instrumentation, add the OpenTelemetry Operator and annotate workloads."},
    {"kind": "command", "label": "Enable the OpenTelemetry Operator in your NebuLens chart (one-time)",
     "code": "helm -n observability upgrade nebulens ./helm/nebulens \\\n  --reset-then-reuse-values --set opentelemetry-operator.enabled=true --wait"},
    {"kind": "command", "label": "Then annotate a workload to inject the OpenTelemetry Java agent (no image change)",
     "code": "kubectl -n <namespace> patch deployment <name> -p \\\n  '{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"instrumentation.opentelemetry.io/inject-java\":\"observability/nebulens\"}}}}}'"},
    {"kind": "note", "text": "Replace `inject-java` with `inject-python`, `inject-nodejs`, `inject-dotnet` or `inject-go` for other languages. The operator restarts the pod once with the agent injected."},
]

INTEGRATIONS = [
    {
        "id": "kubernetes", "name": "Kubernetes", "icon": "map", "category": "Infrastructure",
        "description": "Cluster overview, pod and node health, workload instrumentation via the OpenTelemetry Operator.",
        "setup": "operator", "setup_blocks": K8S_SETUP,
        "detect": {"kubernetes": True, "attributes": ["k8s.pod.name", "k8s.namespace.name", "k8s.node.name"],
                   "metric_names": ["k8s.pod.cpu.usage", "k8s.node.cpu.usage", "k8s.container.restarts"]},
        "dashboards": [
            {"slug": "kubernetes-cluster", "name": "Kubernetes · Cluster",
             "description": "Node and cluster-wide health at a glance.",
             "panels": [
                 _m("Node CPU utilisation", "k8s.node.cpu.utilization", type_="area", agg="avg", group_by="k8s.node.name"),
                 _m("Node memory utilisation", "k8s.node.memory.utilization", type_="area", agg="avg", group_by="k8s.node.name"),
                 _m("Pod count", "k8s.pod.phase", type_="stat", agg="count"),
                 _m("Container restarts (last range)", "k8s.container.restarts", type_="bar", agg="max", group_by="k8s.pod.name"),
             ]},
            {"slug": "kubernetes-workloads", "name": "Kubernetes · Workloads",
             "description": "Per-pod CPU and memory, container limit pressure, and restart rate.",
             "panels": [
                 _m("CPU usage by pod", "k8s.pod.cpu.usage", agg="avg", group_by="k8s.pod.name"),
                 _m("Memory usage by pod", "k8s.pod.memory.usage", agg="avg", group_by="k8s.pod.name"),
                 _m("Container CPU limit utilisation", "k8s.container.cpu_limit_utilization", type_="line", agg="max", group_by="k8s.container.name"),
                 _m("Container memory limit utilisation", "k8s.container.memory_limit_utilization", type_="line", agg="max", group_by="k8s.container.name"),
             ]},
        ],
        "alerts": ["k8s-cpu-limit", "k8s-mem-limit", "pod-restarts"],
        "views": [
            {"slug": "k8s-events", "name": "Kubernetes events", "description": "Events emitted by the cluster itself.",
             "kind": "logs", "params": {"q": "event.name:", "min_severity": 9}},
            {"slug": "pod-logs", "name": "Pod logs", "description": "Everything collected from pods.",
             "kind": "logs", "params": {"groupby": "k8s.pod.name"}},
        ],
    },
    # --------------------------------------------------------------- Java ---
    {
        "id": "java", "name": "Java / Spring Boot", "icon": "setup", "category": "Languages",
        "description": "JVM metrics, Spring Boot traces and logs via the OpenTelemetry Java agent. No code changes needed.",
        "setup": "agent", "setup_blocks": [
            {"kind": "intro", "text": "Two options: let the OpenTelemetry Operator inject the agent (see the Kubernetes integration), or run the agent yourself."},
            {"kind": "command", "label": "Download the agent",
             "code": "curl -LO https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases/latest/download/opentelemetry-javaagent.jar"},
            {"kind": "command", "label": "Then start your app with it",
             "code": "OTEL_SERVICE_NAME=my-app \\\nOTEL_EXPORTER_OTLP_ENDPOINT=http://nebulens-collector.observability.svc.cluster.local:4319 \\\nOTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \\\nOTEL_LOGS_EXPORTER=otlp \\\njava -javaagent:opentelemetry-javaagent.jar -jar my-app.jar"},
        ],
        "detect": {"languages": ["java"], "attributes": ["telemetry.sdk.language"],
                   "metric_names": ["jvm.memory.used", "jvm.gc.duration", "jvm.thread.count"]},
        "dashboards": [
            {"slug": "jvm", "name": "JVM · Memory, GC and threads",
             "description": "Heap, non-heap, garbage collection pauses and thread count per service.",
             "panels": [
                 _m("Heap used", "jvm.memory.used", agg="avg", group_by="service.name"),
                 _m("Non-heap used", "jvm.memory.used", agg="avg", group_by="jvm.memory.pool.name"),
                 _m("GC pause time", "jvm.gc.duration", agg="sum", group_by="service.name"),
                 _m("Threads", "jvm.thread.count", type_="line", agg="avg", group_by="service.name"),
             ]},
        ],
        "alerts": ["high-error-rate", "p95-latency-500", "error-log-burst"],
        "views": [
            {"slug": "java-stack-traces", "name": "Java stack traces", "description": "Logs where a Java exception was detected.",
             "kind": "logs", "params": {"q": "exception.type:", "min_severity": 17}},
        ],
    },
    # --------------------------------------------------------------- Node.js ---
    {
        "id": "nodejs", "name": "Node.js", "icon": "setup", "category": "Languages",
        "description": "Event-loop lag, V8 heap, HTTP and database spans via the OpenTelemetry Node SDK.",
        "setup": "sdk", "setup_blocks": [
            {"kind": "command", "label": "Install",
             "code": "npm install @opentelemetry/api @opentelemetry/auto-instrumentations-node"},
            {"kind": "command", "label": "Start your app with it",
             "code": "OTEL_SERVICE_NAME=my-app \\\nOTEL_EXPORTER_OTLP_ENDPOINT=http://nebulens-collector.observability.svc.cluster.local:4319 \\\nOTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \\\nnode --require @opentelemetry/auto-instrumentations-node/register app.js"},
        ],
        "detect": {"languages": ["nodejs"], "attributes": ["telemetry.sdk.language"],
                   "metric_names": ["nodejs.eventloop.lag", "v8js.heap.space.used_size"]},
        "dashboards": [
            {"slug": "nodejs", "name": "Node.js · Runtime",
             "description": "Event-loop lag, V8 heap, and process memory per service.",
             "panels": [
                 _m("Event-loop lag", "nodejs.eventloop.lag", agg="avg", group_by="service.name"),
                 _m("V8 heap used", "v8js.heap.space.used_size", agg="avg", group_by="v8js.heap.space.name"),
                 _m("Process memory (RSS)", "process.memory.rss", agg="avg", group_by="service.name"),
             ]},
        ],
        "alerts": ["high-error-rate", "p95-latency-500", "timeout-errors"],
        "views": [],
    },
    # --------------------------------------------------------------- Python ---
    {
        "id": "python", "name": "Python / Django / FastAPI", "icon": "setup", "category": "Languages",
        "description": "ASGI and WSGI HTTP spans, database spans, logs via the OpenTelemetry Python SDK.",
        "setup": "sdk", "setup_blocks": [
            {"kind": "command", "label": "Install",
             "code": "pip install opentelemetry-distro opentelemetry-exporter-otlp-proto-http\nopentelemetry-bootstrap --action=install"},
            {"kind": "command", "label": "Start your app with it",
             "code": "OTEL_SERVICE_NAME=my-app \\\nOTEL_EXPORTER_OTLP_ENDPOINT=http://nebulens-collector.observability.svc.cluster.local:4319 \\\nOTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \\\nOTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=true \\\nopentelemetry-instrument python app.py"},
        ],
        "detect": {"languages": ["python"], "attributes": ["telemetry.sdk.language"],
                   "metric_names": ["process.runtime.cpython.memory", "process.runtime.cpython.gc_count"]},
        "dashboards": [
            {"slug": "python", "name": "Python · Runtime",
             "description": "CPython memory and garbage-collection activity.",
             "panels": [
                 _m("Memory by type", "process.runtime.cpython.memory", agg="avg", group_by="type"),
                 _m("Threads", "process.runtime.cpython.thread_count", type_="line", agg="avg", group_by="service.name"),
             ]},
        ],
        "alerts": ["high-error-rate", "p95-latency-500"],
        "views": [],
    },
    # --------------------------------------------------------------- PostgreSQL ---
    {
        "id": "postgres", "name": "PostgreSQL", "icon": "metrics", "category": "Databases",
        "description": "Connections, transactions, locks and replication lag via the OpenTelemetry Postgres receiver.",
        "setup": "scrape", "setup_blocks": [
            {"kind": "intro", "text": "The NebuLens cluster-collector already has an OTel Collector. Add a postgresql receiver pointing at your database."},
            {"kind": "command", "label": "Add to the collector config",
             "code": "receivers:\n  postgresql:\n    endpoint: db.default.svc.cluster.local:5432\n    username: ${env:PG_USER}\n    password: ${env:PG_PASSWORD}\n    databases: [postgres]\n    collection_interval: 60s\nservice:\n  pipelines:\n    metrics:\n      receivers: [otlp, postgresql]"},
            {"kind": "note", "text": "Grant the user: `GRANT pg_monitor TO <user>;` (PostgreSQL 10+)."},
        ],
        "detect": {"attributes": [], "metric_names": ["postgresql.connections.active", "postgresql.rows", "postgresql.commits"]},
        "dashboards": [
            {"slug": "postgresql", "name": "PostgreSQL",
             "description": "Connections, transactions, locks, and I/O.",
             "panels": [
                 _m("Active connections", "postgresql.connections.active", type_="line", agg="max", group_by="postgresql.database.name"),
                 _m("Commits per second", "postgresql.commits", agg="sum", group_by="postgresql.database.name"),
                 _m("Rollbacks per second", "postgresql.rollbacks", agg="sum", group_by="postgresql.database.name"),
                 _m("Block I/O", "postgresql.blocks_read", agg="sum", group_by="postgresql.database.name"),
             ]},
        ],
        "alerts": ["db-errors"],
        "views": [
            {"slug": "postgres-errors", "name": "Postgres log errors", "description": "Database error lines.",
             "kind": "logs", "params": {"q": "postgres", "min_severity": 17}},
        ],
    },
    # --------------------------------------------------------------- Redis ---
    {
        "id": "redis", "name": "Redis", "icon": "metrics", "category": "Databases",
        "description": "Connections, commands per second, memory and keyspace via the OpenTelemetry Redis receiver.",
        "setup": "scrape", "setup_blocks": [
            {"kind": "command", "label": "Add to the collector config",
             "code": "receivers:\n  redis:\n    endpoint: redis.default.svc.cluster.local:6379\n    collection_interval: 60s\nservice:\n  pipelines:\n    metrics:\n      receivers: [otlp, redis]"},
        ],
        "detect": {"attributes": [], "metric_names": ["redis.connections.received", "redis.memory.used", "redis.commands.processed"]},
        "dashboards": [
            {"slug": "redis", "name": "Redis",
             "description": "Commands/s, memory, connections and keyspace.",
             "panels": [
                 _m("Commands per second", "redis.commands.processed", agg="sum"),
                 _m("Memory used", "redis.memory.used", type_="line", agg="max"),
                 _m("Connected clients", "redis.clients.connected", type_="line", agg="max"),
                 _m("Keyspace hits vs misses", "redis.keyspace.hits", agg="sum"),
             ]},
        ],
        "alerts": [],
        "views": [],
    },
    # --------------------------------------------------------------- Kafka ---
    {
        "id": "kafka", "name": "Apache Kafka", "icon": "metrics", "category": "Infrastructure",
        "description": "Broker throughput, consumer lag, and client errors from logs.",
        "setup": "scrape", "setup_blocks": [
            {"kind": "intro", "text": "The OpenTelemetry Kafka Metrics receiver uses JMX to read broker internals."},
            {"kind": "command", "label": "Add to the collector config",
             "code": "receivers:\n  kafkametrics:\n    brokers: [kafka.default.svc.cluster.local:9092]\n    protocol_version: 2.0.0\n    collection_interval: 30s\nservice:\n  pipelines:\n    metrics:\n      receivers: [otlp, kafkametrics]"},
        ],
        "detect": {"attributes": [], "metric_names": ["kafka.consumer.lag", "kafka.brokers", "kafka.messages.in"]},
        "dashboards": [
            {"slug": "kafka", "name": "Apache Kafka",
             "description": "Messages in/out, consumer lag per group, under-replicated partitions.",
             "panels": [
                 _m("Messages in", "kafka.messages.in", agg="sum", group_by="topic"),
                 _m("Consumer lag", "kafka.consumer.lag", type_="line", agg="max", group_by="group"),
                 _m("Under-replicated partitions", "kafka.partition.under_replicated", type_="stat", agg="sum"),
             ]},
        ],
        "alerts": ["timeout-errors"],
        "views": [
            {"slug": "kafka-client-errors", "name": "Kafka client errors", "description": "librdkafka client failures.",
             "kind": "logs", "params": {"q": "rdkafka", "min_severity": 13}},
        ],
    },
    # --------------------------------------------------------------- nginx ---
    {
        "id": "nginx", "name": "nginx", "icon": "metrics", "category": "Infrastructure",
        "description": "Request rate, status code distribution, and connections from nginx access logs and stub_status.",
        "setup": "scrape", "setup_blocks": [
            {"kind": "command", "label": "Enable stub_status in nginx",
             "code": "location /nginx_status {\n    stub_status on;\n    allow 10.0.0.0/8;\n    deny all;\n}"},
            {"kind": "command", "label": "Add the receiver",
             "code": "receivers:\n  nginx:\n    endpoint: http://nginx.default.svc.cluster.local/nginx_status\n    collection_interval: 30s"},
        ],
        "detect": {"attributes": [], "metric_names": ["nginx.requests", "nginx.connections_accepted"]},
        "dashboards": [
            {"slug": "nginx", "name": "nginx",
             "description": "Requests per second, active and waiting connections.",
             "panels": [
                 _m("Requests", "nginx.requests", agg="sum"),
                 _m("Active connections", "nginx.connections_current", type_="line", agg="max", group_by="state"),
                 _m("Accepted vs handled", "nginx.connections_accepted", agg="sum"),
             ]},
        ],
        "alerts": ["high-error-rate", "traffic-drop"],
        "views": [],
    },
    # --------------------------------------------------------------- Browser RUM ---
    {
        "id": "browser-rum", "name": "Browser (RUM)", "icon": "globe", "category": "Frontends",
        "description": "Core Web Vitals, uncaught errors and slow network calls from the browser. Uses the NebuLens RUM SDK (8 KB, no dependencies).",
        "setup": "sdk", "setup_blocks": [
            {"kind": "command", "label": "Add the script to your site",
             "code": '<script src="{BASE_URL}/sdk/nebulens-rum.js"\n        data-endpoint="{BASE_URL}"\n        data-service="my-web-app"\n        data-env="production"></script>'},
            {"kind": "note", "text": "Set `data-token` if the page is hosted on a different domain than NebuLens. Set `data-sample=\"0.2\"` to sample 20% of visitors."},
        ],
        "detect": {"languages": ["webjs"], "attributes": ["telemetry.sdk.name", "event.name"], "metric_names": []},
        "dashboards": [
            {"slug": "browser-rum", "name": "Browser · Overview",
             "description": "Visit the dedicated Browser page for full Web Vitals; this dashboard summarises traffic.",
             "panels": []},
        ],
        "alerts": ["error-log-burst"],
        "views": [],
    },
]


def by_id(id_):
    for it in INTEGRATIONS:
        if it["id"] == id_:
            return it
    return None
