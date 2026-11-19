/* Oscilyx multi-cluster console: register any number of Kubernetes clusters, give each its own ingest token, see which
   are sending, and get the install command for each. Loaded after core.js/views*.js. Admin only (the server enforces it). */
(function () {
  const P = window.Oscilyx;
  const { el, icon, head, panel, empty, codeBlock, field, modal, confirmBox, toast, pill, api, dataTable, ago, copyText } = P;

  const NAME_RE = /[a-z0-9]/i;
  const slugOf = n => String(n || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);

  // The commands to run in the NEW cluster. `token` is only known right after creating or rotating; otherwise a placeholder.
  function installText(slug, endpoint, token) {
    const tok = token || "<CLUSTER TOKEN>";
    return [
      "# 1. In the cluster you are connecting:",
      "kubectl create namespace observability",
      `kubectl -n observability create secret generic oscilyx-cluster-token --from-literal=token=${tok}`,
      "",
      "# 2. Install the agent (collector only, no server) from the Oscilyx chart:",
      "helm upgrade --install oscilyx-agent ./helm/oscilyx -n observability \\",
      "  -f helm/oscilyx/values-agent.yaml \\",
      `  --set collector.clusterName=${slug} \\`,
      `  --set collector.remote.endpoint=${endpoint || "<CONSOLE ENDPOINT>"}`,
      "",
      "# 3. Check that it is running:",
      "kubectl -n observability get pods",
    ].join("\n");
  }

  // Modal that shows the install commands; with a token it is the one-time reveal.
  function showInstall({ slug, name, token, title }) {
    const endpoint = el("input", { type: "text", value: location.origin, style: "width:100%" });
    const out = el("div");
    const paint = () => out.replaceChildren(codeBlock(installText(slug, endpoint.value.trim().replace(/\/+$/, ""), token)));
    endpoint.addEventListener("input", paint); paint();
    const body = el("div", { class: "col", style: "gap:12px" },
      token ? el("div", { class: "tn-note warn" }, "Copy the token now. Oscilyx stores only a hash of it and cannot show it again. If you lose it, use Rotate token.") : null,
      token ? field("Cluster token", el("div", { style: "display:flex;gap:6px" },
        el("input", { type: "text", readonly: true, value: token, style: "flex:1;font-family:var(--mono);font-size:12px", on: { focus: e => e.target.select() } }),
        el("button", { class: "btn sm", on: { click: () => copyText(token) } }, icon("copy", 13), "Copy"))) : null,
      field("Console address the cluster will use", endpoint,
        "Must be reachable from the new cluster: this console's NodePort or ingress address, not localhost. Change it if the address in your browser is different from the one the cluster can reach."),
      out,
      el("div", { class: "muted", style: "font-size:12px" },
        `Telemetry sent with this token is always stored under the cluster “${name}” (${slug}); the cluster cannot choose another name.`));
    modal({ title: title || `Connect ${name}`, body, width: 720, actions: [{ label: "Done", primary: true }] });
  }

  function addCluster() {
    const f = { name: el("input", { type: "text", placeholder: "e.g. plant-2-production" }), note: el("input", { type: "text", placeholder: "Optional: where it runs, who owns it" }) };
    modal({
      title: "Add cluster",
      body: el("div", { class: "form" },
        field("Cluster name", f.name, "Shown in the console; the id is derived from it (letters, digits and dashes).", "wide"),
        field("Note", f.note, null, "wide")),
      actions: [{ label: "Cancel" }, { label: "Create", primary: true, onClick: async () => {
        const name = f.name.value.trim();
        if (!name || !NAME_RE.test(name)) { toast("Give the cluster a name with letters or digits", "err"); return false; }
        const r = await api("/clusters", { method: "POST", body: { name, note: f.note.value.trim() } });
        P.render();
        setTimeout(() => showInstall({ slug: r.slug, name: r.name, token: r.token, title: `Cluster ${r.name} created` }), 50);
      } }],
    });
  }

  async function rotate(c) {
    if (!(await confirmBox(`Issue a new token for ${c.name}? Its current token stops working within seconds, so update the agent in that cluster straight away.`, "Rotate token"))) return;
    try {
      const r = await api(`/clusters/${encodeURIComponent(c.slug)}/rotate`, { method: "POST" });
      P.render();
      setTimeout(() => showInstall({ slug: c.slug, name: c.name, token: r.token, title: `New token for ${c.name}` }), 50);
    } catch (e) { toast(e.message, "err"); }
  }

  async function toggle(c) {
    try { await api(`/clusters/${encodeURIComponent(c.slug)}`, { method: "PATCH", body: { disabled: !c.disabled } }); toast(c.disabled ? "Cluster enabled" : "Cluster disabled", "ok"); P.render(); }
    catch (e) { toast(e.message, "err"); }
  }

  async function remove(c) {
    if (!(await confirmBox(`Remove ${c.name}? Its token stops working and the cluster can no longer send data. Telemetry already stored stays in this console.`, "Remove cluster"))) return;
    try { await api(`/clusters/${encodeURIComponent(c.slug)}`, { method: "DELETE" }); toast("Cluster removed", "ok"); P.render(); }
    catch (e) { toast(e.message, "err"); }
  }

  const statusPill = c => c.status === "live" ? pill("live", "ok")
    : c.status === "disabled" ? pill("disabled", "err")
    : c.status === "waiting" ? pill("waiting for data", "warn", "No telemetry received yet. Run the install commands in the cluster.")
    : pill("idle", "", "Nothing received in the last 5 minutes");

  P.route("/clusters", {
    title: "Clusters", live: false, range: false,
    async render(page) {
      const addBtn = el("button", { class: "btn primary", on: { click: addCluster } }, icon("plus", 14), "Add cluster");
      page.append(head("Clusters", "Connect any number of Kubernetes clusters to this one console. Each cluster has its own token.", addBtn));
      let data;
      try { data = await api("/clusters"); }
      catch (e) {
        page.append(el("section", { class: "panel fill" }, empty("Clusters are not available here", /forbidden|admin/i.test(e.message) ? "Only administrators can manage clusters." : e.message)));
        return;
      }
      if (!data.ingest_auth_required) {
        page.append(el("div", { class: "tn-note warn", style: "margin-bottom:12px" },
          "This console accepts telemetry without any token. Cluster tokens still decide which cluster each record belongs to, but anyone who can reach this address can also send data without one. " +
          "Set auth.ingestToken in the Helm values (OSCILYX_INGEST_TOKEN) to refuse unauthenticated senders. The collector in this cluster then needs auth.token."));
      }
      if (data.this_cluster) {
        page.append(el("div", { class: "muted", style: "font-size:12.5px;margin-bottom:10px" },
          `This console runs in the cluster “${data.this_cluster}”. Its own collector sends with the console's global token and does not need to be added here.`));
      }
      const rows = data.clusters;
      page.append(el("section", { class: "panel fill" }, dataTable({
        keep: "clusters", rows, sortKey: "name", sortDir: 1,
        cols: [
          { key: "name", label: "Cluster", render: c => el("div", {}, el("div", { style: "font-weight:500" }, c.name),
              el("div", { class: "muted", style: "font-size:11.5px" }, c.slug + (c.note ? " · " + c.note : ""))) },
          { key: "status", label: "Status", sort: c => c.status, render: statusPill },
          { key: "last_seen", label: "Last data", sort: c => c.last_seen || 0, render: c => c.last_seen ? ago(c.last_seen * 1000) : "—" },
          { key: "logs_1h", label: "Logs (1h)", num: true, render: c => P.compact(c.logs_1h) },
          { key: "spans_1h", label: "Spans (1h)", num: true, render: c => P.compact(c.spans_1h) },
          { key: "token_prefix", label: "Token", sort: false, render: c => el("span", { style: "font-family:var(--mono);font-size:12px" }, c.token_prefix ? c.token_prefix + "…" : "—") },
          { key: "id", label: "", sort: false, num: true, render: c => el("span", { style: "display:inline-flex;gap:6px;flex-wrap:wrap;justify-content:flex-end" },
              el("button", { class: "btn sm", on: { click: () => P.nav("#/logs?env=" + encodeURIComponent(c.slug)) } }, "Open"),
              el("button", { class: "btn sm", on: { click: () => showInstall({ slug: c.slug, name: c.name }) } }, "Install"),
              el("button", { class: "btn sm", on: { click: () => rotate(c) } }, "Rotate token"),
              el("button", { class: "btn sm", on: { click: () => toggle(c) } }, c.disabled ? "Enable" : "Disable"),
              el("button", { class: "btn sm", on: { click: () => remove(c) } }, "Remove")) },
        ],
        empty: empty("No clusters added yet", "Add a cluster to get its token and the commands that connect it to this console.", el("button", { class: "btn primary", on: { click: addCluster } }, "Add cluster")),
      })));
      page.append(el("div", { class: "muted", style: "font-size:12.5px;margin-top:12px;line-height:1.6" },
        "Once a cluster is live, filter any page by it with the environment filter (Environments lists each cluster), or use the cluster selector on the Kubernetes page. ",
        "Each token belongs to one cluster, so you can revoke one cluster without touching the others."));
    },
  });
})();
