/* Oscilyx pages, part 1: Home, Services, Service map, Traces, Logs */
(function () {
  const P = window.P, { el, sv, icon, api, qs, compact, fmt, ms, pct, rate, clock, stamp, ago, sevName, pill, errPill, healthPill, copyText, chart, sparkline, dataTable, empty, panel, kpi, head, codeBlock, field, toast, setParams, nav, COLORS } = P;
  const M = () => P.minutes(), enc = encodeURIComponent;
  const sevIcon = s => s === "critical" || s === "warning" ? "warn" : "info";
  const errColor = p => p >= 5 ? "var(--err)" : p >= 1 ? "var(--warn)" : "var(--ok)";

  const insightRow = i => el("a", { class: "insight " + i.severity, href: i.link }, el("div", { class: "ic" }, icon(sevIcon(i.severity), 15)), el("div", { style: "min-width:0" }, el("div", { class: "t" }, i.title), el("div", { class: "d" }, i.detail)), i.service ? el("span", { class: "svc" }, pill(i.service, "")) : null,
    el("button", { class: "btn sm ghost icon explain", title: P.aiStatus?.enabled ? `Explain with ${P.aiStatus.name || "AI"}` : "Explain (rule-based)", "aria-label": "Explain this insight", on: { click: e => { e.preventDefault(); e.stopPropagation(); P.explain("this insight", `${i.title}. ${i.detail}${i.service ? ` (service: ${i.service})` : ""}`); } } }, icon("sparkle", 14)));
  const insightsPanel = (list, extra = {}) => panel("Live insights", list.length ? el("div", {}, ...list.map(insightRow)) : empty("All clear", "No anomalies detected in this window."), { sub: list.length ? `${list.length} active` : "", bodyCls: "pb", cls: extra.cls || "", style: "" });

  // ============================================================ HOME
  P.route("/home", {
    title: "Home",
    async render(page) {
      page.classList.add("scroll");
      const m = M();
      const [h, ins] = await Promise.all([api(`/home?minutes=${m}&buckets=60`), api("/insights?minutes=" + m)]);
      const t = h.totals, T = h.timeseries;
      if (!t.services && !t.logs) {
        page.append(head("Welcome to Oscilyx", "Nothing has been received yet."), panel("Get started", el("div", { style: "display:flex;flex-direction:column;gap:12px;align-items:flex-start" }, el("div", { class: "muted" }, "Point any OpenTelemetry SDK or Collector at this server and data shows up within seconds."), el("a", { class: "btn primary", href: "#/setup" }, "Open setup guide"))));
        return;
      }
      const crit = ins.filter(i => i.severity === "critical").length;
      page.append(
        head("Overview", `${t.services} services · ${compact(t.requests)} requests · ${compact(t.logs)} logs in the selected range`, el("button", { class: "btn", on: { click: () => P.openAsk("Give me an overview") } }, icon("sparkle", 15), "Ask Oscilyx")),
        el("div", { class: "kpis" },
          kpi("Requests", compact(t.requests), `${rate(t.rate)} req/s`, { spark: sparkline(T.map(x => x.rate), { w: 70, h: 24 }), href: "#/traces" }),
          kpi("Error rate", pct(t.err_pct), `${compact(t.errors)} failed`, { cls: t.err_pct >= 5 ? "err" : t.err_pct >= 1 ? "warn" : "ok", spark: sparkline(T.map(x => x.err_rate), { w: 70, h: 24, color: "var(--err)" }), href: "#/traces?errors=1" }),
          kpi("Services", t.services, "reporting traces", { href: "#/services" }),
          kpi("Avg p95", ms(t.p95_avg), "across services", { href: "#/services" }),
          kpi("Error logs", compact(t.error_logs), `of ${compact(t.logs)} logs`, { cls: t.error_logs ? "warn" : "", href: "#/logs?sev=17" }),
          kpi("Insights", ins.length, crit ? `${crit} critical` : ins.length ? "needs a look" : "all clear", { cls: crit ? "err" : ins.length ? "warn" : "ok" })),
        el("div", { class: "row", style: "flex:none;height:340px" },
          panel("Traffic & errors", chart({ t: T.map(x => x.t), type: "area", h: 228, f: compact, series: [{ name: "requests/s", values: T.map(x => x.rate), color: "var(--c1)" }, { name: "errors/s", values: T.map(x => x.err_rate), color: "var(--err)" }] }), { sub: "per second", cls: "fill", style: "flex:2" }),
          el("section", { class: "panel fill", style: "flex:1;min-width:300px" }, el("div", { class: "ph" }, "Live insights", el("small", {}, ins.length ? `${ins.length} active` : ""), el("div", { class: "r" }, el("a", { class: "btn sm ghost", href: "#/alerts" }, "Alerts"))), el("div", { class: "pb", style: "padding:0", "data-keep": "ins" }, ins.length ? ins.map(insightRow) : empty("All clear", "No anomalies detected in this window.")))),
        el("div", { class: "grid", style: "grid-template-columns:repeat(auto-fit,minmax(300px,1fr));flex:none;height:270px" },
          panel("Top services by errors", rankTable(h.top_errors, "err_pct"), { bodyCls: "pb", cls: "fill" }),
          panel("Slowest services (p95)", rankTable(h.top_latency, "p95"), { cls: "fill" }),
          panel("Alerts & SLOs", el("div", {}, ...h.events.slice(0, 4).map(e => el("div", { class: "insight", style: "cursor:default" }, el("div", { class: "ic", style: e.state === "firing" ? "background:var(--err-bg);color:var(--err)" : "background:var(--ok-bg);color:var(--ok)" }, icon(e.state === "firing" ? "alerts" : "check", 15)), el("div", {}, el("div", { class: "t" }, e.name || "rule"), el("div", { class: "d" }, `${e.msg} · ${ago(e.ts * 1000)}`)))), ...h.slos.slice(0, 3).map(s => el("a", { class: "insight", href: "#/slos" }, el("div", { class: "ic" }, icon("slo", 15)), el("div", { style: "flex:1;min-width:0" }, el("div", { class: "t" }, s.name), el("div", { class: "d" }, s.attainment_pct == null ? "no data" : `${s.attainment_pct}% attainment (objective ${s.objective_pct}%)`)))), !h.events.length && !h.slos.length ? empty("No alert events or SLOs yet", "Create one from a template.", el("a", { class: "btn sm", href: "#/alerts?tab=templates" }, "Browse templates")) : null), { cls: "fill", bodyCls: "pb" })));
    },
  });
  function rankTable(rows, key) {
    if (!rows.length) return empty("No traffic yet");
    const max = Math.max(1e-9, ...rows.map(r => r[key]));
    return el("div", { class: "tbl" }, el("table", {}, el("tbody", {}, ...rows.map(r => el("tr", { class: "click", on: { click: () => nav("/services/" + enc(r.service)) } }, el("td", {}, el("b", {}, r.service)), el("td", { style: "width:38%" }, el("span", { class: "bar-in", style: `width:${Math.max(3, 100 * r[key] / max)}%;background:${key === "err_pct" ? errColor(r.err_pct) : "var(--c1)"}` })), el("td", { class: "num" }, key === "err_pct" ? errPill(r.err_pct) : ms(r.p95)))))));
  }

  // ============================================================ SERVICES
  async function enrich(rows) {
    let all = null;
    try { all = await api(`/services-summary?minutes=${M()}&buckets=30`); } catch (e) { all = null; }
    if (all && Object.keys(all).length) {
      rows.forEach(r => { const x = all[r.service]; r.health = x ? x.health : { service: r.service, score: null, reason: "no traffic in window" }; r.ts = x ? x.ts : []; });
      return rows;
    }
    await Promise.all(rows.map(async r => {
      const [h, ts] = await Promise.all([api(`/services/${enc(r.service)}/health?minutes=${M()}`).catch(() => null), api(`/services/${enc(r.service)}/timeseries?minutes=${M()}&buckets=30`).catch(() => [])]);
      r.health = h; r.ts = ts;
    }));
    return rows;
  }
  P.route("/services", {
    title: "Services",
    async render(page, { q }) {
      const showAll = q.get("all") === "1", term = q.get("s") || "", hf = q.get("health") || "";
      const rows = await enrich(await P.services(showAll));
      const tbl = dataTable({
        rows: [], sortKey: "rate", sortDir: -1,
        cols: [
          { key: "service", label: "Service", sort: r => r.service.toLowerCase(), render: r => el("a", { href: "#/services/" + enc(r.service), style: "font-weight:600" }, r.service) },
          { key: "health", label: "Health", sort: r => r.health?.score ?? -1, render: r => healthPill(r.health) },
          { key: "n", label: "Requests", num: true, render: r => fmt(r.n) },
          { key: "rate", label: "Rate /s", num: true, render: r => rate(r.rate) },
          { key: "err_pct", label: "Errors", num: true, render: r => r.n ? errPill(r.err_pct) : "—" },
          { key: "p50", label: "p50", num: true, render: r => ms(r.p50) }, { key: "p95", label: "p95", num: true, render: r => ms(r.p95) }, { key: "p99", label: "p99", num: true, render: r => ms(r.p99) },
          { key: "ts", label: "Trend", sort: false, render: r => sparkline((r.ts || []).map(x => x.rate), { w: 110, h: 26, color: errColor(r.err_pct) }) },
        ],
        onRow: r => nav("/services/" + enc(r.service)),
        empty: (term || hf) ? empty("No services match", "Adjust the filters, or send traces with a service.name resource attribute.") : empty("No services in this time range", "Widen the time range, or point OpenTelemetry at Oscilyx if you have not sent data yet.", el("a", { class: "btn primary", href: "#/setup" }, "Setup & integrations")),
      });
      const apply = () => {
        const t = search.value.trim().toLowerCase(), hv = hsel.value;
        tbl.repaint(rows.filter(r => (!t || r.service.toLowerCase().includes(t)) && (!hv || (r.health?.state || "none") === hv)));
        setParams({ s: search.value.trim(), health: hv });
      };
      const search = el("input", { type: "search", placeholder: "Filter services…", value: term, style: "width:240px", on: { input: apply } });
      const hsel = el("select", { on: { change: apply } }, ...[["", "All health"], ["healthy", "Healthy"], ["degraded", "Degraded"], ["unhealthy", "Unhealthy"]].map(([v, l]) => el("option", { value: v, selected: v === hf }, l)));
      page.append(head("Services", `${rows.length} ${showAll ? "resources" : "services with traces"} · ranked by request rate`,
        search, hsel, el("label", { class: "switch" }, el("input", { type: "checkbox", checked: showAll, on: { change: e => { setParams({ all: e.target.checked }); P.render(); } } }), el("span"), el("em", {}, "Include log/metric-only"))),
        el("section", { class: "panel fill" }, tbl));
      apply();
    },
  });

  // ---------------------------------------------------------------- service detail
  const TABS = [["overview", "Overview"], ["operations", "Operations"], ["traces", "Traces"], ["logs", "Logs"], ["metrics", "Metrics"], ["attributes", "Resource attributes"]];
  P.route("/services/:name", {
    title: "Service", cleanup() { P.cleanupLogs?.(); P.cleanupLogs = null; },
    crumbs: ({ params }) => [["Services", "#/services"], [params.name]],
    async render(page, { params, q }) {
      const svc = params.name, tab = q.get("tab") || "overview", m = M();
      if (["overview", "metrics", "attributes"].includes(tab)) page.classList.add("scroll");
      const [all, h, attrs] = await Promise.all([P.services(true), api(`/services/${enc(svc)}/health?minutes=${m}`), api("/resources/" + enc(svc)).catch(() => ({}))]);
      const s = all.find(x => x.service === svc) || { n: 0, err: 0, err_pct: 0, p50: 0, p95: 0, p99: 0, rate: 0, avg_ms: 0 };
      page.append(
        head(el("span", {}, svc, " ", attrs["service.version"] ? el("span", { class: "chip", style: "vertical-align:middle" }, "v" + attrs["service.version"]) : null, " ", healthPill(h)), attrs["deployment.environment"] ? `Environment: ${attrs["deployment.environment"]}` : h.reason,
          el("a", { class: "btn", href: `#/traces?service=${enc(svc)}` }, icon("traces", 15), "Traces"), el("a", { class: "btn", href: `#/logs?service=${enc(svc)}` }, icon("logs", 15), "Logs")),
        el("div", { class: "tabs" }, ...TABS.map(([k, l]) => el("a", { href: `#/services/${enc(svc)}?tab=${k}`, class: tab === k ? "on" : "" }, l))));
      const body = el("div", { class: "col fill", style: "min-height:0" }); page.append(body);
      if (tab === "overview") {
        const [ts, ops, ins] = await Promise.all([api(`/services/${enc(svc)}/timeseries?minutes=${m}&buckets=60`), api(`/operations?service=${enc(svc)}&minutes=${m}`), api("/insights?minutes=" + m)]);
        const mine = ins.filter(i => i.service === svc);
        body.append(el("div", { class: "kpis" }, kpi("Requests", compact(s.n), `${rate(s.rate)} req/s`), kpi("Error rate", pct(s.err_pct), `${s.err} failed`, { cls: s.err_pct >= 5 ? "err" : s.err_pct >= 1 ? "warn" : "ok" }), kpi("p50", ms(s.p50)), kpi("p95", ms(s.p95), h.signals ? `${h.signals.p95_drift_pct >= 0 ? "+" : ""}${h.signals.p95_drift_pct}% vs prior` : ""), kpi("p99", ms(s.p99)), kpi("Error logs/min", h.signals ? h.signals.log_err_per_min : "—")),
          el("div", { class: "row", style: "flex:1;min-height:260px" },
            el("div", { class: "col", style: "flex:2" }, panel("Traffic & errors", chart({ t: ts.map(x => x.t), type: "area", h: 190, series: [{ name: "requests/s", values: ts.map(x => x.rate) }, { name: "errors/s", values: ts.map(x => x.err), color: "var(--err)" }] }), { cls: "" }), panel("Average latency", chart({ t: ts.map(x => x.t), type: "line", h: 150, f: ms, series: [{ name: "avg latency", values: ts.map(x => x.avg_ms), color: "var(--c4)" }] }))),
            el("div", { class: "col", style: "flex:1;min-width:300px" }, panel("Insights", mine.length ? el("div", {}, ...mine.map(insightRow)) : empty("No issues", h.reason), { bodyCls: "pb", style: "" }), panel("Top operations", opsTable(ops.slice(0, 6), svc, true), { bodyCls: "pb", style: "padding:0" }))));
      } else if (tab === "operations") {
        const ops = await api(`/operations?service=${enc(svc)}&minutes=${m}`);
        body.append(el("section", { class: "panel fill" }, opsTable(ops, svc)));
      } else if (tab === "traces") await tracesBlock(body, { service: svc, fixed: true, q });
      else if (tab === "logs") await logsBlock(body, { service: svc, fixed: true, q });
      else if (tab === "metrics") {
        const names = (await api("/metrics/names?" + qs({ minutes: M() }))).filter(n => (n.services || "").split(",").includes(svc));
        if (!names.length) body.append(el("section", { class: "panel fill" }, empty("No metrics for this service", "Send metrics with this service.name to see them here.")));
        else {
          const g = el("div", { class: "grid", style: "grid-template-columns:repeat(auto-fill,minmax(420px,1fr))" }); body.append(g);
          for (const n of names.slice(0, 12)) {
            const d = await api("/metrics/query?" + qs({ name: n.name, service: svc, minutes: m, buckets: 50, agg: n.mtype === "sum" ? "sum" : "avg" }));
            g.append(panel(el("span", { class: "mono" }, n.name), chart({ t: d.t, type: "area", h: 150, series: Object.entries(d.series).map(([name, values]) => ({ name, values })) }), { sub: n.unit || n.mtype, right: el("a", { class: "btn sm ghost", href: `#/metrics?name=${enc(n.name)}&service=${enc(svc)}` }, "Explore") }));
          }
        }
      } else {
        const keys = Object.keys(attrs).sort();
        body.append(el("section", { class: "panel fill" }, el("div", { class: "pb", style: "padding:0" }, keys.length ? el("div", { class: "kv" }, ...keys.flatMap(k => [el("div", { class: "mono" }, k), el("div", { class: "mono" }, typeof attrs[k] === "object" ? JSON.stringify(attrs[k]) : String(attrs[k]))])) : empty("No resource attributes"))));
      }
    },
  });
  function opsTable(ops, svc, mini = false) {
    return dataTable({
      keep: "ops", rows: ops, sortKey: "n", sortDir: -1,
      cols: [{ key: "name", label: "Operation", render: r => el("span", { class: "mono" }, r.name) }, { key: "n", label: "Count", num: true, render: r => fmt(r.n) },
        { key: "err", label: "Errors", num: true, render: r => r.err ? pill(`${fmt(r.err)} · ${pct(100 * r.err / r.n)}`, "err") : "—" },
        ...(mini ? [] : [{ key: "avg_ms", label: "Avg", num: true, render: r => ms(r.avg_ms) }]), { key: "max_ms", label: "Max", num: true, render: r => ms(r.max_ms) }],
      onRow: r => nav("/traces", { service: svc, q_: r.name }), empty: empty("No operations in range"),
    });
  }

  // ============================================================ SERVICE MAP

  // ============================================================ TRACES
  async function tracesBlock(host, { service = "", fixed = false, q }) {
    const svcs = fixed ? [] : await P.services();
    const state = { service, errors: q.get("errors") === "1", term: q.get("q_") || "", min: q.get("min") || "" };
    if (!fixed) state.service = q.get("service") || "";
    const box = el("div", { class: "fill", style: "display:flex;flex-direction:column;min-height:0" });
    const count = el("span", { class: "muted" });
    async function load() {
      const rows = await api("/traces?" + qs({ minutes: M(), service: state.service, errors: state.errors, min_ms: state.min, q_: state.term, limit: 200 }));
      count.textContent = `${rows.length} trace${rows.length === 1 ? "" : "s"}`;
      const maxd = Math.max(1, ...rows.map(r => r.dur_ms));
      const tbl = dataTable({
        keep: "traces", rows, sortKey: "start_ns", sortDir: -1, rowClass: r => (q.get("id") === r.trace_id ? "sel" : ""),
        cols: [{ key: "start_ns", label: "Time", render: r => el("span", { class: "muted" }, clock(r.start_ns / 1e6)) }, { key: "root", label: "Root span", render: r => el("span", { class: "mono" }, r.root || "—") },
          { key: "root_service", label: "Service", render: r => r.root_service ? pill(r.root_service, "") : "—" },
          { key: "dur_ms", label: "Duration", num: true, render: r => el("span", { style: "display:inline-flex;align-items:center;gap:8px;justify-content:flex-end" }, el("span", { class: "bar-in", style: `width:${Math.max(2, 70 * r.dur_ms / maxd)}px;background:${r.errors ? "var(--err)" : "var(--c1)"}` }), ms(r.dur_ms)) },
          { key: "spans", label: "Spans", num: true }, { key: "services", label: "Services", num: true },
          { key: "errors", label: "Status", num: true, render: r => r.errors ? pill(`${r.errors} error${r.errors > 1 ? "s" : ""}`, "err") : pill("OK", "ok") }, { key: "trace_id", label: "Trace ID", sort: false, render: r => el("span", { class: "chip" }, r.trace_id.slice(0, 12)) }],
        onRow: r => { setParams({ id: r.trace_id }); openTrace(r.trace_id); }, empty: (state.service || state.errors || Number(state.min) > 0 || state.term) ? empty("No traces match", "Widen the time range or relax the filters.") : empty("No traces in this time range", "Widen the time range, or point OpenTelemetry at Oscilyx if you have not sent data yet.", el("a", { class: "btn primary", href: "#/setup" }, "Setup & integrations")),
      });
      box.replaceChildren(el("section", { class: "panel fill" }, tbl));
    }
    const upd = () => { if (!fixed) setParams({ service: state.service, errors: state.errors, q_: state.term, min: state.min }); load(); };
    const tb = el("div", { class: "toolbar" },
      fixed ? null : el("select", { on: { change: e => { state.service = e.target.value; upd(); } } }, el("option", { value: "" }, "All services"), ...svcs.map(s => el("option", { value: s.service, selected: s.service === state.service }, s.service))),
      el("input", { type: "search", placeholder: "Search span name, attribute or trace id…", value: state.term, style: "width:min(380px,100%);flex:1;max-width:420px", on: { keydown: e => { if (e.key === "Enter") { state.term = e.target.value.trim(); upd(); } } } }),
      el("button", { class: "chipbtn" + (state.errors ? " on" : ""), on: { click: e => { state.errors = !state.errors; e.currentTarget.classList.toggle("on", state.errors); upd(); } } }, el("span", { class: "pill err", style: "height:8px;width:8px;padding:0" }), "Errors only"),
      el("input", { type: "number", placeholder: "min ms", value: state.min, style: "width:92px", on: { change: e => { state.min = e.target.value; upd(); } } }), el("span", { class: "sp" }), count);
    host.append(tb, box); await load();
  }
  P.route("/traces", {
    title: "Traces",
    async render(page, { q }) {
      page.append(head("Traces", "End-to-end distributed traces across services"));
      const host = el("div", { class: "col fill", style: "min-height:0" }); page.append(host);
      await tracesBlock(host, { q });
      const id = q.get("id"); if (id && !(P.drawerOpen === "drawer" && P.openTraceId === id)) openTrace(id);
    },
  });

  async function openTrace(id) {
    P.openTraceId = id;
    let t; try { t = await api("/traces/" + id); } catch (e) { toast(e.message, "err"); return; }
    const spans = t.spans; const ids = new Set(spans.map(s => s.span_id)), kids = {};
    spans.forEach(s => { const p = ids.has(s.parent_id) ? s.parent_id : ""; (kids[p] = kids[p] || []).push(s); });
    Object.values(kids).forEach(a => a.sort((x, y) => x.start_ns - y.start_ns));
    const order = []; (function walk(p, d) { for (const s of kids[p] || []) { order.push({ s, d }); walk(s.span_id, d + 1); } })("", 0);
    const t0 = Math.min(...spans.map(s => s.start_ns)), t1 = Math.max(...spans.map(s => s.end_ns)), total = Math.max(1, t1 - t0);
    const svcIdx = {}; spans.forEach(s => { if (!(s.service in svcIdx)) svcIdx[s.service] = Object.keys(svcIdx).length; });
    const errs = spans.filter(s => s.status === 2), root = order[0]?.s;
    const side = el("div", {}); const rows = {};
    const kindName = k => ({ 1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer" }[k] || "unspecified");
    function select(s) {
      Object.entries(rows).forEach(([k, r]) => r.classList.toggle("sel", k === s.span_id));
      const attrs = Object.entries(s.attrs || {}), logs = t.logs.filter(l => l.span_id === s.span_id);
      side.replaceChildren(
        el("div", { class: "sect" }, "Span"), el("div", { class: "kv" }, el("div", {}, "Name"), el("div", { class: "mono" }, s.name), el("div", {}, "Service"), el("div", {}, s.service), el("div", {}, "Kind"), el("div", {}, kindName(s.kind)), el("div", {}, "Status"), el("div", {}, s.status === 2 ? pill("error", "err") : pill(s.status === 1 ? "ok" : "unset", s.status === 1 ? "ok" : "")), el("div", {}, "Duration"), el("div", {}, ms(s.dur_ms)), el("div", {}, "Offset"), el("div", {}, ms((s.start_ns - t0) / 1e6)), ...(s.status_msg ? [el("div", {}, "Message"), el("div", {}, s.status_msg)] : []), el("div", {}, "Span ID"), el("div", { class: "mono" }, s.span_id)),
        el("div", { class: "sect" }, `Attributes (${attrs.length})`), attrs.length ? el("div", { class: "kv" }, ...attrs.flatMap(([k, v]) => [el("div", { class: "mono" }, k), el("div", { class: "mono" }, typeof v === "object" ? JSON.stringify(v) : String(v))])) : el("div", { class: "muted", style: "padding:4px 14px 10px" }, "none"),
        el("div", { class: "sect" }, logs.length ? `Logs for this span (${logs.length})` : `Trace logs (${t.logs.length})`),
        (logs.length ? logs : t.logs).length ? el("div", {}, ...(logs.length ? logs : t.logs).map(l => el("div", { class: "log", style: "grid-template-columns:70px 54px minmax(0,1fr)", on: { click: () => { const sp = spans.find(x => x.span_id === l.span_id); if (sp) select(sp); } } }, el("span", { class: "t" }, clock(l.ts_ns / 1e6)), el("span", { class: "sevt " + sevName(l.severity) }, sevName(l.severity)), el("span", { class: "b" }, l.body)))) : el("div", { class: "muted", style: "padding:4px 14px 10px" }, "No correlated logs"));
    }
    const wf = el("div", { class: "wf" }, el("div", { class: "wf-head" }, el("div", { style: "padding:8px 10px" }, "Span"), el("div", { class: "ruler" }, ...[0, .25, .5, .75, 1].map(f => el("span", { style: `left:${f * 100}%` }, ms(total * f / 1e6))))));
    for (const { s, d } of order) {
      const left = (s.start_ns - t0) / total * 100, w = Math.max(.4, (s.end_ns - s.start_ns) / total * 100), lblLeft = left + w > 82;
      const r = el("div", { class: "wf-row", on: { click: () => select(s) } }, el("div", { class: "wf-n", style: `padding-left:${10 + d * 16}px` }, el("i", { style: `width:7px;height:7px;border-radius:50%;flex:none;background:${COLORS[svcIdx[s.service] % COLORS.length]}` }), el("span", { class: "nm", title: s.name }, s.name), el("span", { class: "sv" }, s.service)),
        el("div", { class: "wf-t" }, el("div", { class: "wf-bar" + (s.status === 2 ? " err" : ""), style: `left:${left}%;width:${w}%;${s.status === 2 ? "" : `background:${COLORS[svcIdx[s.service] % COLORS.length]}`}` }), el("div", { class: "wf-lbl", style: lblLeft ? `right:${100 - left}%;padding-right:6px` : `left:${left + w}%` }, ms(s.dur_ms))));
      rows[s.span_id] = r; wf.append(r);
    }
    const dh = el("div", { class: "dh" }, el("div", {}, el("h2", {}, root ? root.name : "Trace", " ", errs.length ? pill(`${errs.length} error${errs.length > 1 ? "s" : ""}`, "err") : pill("OK", "ok")), el("div", { class: "muted", style: "font-size:12px;margin-top:2px;display:flex;gap:12px;flex-wrap:wrap" }, el("span", { class: "chip" }, id), ...[`${spans.length} spans`, `${Object.keys(svcIdx).length} services`, ms(total / 1e6), stamp(t0 / 1e6)].map(x => el("span", {}, x)))),
      el("button", { class: "btn sm accent", on: { click: () => P.explain(`trace ${id}`, `Trace ${id}${root ? ` (root span ${root.name} in ${root.service})` : ""}: ${spans.length} spans, ${errs.length} with errors. Why is it slow or failing? Use get_trace to inspect it.`) } }, icon("sparkle", 13), P.aiStatus?.enabled ? `Explain with ${P.aiStatus.name || "AI"}` : "Explain"), el("button", { class: "btn sm", on: { click: () => copyText(id) } }, icon("copy", 13), "Copy ID"), el("button", { class: "btn icon ghost x", on: { click: P.closeDrawer } }, icon("x")));
    P.openDrawer(el("div", { class: "db" }, dh, el("div", { class: "split" }, el("div", { class: "l", "data-keep": "wf" }, wf), el("div", { class: "r" }, side))));
    select(errs[0] || root || spans[0]);
  }

  // ============================================================ LOGS
  async function logsBlock(host, { service = "", fixed = false, q }) {
    const svcs = fixed ? [] : await P.services(true);
    const st = { service: fixed ? service : q.get("service") || "", sev: +(q.get("sev") || 0) || 0, q: q.get("q") || "", env: fixed ? "" : q.get("env") || "", live: false,
      from: +q.get("from") || 0, to: +q.get("to") || 0,            // a time window dragged out on the chart (epoch ms); 0 = the page's time range
      zoom: [],                                                    // earlier windows, so "Back" steps out one zoom at a time
      svcSort: P.store.get("logs.svcsort", "errors") };           // service list: most errors first, or most records first
    if (!(st.from && st.to > st.from)) st.from = st.to = 0;
    // Layout choices persist per browser: chart size, filter sidebar, and focus mode (header hidden) which is per visit.
    const ui = { chart: P.store.get("logs.chart", "small"), facets: P.store.get("logs.facets", "on"), focus: false };
    const CHART_H = { small: 92, large: 190 };
    let hist = [];
    const rowsBox = el("div", { class: "pb", style: "padding:0", "data-keep": "logs" }), facets = el("div", { class: "facet" }), count = el("small", {}), histHost = el("div");
    const seen = new Set(); let timer = null;
    const sevOpts = [[0, "All levels"], [9, "INFO and above"], [13, "WARN and above"], [17, "ERROR and above"]];
    // ---- expandable rows. Click a row to open it in place: full message with the stack trace laid out, where it ran
    // (namespace / pod / container / node, click one to filter), the trace, and every attribute. Open rows stay open
    // across live-tail refreshes. "Details" still opens the side drawer.
    const openKeys = new Set();
    const rowEls = [];
    const rowKey = r => r.ts_ns + "|" + r.service + "|" + (r.body || "").length;
    const K8S = [["k8s.cluster.name", "cluster"], ["k8s.namespace.name", "namespace"], ["k8s.pod.name", "pod"],
                 ["k8s.container.name", "container"], ["k8s.node.name", "node"]];
    const ctxOf = r => {
      if (r.k8s && Object.keys(r.k8s).length) return r.k8s;
      const res = r.res && typeof r.res === "object" ? (r.res.attributes && typeof r.res.attributes === "object" ? r.res.attributes : r.res) : {};
      const a = r.attrs || {};
      return Object.fromEntries(K8S.map(([k]) => [k, res[k] || a[k] || ""]).filter(([, v]) => v));
    };
    function renderMessage(body) {
      // Exception headline red, stack frames dimmed, so the line that matters is the one you see first.
      const box = el("pre", { class: "lr-msg mono" });
      (body || "").split("\n").forEach((ln, i, all) => {
        const frame = /^\s*(at |File "|\.\.\. \d+ more|Caused by:|goroutine |\s+\S+\.go:\d+|\s+\S+\.(py|java|js|cs):\d+)/.test(ln);
        const head = !frame && /(Exception|Error|Traceback|panic|FATAL|Caused by)/.test(ln);
        box.append(el("span", { class: frame ? "fr" : head ? "hd" : "" }, ln + (i < all.length - 1 ? "\n" : "")));
      });
      return box;
    }
    function logDetail(r) {
      const ctx = ctxOf(r);
      const addFilter = (k, v) => { st.q = (st.q + " " + k + "=" + v).trim(); qin.value = st.q; tab = "table"; renderTabs(); update(); };
      const attrs = Object.entries(r.attrs || {}).filter(([k]) => !k.startsWith("oscilyx."));
      const chips = [el("span", { class: "lr-chip", title: "Filter to this service", on: { click: e => { e.stopPropagation(); st.service = r.service; update(); } } }, el("em", {}, "service"), r.service),
        ...K8S.filter(([k]) => ctx[k]).map(([k, label]) => el("span", { class: "lr-chip", title: "Filter: " + k + "=" + ctx[k],
          on: { click: e => { e.stopPropagation(); addFilter(k, ctx[k]); } } }, el("em", {}, label), ctx[k]))];
      return el("div", { class: "lr-detail", on: { click: e => e.stopPropagation() } },
        el("div", { class: "lr-actions" },
          el("button", { class: "btn sm", on: { click: () => copyText(r.body || "") } }, "Copy message"),
          r.trace_id ? el("button", { class: "btn sm primary", on: { click: () => { location.hash = "#/traces?id=" + r.trace_id; } } }, "Open trace") : null,
          el("button", { class: "btn sm ghost", on: { click: () => openLog(r) } }, "Details ▸"),
          el("span", { class: "muted lr-when" }, new Date(r.ts_ns / 1e6).toLocaleString())),
        renderMessage(r.body),
        el("div", { class: "lr-chips" }, ...chips),
        attrs.length ? el("details", { class: "lr-attrs" }, el("summary", {}, attrs.length + " attributes"),
          el("div", { class: "kv" }, ...attrs.flatMap(([k, v]) => [el("div", { class: "mono kvk" }, k), el("div", { class: "mono kvv" }, typeof v === "object" ? JSON.stringify(v) : String(v))]))) : null);
    }
    function logRow(r, fresh) {
      const sn = r.sev_text || sevName(r.severity);
      const inferred = r.attrs && r.attrs["oscilyx.severity.inferred"];
      const lines = (r.body || "").split("\n");
      const multiline = lines.length > 1;
      const pod = ctxOf(r)["k8s.pod.name"];
      const wrap = el("div", { class: "lr " + (/^(ERROR|FATAL)/.test(sn) ? "err" : sn === "WARN" ? "warn" : "") + (fresh ? " new" : "") });
      const caret = el("span", { class: "lr-caret" }, "▸");
      const line = el("div", { class: "lr-line", on: { click: () => toggle() } },
        caret,
        el("span", { class: "t" }, clock(r.ts_ns / 1e6)),
        el("span", { class: "sevt " + sn + " lr-sev", title: inferred ? "Severity inferred from the log text" : "" }, sn, inferred ? el("span", { class: "inf" }, "~") : null),
        el("span", { class: "sv", title: r.service }, r.service),
        el("span", { class: "b1" }, lines[0] || "(empty)",
          multiline ? el("span", { class: "ml-badge" }, "+" + (lines.length - 1) + " lines") : null,
          pod ? el("span", { class: "lr-pod", title: "Pod " + pod }, pod) : null));
      let detail = null;
      function toggle(force) {
        const open = force === undefined ? !wrap.classList.contains("open") : force;
        wrap.classList.toggle("open", open);
        caret.textContent = open ? "▾" : "▸";
        if (open) { detail = detail || logDetail(r); wrap.append(detail); openKeys.add(rowKey(r)); }
        else { detail?.remove(); openKeys.delete(rowKey(r)); }
      }
      wrap.append(line);
      wrap._toggle = toggle;
      rowEls.push(wrap);
      if (openKeys.has(rowKey(r))) toggle(true);
      return wrap;
    }
    function setAllRows(open) { rowEls.forEach(w => w._toggle(open)); }
    // Slide-in detail panel. Reuses the shared drawer (same one traces use).
    function openLog(r) {
      const sn = r.sev_text || sevName(r.severity);
      const inferred = r.attrs && r.attrs["oscilyx.severity.inferred"];
      const attrs = Object.entries(r.attrs || {});
      const res = Object.entries(r.res && typeof r.res === "object" ? r.res : {});
      const kvTable = rows => el("div", { class: "kv", style: "border:1px solid var(--line);border-radius:8px;overflow:hidden" },
        ...rows.flatMap(([k, v]) => [el("div", { class: "mono kvk" }, k), el("div", { class: "mono kvv" }, String(v))]));
      P.openDrawer(el("div", { class: "logdrawer" },
        el("div", { class: "ld-head" },
          el("span", { class: "sevt " + sn }, sn, inferred ? el("span", { class: "inf", title: "Severity inferred from the log text" }, "~") : null),
          el("span", { class: "ld-svc" }, r.service),
          el("span", { class: "ld-time muted" }, new Date(r.ts_ns / 1e6).toLocaleString()),
          el("button", { class: "btn sm icon ghost", title: "Close", style: "margin-left:auto", on: { click: () => P.closeDrawer() } }, "✕")),
        el("div", { class: "ld-sect" }, "Message",
          el("button", { class: "btn sm ghost", style: "float:right", on: { click: () => copyText(r.body) } }, "Copy")),
        el("pre", { class: "ld-body" }, r.body),
        r.trace_id ? el("div", { class: "ld-sect" }, "Trace") : null,
        r.trace_id ? el("div", { style: "display:flex;gap:8px;align-items:center;margin-bottom:10px" },
          el("code", { class: "mono muted" }, r.trace_id.slice(0, 16) + "…"),
          el("button", { class: "btn sm primary", on: { click: () => { P.closeDrawer(); location.hash = "#/traces?id=" + r.trace_id; } } }, "Open trace")) : null,
        attrs.length ? el("div", { class: "ld-sect" }, "Attributes") : null,
        attrs.length ? kvTable(attrs) : null,
        res.length ? el("div", { class: "ld-sect" }, "Resource") : null,
        res.length ? kvTable(res) : null));
    }
    const params = () => ({ minutes: M(), service: st.service, min_severity: st.sev, q_: st.q, env: st.env, limit: 300, start_ms: st.from || "", end_ms: st.to || "" });
    async function loadRows(live) {
      const rows = await api("/logs?" + qs(params()));
      rowEls.length = 0;
      count.textContent = `${rows.length}${rows.length >= 300 ? "+" : ""} entries`;
      if (live && rows.length) { const keep = rowsBox.scrollTop; const fresh = rows.filter(r => !seen.has(r.ts_ns + r.body + r.service)); rows.forEach(r => seen.add(r.ts_ns + r.body + r.service)); rowsBox.replaceChildren(...rows.map(r => logRow(r, fresh.includes(r)))); rowsBox.scrollTop = keep; return; }
      rows.forEach(r => seen.add(r.ts_ns + r.body + r.service));
      rowsBox.replaceChildren(...(rows.length ? rows.map(r => logRow(r)) : [(st.service || Number(st.sev) > 0 || st.q) ? empty("No logs match", "Try another filter or a wider time range.") : empty("No logs in this time range", "Widen the time range, or point OpenTelemetry at Oscilyx if you have not sent data yet.", el("a", { class: "btn primary", href: "#/setup" }, "Setup & integrations"))]));
    }
    async function loadAside() {
      const win = { start_ms: st.from || "", end_ms: st.to || "" };
      const [h, f] = await Promise.all([
        ui.chart === "hide" ? Promise.resolve(hist) : api("/logs/histogram?" + qs({ minutes: M(), service: st.service, env: st.env, buckets: 60, ...win })),
        api("/logs/facets?" + qs({ minutes: M(), service: st.service, min_severity: st.sev, q_: st.q, env: st.env, sort: st.svcSort === "errors" ? "errors" : "", ...win }))]);
      hist = h;
      // Full breakdown by level, most severe at the bottom, so a red band is obvious at a glance.
      if (ui.chart !== "hide") histHost.replaceChildren(chart({ t: h.map(x => x.t), type: "stack", h: CHART_H[ui.chart] || 92, onBrush: zoomTo, series: [
        { name: "Error", values: h.map(x => (x.err || 0) + (x.fatal || 0)), color: "var(--err)" },
        { name: "Warn", values: h.map(x => x.warn || 0), color: "var(--warn)" },
        { name: "Info", values: h.map(x => x.info || 0), color: "var(--info)" },
        { name: "Debug", values: h.map(x => x.debug || 0), color: "var(--c2)" },
        { name: "Unset", values: h.map(x => x.unset || 0), color: "var(--text-3)" },
      ] }));
      const fi = (label, n, on, cb) => el("div", { class: "fi" + (on ? " on" : ""), on: { click: cb } }, el("span", {}, label), el("span", { class: "n" }, compact(n)));
      facets.replaceChildren(el("div", { class: "sect", style: "padding-left:8px" }, "Severity"), ...f.severity.map(x => fi(x.value, x.n, st.sev && sevName(st.sev) === x.value, () => { const n = ({ FATAL: 21, ERROR: 17, WARN: 13, INFO: 9, DEBUG: 5, TRACE: 1 })[x.value]; if (n === undefined) return; /* UNSET has no minimum-severity equivalent */ st.sev = st.sev && sevName(st.sev) === x.value ? 0 : n; update(); })),
        ...(fixed ? [] : [
          el("div", { class: "sect svc-sect", style: "padding-left:8px" }, "Service",
            el("button", { class: "mini", title: "Switch between the services with the most errors and the services with the most records",
              on: { click: () => { st.svcSort = st.svcSort === "errors" ? "count" : "errors"; P.store.set("logs.svcsort", st.svcSort); loadAside(); } } },
              st.svcSort === "errors" ? "most errors ▾" : "most logs ▾")),
          // Red number = ERROR/FATAL records, grey = all records; the red line under a row is the error share.
          ...f.service.map(x => el("div", { class: "fi svc" + (st.service === x.value ? " on" : ""), title: `${x.value}: ${x.err || 0} errors of ${x.n} records`,
              on: { click: () => { st.service = st.service === x.value ? "" : x.value; update(); } } },
            el("span", {}, x.value),
            el("span", { class: "n" }, x.err ? el("b", { class: "e" }, compact(x.err)) : null, el("em", {}, compact(x.n))),
            x.err ? el("i", { class: "eb", style: `width:calc(${Math.max(3, Math.min(100, 100 * x.err / x.n))}% - 16px)` }) : null))]));
    }
    async function loadPatterns() {
      const r = await api("/logs/patterns?" + qs(params()));
      count.textContent = `${r.distinct} pattern${r.distinct === 1 ? "" : "s"} from ${compact(r.scanned)} entries`;
      if (!r.patterns.length) { rowsBox.replaceChildren(empty("No logs match", "Try another filter or a wider time range.")); return; }
      rowsBox.replaceChildren(...r.patterns.map(p => {
        const sn = p.sev_text || "UNSET";
        const row = el("div", { class: "log", style: "cursor:pointer", on: { click: () => {
          // drill in: search the literal prefix of this pattern
          st.q = (p.example || "").split("\n")[0].replace(/[0-9]+/g, " ").split(/\s+/).filter(w => w.length > 3).slice(0, 3).join(" ");
          tab = "table"; renderTabs(); qin.value = st.q; update();
        } } },
          el("span", { class: "t" }, compact(p.count) + "x"),
          el("span", { class: "sevt " + sn }, sn),
          (p.levels ? el("span", { class: "sevbar", title: Object.entries(p.levels).map(([k, v]) => `${k}: ${v}`).join("  ") },
            ...["ERROR", "FATAL", "WARN", "INFO", "DEBUG", "UNSET"].filter(L => p.levels[L]).map(L =>
              el("span", { class: "seg " + L, style: `flex:${p.levels[L]}` }))) : null),
          el("span", { class: "sv", title: p.services.join(", ") }, p.services.slice(0, 2).join(", ") + (p.services.length > 2 ? ` +${p.services.length - 2}` : "")),
          el("span", { class: "bd" }, p.pattern));
        return row;
      }));
    }
    async function loadGroups() {
      const field = groupSel.value || "severity";
      const r = await api("/logs/groupby?" + qs({ ...params(), field }));
      count.textContent = `${r.distinct} value${r.distinct === 1 ? "" : "s"} of ${field} across ${compact(r.scanned)} entries`;
      const max = Math.max(1, ...r.groups.map(g => g.count));
      rowsBox.replaceChildren(...(r.groups.length ? r.groups.map(g =>
        el("div", { class: "log", style: "cursor:pointer", on: { click: () => {
          if (field === "service") { st.service = g.value; if (selSvc) selSvc.value = g.value; }
          else if (field !== "severity" && field !== "env" && g.value !== "(not set)") { st.q = (st.q + " " + field + "=" + g.value).trim(); qin.value = st.q; }
          tab = "table"; renderTabs(); update();
        } } },
          el("span", { class: "t" }, compact(g.count)),
          el("span", { class: "sv" }, g.value),
          el("span", { class: "bd" },
            el("span", { style: `display:inline-block;height:8px;border-radius:4px;background:var(--c5);width:${Math.max(2, 100 * g.count / max)}%` }),
            g.errors ? el("span", { class: "muted", style: "margin-left:8px;font-size:12px" }, `${compact(g.errors)} errors`) : null))
      ) : [empty("No logs match", "Try another filter or a wider time range.")]));
    }
    const update = () => {
      if (!fixed) setParams({ service: st.service, sev: st.sev, q: st.q, env: st.env, from: st.from || "", to: st.to || "" }); else setParams({ sev: st.sev, q: st.q, from: st.from || "", to: st.to || "" });
      syncSev(); if (selSvc) selSvc.value = st.service || ""; renderChips();
      groupSel.style.display = tab === "groups" ? "" : "none";
      if (tab === "patterns") { clearInterval(timer); st.live = false; loadPatterns(); }
      else if (tab === "groups") { clearInterval(timer); st.live = false; loadFields(); loadGroups(); }
      else loadRows();
      loadAside();
    };
    const chips = el("div", { style: "display:flex;gap:6px;flex-wrap:wrap" });
    // Zoom: drag across the chart to look at just that period. Everything on the page (entries, patterns, groups, the
    // service list, the chart itself and the CSV export) then covers only that window.
    function zoomTo(i0, i1) {
      if (!hist.length) return;
      const bw = hist.length > 1 ? hist[1].t - hist[0].t : 60000;
      st.zoom.push(st.from ? [st.from, st.to] : [0, 0]);
      st.from = Math.floor(hist[i0].t); st.to = Math.ceil(hist[i1].t + bw);
      if (st.live) liveBtn.click();
      update();
    }
    function zoomBack() { const [f, t] = st.zoom.pop() || [0, 0]; st.from = f; st.to = t; update(); }
    function zoomReset() { st.zoom = []; st.from = st.to = 0; update(); }
    function zoomChips() {
      if (!st.from) return [];
      const tm = t => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
      const day = new Date(st.from).toDateString() !== new Date().toDateString() ? new Date(st.from).toLocaleDateString() + " " : "";
      return [el("span", { class: "chipbtn on", title: "Everything on this page covers only this window" }, "Window " + day + tm(st.from) + " – " + tm(st.to)),
        st.zoom.length ? el("button", { class: "btn sm", on: { click: zoomBack } }, "◀ Back") : null,
        el("button", { class: "btn sm", on: { click: zoomReset } }, "Reset to " + P.rangeLabel(M()).toLowerCase())].filter(Boolean);
    }
    function renderChips() { chips.replaceChildren(...zoomChips(), ...(st.env ? [el("span", { class: "chipbtn on", title: "Filtered to one environment" }, "env: " + st.env, el("span", { style: "cursor:pointer", on: { click: () => { st.env = ""; update(); } } }, "×"))] : []), ...st.q.split(/\s+/).filter(t => t.includes("=")).map(t => el("span", { class: "chipbtn on" }, t, el("span", { style: "cursor:pointer", on: { click: () => { st.q = st.q.split(/\s+/).filter(x => x !== t).join(" "); qin.value = st.q; update(); } } }, "×")))); }
    const qin = el("input", { type: "search", placeholder: "Search text, or filter with key=value (e.g. http.status_code=500)…", value: st.q, style: "flex:1;min-width:220px;max-width:560px", on: { keydown: e => { if (e.key === "Enter") { st.q = e.target.value.trim(); update(); } } } });
    const sevSel = el("select", { on: { change: e => { st.sev = +e.target.value; update(); } } }, ...sevOpts.map(([v, l]) => el("option", { value: v, selected: v === st.sev }, l)));
    // Show the nearest option at or below the active minimum severity; never leave the dropdown blank.
    const syncSev = () => { const v = sevOpts.map(o => o[0]).filter(o => o <= (+st.sev || 0)).pop() ?? 0; sevSel.value = String(v); };
    const liveBtn = el("button", { class: "btn", on: { click: () => { st.live = !st.live; if (st.live && st.from) { st.zoom = []; st.from = st.to = 0; renderChips(); loadAside(); } liveBtn.classList.toggle("primary", st.live); liveBtn.replaceChildren(icon(st.live ? "pause" : "play", 14), st.live ? "Live tail on" : "Live tail"); clearInterval(timer); if (st.live) timer = setInterval(() => loadRows(true), 2000); } } }, icon("play", 14), "Live tail");
    P.cleanupLogs = () => { clearInterval(timer); document.body.classList.remove("logs-focus"); };
    const selSvc = fixed ? null : el("select", { on: { change: e => { st.service = e.target.value; update(); } } }, el("option", { value: "" }, "All services"), ...svcs.map(s => el("option", { value: s.service, selected: s.service === st.service }, s.service)));
    // Tabs: the same filters, three ways of looking at the result.
    const tabHost = el("div", { class: "col fill", style: "min-height:0" });
    const tabs = [["table", "Table"], ["patterns", "Patterns"], ["groups", "Groups"]];
    let tab = "table";
    const tabBar = el("div", { class: "tabs", style: "border:0;flex:none" });
    function renderTabs() {
      tabBar.replaceChildren(...tabs.map(([k, label]) =>
        el("button", { class: tab === k ? "on" : "", on: { click: () => { if (tab !== k) { tab = k; renderTabs(); update(); } } } }, label)));
    }
    renderTabs();
    const exportBtn = el("button", { class: "btn", title: "Download the current view as CSV", on: { click: () => {
      window.open("/api/logs/export?" + qs(params()), "_blank");
    } } }, icon("download", 14), "CSV");
    const groupSel = el("select", { style: "display:none", on: { change: () => update() } });
    async function loadFields() {
      try {
        const f = await api("/logs/fields?" + qs({ minutes: M() }));
        const cur = groupSel.value;
        groupSel.replaceChildren(...[...f.builtin, ...f.attributes.map(a => a.key)].map(k => el("option", { value: k, selected: k === cur }, "Group by " + k)));
        if (!groupSel.value) groupSel.value = st.service ? "severity" : "service";  // "which services?" is the usual question
      } catch (e) { /* grouping is optional */ }
    }
    // Views button: a popover with built-in and saved views so the sidebar stays clean. Placed next to the
    // tab bar because "which slice am I looking at?" belongs with the tab controls, not with the search.
    const viewsWrap = el("div", { class: "menu-wrap", style: "display:inline-block" });
    const viewsBtn = el("button", { class: "btn sm", title: "Saved and built-in views",
      on: { click: () => P.openViewsMenu(viewsBtn, "logs", {
        currentParams: viewParams(), onApply: applyView, onSaved: () => {} }) } },
      icon("logs", 14), "Views");
    viewsWrap.appendChild(viewsBtn);
    function viewParams() {
      // Only include fields that identify the view; omit defaults so an unset filter does not count as "set".
      const o = {}; if (st.service) o.service = st.service; if (st.sev) o.min_severity = st.sev;
      if (st.q) o.q = st.q; if (st.env) o.env = st.env; if (tab !== "table") o.view = tab;
      return o;
    }
    function applyView(v) {
      const p = v.params || {};
      st.service = p.service || ""; st.sev = +p.min_severity || 0; st.q = p.q || ""; st.env = p.env || st.env;
      if (p.view && ["patterns", "groups"].includes(p.view)) { tab = p.view; renderTabs(); }
      else { tab = "table"; renderTabs(); }
      if (p.groupby) { tab = "groups"; renderTabs(); groupSel.value = p.groupby; }
      if (qin) qin.value = st.q;
      update();
    }
    // Layout controls: give the log lines the room. Hide the chart or the filter sidebar, make the chart bigger, or hide the
    // page header and banner (Focus). Choices are remembered in this browser.
    const histPanel = el("section", { class: "panel", style: "flex:none", title: "Drag across the chart to zoom into a time window" },
      el("div", { class: "ph lr-vol" }, "Log volume", el("small", { class: "muted" }, "drag across the bars to zoom into a time window")),
      el("div", { class: "pb", style: "padding:8px 12px" }, histHost));
    const layout = el("div", { class: "logs-layout fill" }, el("section", { class: "panel facetp" }, el("div", { class: "pb", style: "padding:4px 4px 8px" }, facets)),
      el("div", { class: "col" },
        el("section", { class: "panel fill" },
          el("div", { class: "ph" }, "Log entries", count, el("span", { class: "sp" }),
            el("button", { class: "btn sm", title: "Show only ERROR and FATAL", on: { click: () => { st.sev = st.sev >= 17 ? 0 : 17; update(); } } }, "Errors only"),
            el("button", { class: "btn sm", title: "Open every row below", on: { click: () => setAllRows(true) } }, "Expand all"),
            el("button", { class: "btn sm", title: "Close every open row", on: { click: () => setAllRows(false) } }, "Collapse")),
          rowsBox),
        histPanel));
    const filtersBtn = el("button", { class: "btn sm", title: "Show or hide the filter sidebar", on: { click: () => { ui.facets = ui.facets === "on" ? "off" : "on"; P.store.set("logs.facets", ui.facets); applyLayout(); } } }, "Filters");
    const chartSel = el("select", { title: "Chart size", on: { change: e => { ui.chart = e.target.value; P.store.set("logs.chart", ui.chart); applyLayout(); if (ui.chart !== "hide") loadAside(); } } },
      ...[["hide", "Chart: hidden"], ["small", "Chart: small"], ["large", "Chart: large"]].map(([v, l]) => el("option", { value: v, selected: v === ui.chart }, l)));
    const focusBtn = fixed ? null : el("button", { class: "btn sm", title: "Hide the page header and banner so the log lines get the whole screen", on: { click: () => { ui.focus = !ui.focus; applyLayout(); } } }, "Focus");
    function applyLayout() {
      histPanel.style.display = ui.chart === "hide" ? "none" : "";
      layout.classList.toggle("nofacet", ui.facets !== "on");
      filtersBtn.classList.toggle("primary", ui.facets === "on");
      focusBtn?.classList.toggle("primary", ui.focus);
      document.body.classList.toggle("logs-focus", !fixed && ui.focus);
    }
    const layoutBar = el("span", { class: "layout-bar" }, filtersBtn, chartSel, focusBtn);
    host.append(el("div", { class: "toolbar" }, selSvc, sevSel, qin, groupSel, el("span", { class: "sp" }), tabBar, viewsWrap, layoutBar, exportBtn, liveBtn), chips, layout);
    applyLayout(); renderChips(); loadFields(); await Promise.all([loadRows(), loadAside()]);
  }
  P.route("/logs", {
    title: "Logs", live: false, cleanup() { P.cleanupLogs?.(); P.cleanupLogs = null; },
    async render(page, { q }) {
      page.append(head("Logs", "Full-text and attribute search with trace correlation"));
      const host = el("div", { class: "col fill", style: "min-height:0" }); page.append(host);
      await logsBlock(host, { q });
    },
  });
  // ============================================================ ERRORS
  // Groups ERROR+ logs by message pattern, with one row per group: sparkline, first/last seen,
  // affected services, count. Clicking a row drills into the Logs view filtered to that pattern.
  async function errorsBlock(host, { q }) {
    const svcs = await P.services(true);
    const st = { service: q.get("service") || "", env: q.get("env") || "", q: q.get("q") || "",
                 sort: q.get("sort") || "count", new_within_hours: +(q.get("new_within_hours") || 0) || 0 };
    const rowsBox = el("div", { class: "pb", style: "padding:0;overflow:auto", "data-keep": "errors" });
    const count = el("small", { class: "muted" });

    const selSvc = el("select", { on: { change: e => { st.service = e.target.value; update(); } } },
      el("option", { value: "" }, "All services"), ...svcs.map(s => el("option", { value: s.service, selected: s.service === st.service }, s.service)));
    const sortSel = el("select", { on: { change: e => { st.sort = e.target.value; update(); } } },
      ...[["count", "Most frequent"], ["last_seen", "Latest first"], ["first_seen", "Oldest first"], ["new", "New (last 24h)"]].map(
        ([v, t]) => el("option", { value: v, selected: v === st.sort }, t)));
    const qin = el("input", { placeholder: "Search within error text", value: st.q, style: "flex:1;max-width:320px" });
    let qTimer = null;
    qin.addEventListener("input", () => { clearInterval(qTimer); qTimer = setTimeout(() => { st.q = qin.value.trim(); update(); }, 300); });

    const viewsWrap = el("div", { class: "menu-wrap", style: "display:inline-block" });
    const viewsBtn = el("button", { class: "btn sm", title: "Saved and built-in views",
      on: { click: () => P.openViewsMenu(viewsBtn, "errors", { currentParams: viewParams(), onApply: applyView }) } },
      icon("logs", 14), "Views");
    viewsWrap.appendChild(viewsBtn);
    function viewParams() {
      const o = { sort: st.sort };
      if (st.service) o.service = st.service;
      if (st.q) o.q = st.q;
      if (st.env) o.env = st.env;
      if (st.new_within_hours > 0) o.new_within_hours = st.new_within_hours;
      return o;
    }
    function applyView(v) {
      const p = v.params || {};
      st.service = p.service || ""; st.q = p.q || ""; st.env = p.env || st.env;
      st.sort = p.sort || "count"; st.new_within_hours = +p.new_within_hours || 0;
      if (st.new_within_hours > 0) st.sort = "new";
      selSvc.value = st.service; sortSel.value = st.sort; qin.value = st.q;
      update();
    }

    function timeAgo(ns) {
      const sec = Math.max(0, Math.round((Date.now() - ns / 1e6) / 1000));
      if (sec < 60) return sec + "s ago";
      if (sec < 3600) return Math.round(sec / 60) + "m ago";
      if (sec < 86400) return Math.round(sec / 3600) + "h ago";
      return Math.round(sec / 86400) + "d ago";
    }

    function errorRow(g) {
      const sn = g.sev_text || "ERROR";
      const spark = P.sparkline(g.sparkline, { w: 110, h: 22, color: "var(--err,#e76e6e)" });
      const svcText = g.services.slice(0, 2).join(", ") + (g.services.length > 2 ? ` +${g.services.length - 2}` : "");
      return el("div", { class: "err-row", on: { click: () => openErrDetail(g) } },
        el("div", { class: "err-count" }, compact(g.count)),
        el("div", { class: "err-sev" }, el("span", { class: "sevt " + sn }, sn)),
        el("div", { class: "err-msg" },
          el("div", { class: "err-pattern mono" }, g.pattern.slice(0, 180)),
          el("div", { class: "err-meta muted" },
            el("span", { title: g.services.join(", ") }, svcText),
            el("span", {}, "·"),
            el("span", { title: new Date(g.first_ts / 1e6).toLocaleString() }, "first " + timeAgo(g.first_ts)),
            el("span", {}, "·"),
            el("span", { title: new Date(g.last_ts / 1e6).toLocaleString() }, "last " + timeAgo(g.last_ts)))),
        el("div", { class: "err-spark" }, spark));
    }

    function openErrDetail(g) {
      const example = g.example || g.pattern;
      P.openDrawer(el("div", { class: "logdrawer" },
        el("div", { class: "ld-head" },
          el("span", { class: "sevt " + (g.sev_text || "ERROR") }, g.sev_text || "ERROR"),
          el("span", { class: "ld-svc" }, g.services.slice(0, 3).join(", ")),
          el("span", { class: "ld-time muted" }, compact(g.count) + " occurrences"),
          el("button", { class: "btn sm icon ghost", title: "Close", style: "margin-left:auto",
            on: { click: () => P.closeDrawer() } }, "✕")),
        el("div", { class: "ld-sect" }, "Pattern"),
        el("pre", { class: "ld-body" }, g.pattern),
        el("div", { class: "ld-sect" }, "Example"),
        el("pre", { class: "ld-body" }, example),
        el("div", { class: "ld-sect" }, "Seen"),
        el("div", { style: "display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin-bottom:10px;font-size:13px" },
          el("span", { class: "muted" }, "First"), el("span", {}, new Date(g.first_ts / 1e6).toLocaleString()),
          el("span", { class: "muted" }, "Last"), el("span", {}, new Date(g.last_ts / 1e6).toLocaleString()),
          el("span", { class: "muted" }, "Services"), el("span", {}, g.services.join(", ") || "(none)")),
        el("div", { style: "display:flex;gap:8px" },
          el("button", { class: "btn sm primary", on: { click: () => {
            P.closeDrawer();
            const term = g.pattern.replace(/<[a-z]+>/g, " ").split(/\s+/).filter(w => w.length > 3).slice(0, 3).join(" ");
            location.hash = "#/logs?" + qs({ min_severity: 17, service: g.services[0] || "", q: term, minutes: S.minutes });
          } } }, "See matching logs"),
          g.trace_id ? el("button", { class: "btn sm", on: { click: () => { P.closeDrawer(); location.hash = "#/traces?id=" + g.trace_id; } } }, "Open example trace") : null)));
    }

    const update = async () => {
      setParams({ service: st.service, q: st.q, env: st.env, sort: st.sort,
                  new_within_hours: st.new_within_hours > 0 ? st.new_within_hours : "" });
      try {
        const r = await api("/errors?" + qs({ minutes: M(), service: st.service, env: st.env, q_: st.q,
                                              sort: st.sort, new_within_hours: st.new_within_hours }));
        count.textContent = `${r.distinct} error group${r.distinct === 1 ? "" : "s"} from ${compact(r.scanned)} entries`;
        rowsBox.replaceChildren(...(r.errors.length ? r.errors.map(errorRow) :
          [empty("No errors", st.service ? "Try a wider time range or remove the service filter." : "The current time range is clean.")]));
      } catch (e) { rowsBox.replaceChildren(empty("Could not load errors", e.message || "")); }
    };

    host.append(
      el("div", { class: "toolbar" }, selSvc, sortSel, qin, el("span", { class: "sp" }), count, viewsWrap),
      el("section", { class: "panel fill", style: "overflow:hidden" }, rowsBox));
    await update();
  }

  P.route("/errors", {
    title: "Errors", live: false,
    async render(page, { q }) {
      page.append(head("Errors", "Grouped by message pattern, so a storm of similar failures reads as one row"));
      const host = el("div", { class: "col fill", style: "min-height:0" }); page.append(host);
      await errorsBlock(host, { q });
    },
  });

  // ============================================================ BROWSER (RUM)
  // Shown in place of the data when no website is connected yet, and behind "What is this page?" once one is: what the page
  // is for, why it is empty, and the two ways to connect a site (one line in the HTML, or injected at the proxy with no code change).
  function rumGuide(emptyState) {
    const origin = location.origin;
    const tag = `<script src="${origin}/sdk/oscilyx-rum.js"
        data-endpoint="${origin}"
        data-token="YOUR_INGEST_ONLY_TOKEN"
        data-service="my-website"></script>`;
    const oneLine = tag.replace(/\s*\n\s*/g, " ");
    const nginx = `location / {
    proxy_pass http://my-frontend;              # your existing upstream
    proxy_set_header Accept-Encoding "";      # the HTML must reach nginx uncompressed
    sub_filter_once on;
    sub_filter '</head>' '${oneLine}</head>';
}`;
    const code = text => el("div", { class: "codewrap" }, el("pre", { class: "code" }, text),
      el("button", { class: "btn sm", on: { click: () => copyText(text) } }, "Copy"));
    const item = (t, d) => el("li", {}, el("b", {}, t), " ", d);
    return el("div", { class: "rum-guide" },
      emptyState ? el("div", { class: "rum-hero" },
        el("div", { class: "rum-title" }, "No website connected yet"),
        el("p", {}, "This page shows what real visitors experience in their browsers. It fills as soon as a website loads the small Oscilyx browser script. ",
          "Your services, traces, logs and Kubernetes data do not depend on it.")) : null,
      el("div", { class: "rum-cols" },
        el("div", {},
          el("div", { class: "sect" }, "What you will see here"),
          el("ul", { class: "rum-list" },
            item("Page views and JavaScript errors", "for every page your visitors open."),
            item("Load time", "how long pages take to become usable (p50 / p95)."),
            item("Core Web Vitals", "Google's speed scores: LCP (main content shown), CLS (layout jumping), INP (reaction to clicks)."),
            item("Slowest pages", "the URLs with the worst load times, each linked to its logs."))),
        el("div", {},
          el("div", { class: "sect" }, "Before you start"),
          el("ul", { class: "rum-list" },
            item("Use an ingest-only token.", "Anyone can read a web page's source, so never put the admin token there. Set OSCILYX_INGEST_TOKEN on Oscilyx and use that value."),
            item("Reachable address.", "Visitors' browsers must reach " + origin + ". If this address is internal, publish Oscilyx (or just /v1/ and /sdk/) on a public URL and use that instead."),
            item("Content-Security-Policy.", "If the site sets one, allow this address in script-src and connect-src.")))),
      el("div", { class: "sect" }, "Option 1: the developer adds one line to the site's HTML (e.g. index.html, before </head>)"),
      code(tag),
      el("div", { class: "sect" }, "Option 2: no code change. The platform team injects it at the nginx / ingress in front of the site"),
      code(nginx),
      el("p", { class: "muted rum-check" }, "To check: open the website in a browser, click around, then come back here. Page views appear within a few seconds. ",
        "Use the same data-service name to find the site in Services, Traces and Logs."));
  }

  async function browserBlock(host, { q }) {
    const svcs = await P.services(true);
    const st = { service: q.get("service") || "", env: q.get("env") || "" };
    const kpis = el("div", { class: "kgrid", style: "margin-bottom:14px" });
    const slow = el("div", { class: "panel", style: "overflow:hidden" });
    const selSvc = el("select", { on: { change: e => { st.service = e.target.value; update(); } } },
      el("option", { value: "" }, "All sites"), ...svcs.map(s => el("option", { value: s.service, selected: s.service === st.service }, s.service)));

    function badge(val, good, ok) {
      if (val == null) return "muted";
      if (val <= good) return "ok"; if (val <= ok) return "warn"; return "err";
    }
    function ms(v) { return v == null ? "—" : v < 1000 ? v + " ms" : (v / 1000).toFixed(2) + " s"; }
    function fmt(v) { return v == null ? "—" : (typeof v === "number" ? (v < 1 ? v.toFixed(3) : v.toFixed(2)) : v); }

    async function update() {
      setParams({ service: st.service, env: st.env });
      selSvc.value = st.service;
      let r;
      try { r = await api("/rum/summary?" + qs({ minutes: M(), service: st.service, env: st.env })); }
      catch (e) { kpis.replaceChildren(empty("Could not load website data", e.message || "")); slow.replaceChildren(); return; }
      about.style.display = r.views ? "" : "none";   // the guide below already says it all while nothing is connected
      if (!r.views) {
        kpis.replaceChildren(rumGuide(true));
        slow.replaceChildren(); return;
      }
      kpis.replaceChildren(
        kpi("Page views", compact(r.views), "", { href: "#/logs?q=" + encodeURIComponent("event.name=page_view"), spark: undefined }),
        kpi("Errors", compact(r.errors), r.error_rate_pct + "% error rate",
            { cls: r.errors > 0 ? "warn" : "", href: "#/errors?service=" + encodeURIComponent(st.service) }),
        kpi("Load (p95)", ms(r.load_p95), "p50 " + ms(r.load_p50),
            { cls: badge(r.load_p95, 2000, 5000) }),
        kpi("LCP (p75)", ms(r.lcp_p75), "Largest Contentful Paint",
            { cls: badge(r.lcp_p75, 2500, 4000) }),
        kpi("CLS (p75)", fmt(r.cls_p75), "Layout shift — lower is better",
            { cls: badge(r.cls_p75, 0.1, 0.25) }),
        kpi("INP (p75)", ms(r.inp_p75), "Interaction to Next Paint",
            { cls: badge(r.inp_p75, 200, 500) }));
      slow.replaceChildren(
        el("div", { class: "pb", style: "padding:10px 14px;font-weight:500" }, "Slowest pages"),
        el("div", {},
          ...(r.slowest_paths.length ? r.slowest_paths.map(p => el("div", {
            class: "err-row",
            style: "grid-template-columns:1fr 100px 100px 100px",
            on: { click: () => location.hash = "#/logs?q=" + encodeURIComponent('url.path="' + p.path + '"') } },
            el("div", { class: "mono" }, p.path),
            el("div", { class: "muted", style: "text-align:right" }, compact(p.views) + " views"),
            el("div", { style: "text-align:right" }, ms(p.load_p95) + " p95"),
            el("div", { style: "text-align:right" }, ms(p.lcp_p75) + " LCP")))
            : [empty("No navigation data", "")])));
    }
    const about = el("details", { class: "rum-about" }, el("summary", {}, "What is this page (also called Real User Monitoring, RUM), and how do I connect a website?"), rumGuide(false));
    host.append(el("div", { class: "toolbar" }, selSvc, el("span", { class: "sp" })), about, kpis, slow);
    await update();
  }

  P.route("/browser", {
    title: "Website Monitoring", live: false,
    async render(page, { q }) {
      page.append(head("Website Monitoring", "How real visitors experience your website: page speed, errors and Google's Core Web Vitals, measured in their browsers"));
      const host = el("div", { class: "col fill", style: "min-height:0" }); page.append(host);
      await browserBlock(host, { q });
    },
  });

  P.tracesBlock = tracesBlock; P.logsBlock = logsBlock; P.insightRow = insightRow; P.openTrace = openTrace;
})();
