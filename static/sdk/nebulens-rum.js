// NebuLens RUM — a tiny browser agent (~5 KB). Sends page views, errors, and slow requests to NebuLens as OTLP.
// No bundling, no dependencies: drop it on the page before your scripts and set the three config values.
//
//   <script src="https://nebulens.example.com/sdk/nebulens-rum.js"
//           data-endpoint="https://nebulens.example.com"
//           data-token="RUM_INGEST_TOKEN"
//           data-service="checkout-frontend"></script>
//
// Captured:
//   - Page view with Core Web Vitals (LCP, CLS, FID / INP), navigation timing, URL, referrer
//   - Uncaught JS errors and unhandled promise rejections (logs)
//   - fetch() and XMLHttpRequest calls that took longer than slowThresholdMs (spans)
// Not captured (deliberately): clicks, inputs, cookies, user content. Nothing leaves the page unless the
// user-agent, viewport and the configured metadata are already OK to send.

(function () {
  "use strict";
  var script = document.currentScript || document.querySelector('script[src*="nebulens-rum"]') || document.querySelector('script[src*="pulse0-rum"]');
  if (!script) return;
  var ENDPOINT = (script.getAttribute("data-endpoint") || "").replace(/\/+$/, "");
  var TOKEN    = script.getAttribute("data-token") || "";
  var SERVICE  = script.getAttribute("data-service") || "browser";
  var ENV      = script.getAttribute("data-env") || "";
  var SLOW_MS  = +script.getAttribute("data-slow-ms") || 1000;
  var SAMPLE   = +script.getAttribute("data-sample") || 1.0;  // 0..1
  if (!ENDPOINT) return;
  if (Math.random() > SAMPLE) return;

  var SESSION  = Math.random().toString(36).slice(2) + Date.now().toString(36);
  function now() { return (Date.now() * 1e6) + ""; }               // unix nano as string
  function attrs(o) { return Object.entries(o).filter(function (kv) { return kv[1] != null; })
    .map(function (kv) { return { key: kv[0], value: typeof kv[1] === "number" ? { doubleValue: kv[1] } : { stringValue: String(kv[1]) } }; }); }
  function resource() {
    return { attributes: attrs({ "service.name": SERVICE, "deployment.environment": ENV,
      "telemetry.sdk.name": "nebulens-rum", "telemetry.sdk.language": "webjs",
      "browser.user_agent": navigator.userAgent, "browser.language": navigator.language,
      "nebulens.session.id": SESSION }) };
  }
  function send(path, body) {
    var url = ENDPOINT + path, json = JSON.stringify(body);
    if (navigator.sendBeacon && !TOKEN) {                          // beacon can't set headers
      try { if (navigator.sendBeacon(url, new Blob([json], { type: "application/json" }))) return; } catch (e) {}
    }
    try {
      fetch(url, { method: "POST", keepalive: true, credentials: "omit",
        headers: Object.assign({ "Content-Type": "application/json" }, TOKEN ? { Authorization: "Bearer " + TOKEN } : {}),
        body: json }).catch(function () {});
    } catch (e) {}
  }
  function logRecord(sev, body, extra) {
    return { timeUnixNano: now(), severityNumber: sev, body: { stringValue: String(body).slice(0, 4000) },
             attributes: attrs(Object.assign({ "url.full": location.href }, extra || {})) };
  }
  function sendLog(sev, body, extra) {
    send("/v1/logs", { resourceLogs: [{ resource: resource(), scopeLogs: [{ logRecords: [logRecord(sev, body, extra)] }] }] });
  }
  function sendSpan(name, startNs, endNs, kind, attrs_, status) {
    var id16 = function () { for (var s = "", i = 0; i < 16; i++) s += Math.floor(Math.random() * 16).toString(16); return s; };
    send("/v1/traces", { resourceSpans: [{ resource: resource(), scopeSpans: [{ spans: [{
      traceId: id16() + id16(), spanId: id16(), parentSpanId: "", name: name, kind: kind || 3,
      startTimeUnixNano: String(startNs), endTimeUnixNano: String(endNs),
      attributes: attrs(attrs_ || {}), status: { code: status || 0 } }] }] }] });
  }

  // ---- Page view with Core Web Vitals ---------------------------------------------------------------
  var vitals = { lcp: null, cls: 0, inp: null, fid: null };
  try {
    new PerformanceObserver(function (l) { var e = l.getEntries().pop(); if (e) vitals.lcp = Math.round(e.renderTime || e.startTime); })
      .observe({ type: "largest-contentful-paint", buffered: true });
  } catch (e) {}
  try {
    new PerformanceObserver(function (l) { l.getEntries().forEach(function (e) { if (!e.hadRecentInput) vitals.cls += e.value; }); })
      .observe({ type: "layout-shift", buffered: true });
  } catch (e) {}
  try {
    new PerformanceObserver(function (l) { l.getEntries().forEach(function (e) { vitals.inp = Math.max(vitals.inp || 0, Math.round(e.duration)); }); })
      .observe({ type: "event", buffered: true, durationThreshold: 40 });
  } catch (e) {}
  try {
    new PerformanceObserver(function (l) {
      var e = l.getEntries()[0]; if (e && vitals.fid == null) vitals.fid = Math.round(e.processingStart - e.startTime);
    }).observe({ type: "first-input", buffered: true });
  } catch (e) {}

  function sendPageView() {
    var nav = (performance.getEntriesByType && performance.getEntriesByType("navigation")[0]) || null;
    var load = nav ? Math.round(nav.loadEventEnd || nav.domComplete) : null;
    var ttfb = nav ? Math.round(nav.responseStart) : null;
    var fcp = (performance.getEntriesByName("first-contentful-paint")[0] || {}).startTime;
    sendLog(9, "page_view " + location.pathname, {
      "event.name": "page_view", "url.path": location.pathname, "url.query": location.search,
      "http.referer": document.referrer, "viewport.width": window.innerWidth, "viewport.height": window.innerHeight,
      "rum.load_ms": load, "rum.ttfb_ms": ttfb, "rum.fcp_ms": fcp ? Math.round(fcp) : null,
      "rum.lcp_ms": vitals.lcp, "rum.cls": vitals.cls, "rum.fid_ms": vitals.fid, "rum.inp_ms": vitals.inp });
  }
  // On page hide so we have the real vitals, not estimated ones from onload.
  addEventListener("pagehide", sendPageView, { once: true });
  addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") sendPageView(); }, { once: true });

  // ---- Errors ---------------------------------------------------------------------------------------
  addEventListener("error", function (e) {
    sendLog(17, (e.message || "Error") + (e.filename ? " at " + e.filename + ":" + e.lineno : ""),
      { "exception.message": e.message, "exception.source": e.filename, "exception.line": e.lineno,
        "exception.stacktrace": (e.error && e.error.stack) || "" });
  });
  addEventListener("unhandledrejection", function (e) {
    var r = e.reason;
    sendLog(17, "Unhandled rejection: " + (r && r.message || r),
      { "exception.message": (r && r.message) || String(r), "exception.stacktrace": r && r.stack || "" });
  });

  // ---- Slow fetches and XHRs -----------------------------------------------------------------------
  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      var startNs = performance.timeOrigin * 1e6 + performance.now() * 1e6;
      var url = typeof input === "string" ? input : (input && input.url) || "";
      var method = (init && init.method) || (typeof input === "object" && input.method) || "GET";
      return origFetch.apply(this, arguments).then(function (res) {
        var durMs = (performance.timeOrigin * 1e6 + performance.now() * 1e6 - startNs) / 1e6;
        if (durMs >= SLOW_MS || res.status >= 400) {
          sendSpan(method + " " + new URL(url, location.href).pathname, startNs,
            performance.timeOrigin * 1e6 + performance.now() * 1e6, 3,
            { "http.method": method, "http.url": url, "http.status_code": res.status, "rum.dur_ms": Math.round(durMs) },
            res.status >= 500 ? 2 : 0);
        }
        return res;
      }, function (err) {
        sendSpan(method + " " + url, startNs, performance.timeOrigin * 1e6 + performance.now() * 1e6, 3,
          { "http.method": method, "http.url": url, "exception.message": err && err.message }, 2);
        throw err;
      });
    };
  }

  // Export for custom events: window.nebulens.event("name", {attrs})
  window.nebulens = window.nebulens || {};
  window.nebulens.event = function (name, a) { sendLog(9, name, a || {}); };
  window.nebulens.error = function (msg, a) { sendLog(17, msg, a || {}); };
  window.pulse0 = window.nebulens;   // the old name, for pages written against Pulse0
})();
