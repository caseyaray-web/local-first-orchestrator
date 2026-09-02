(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  const React = SDK.React;
  const { useEffect, useState } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Button, Badge } = SDK.components;
  const apiBase = "/api/plugins/local-first-orchestrator";

  function LocalFirstPage() {
    const [status, setStatus] = useState(null);
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);
    const refresh = function () {
      setError("");
      SDK.fetchJSON(apiBase + "/status").then(setStatus).catch(function (err) { setStatus(null); setError(String(err.message || err)); });
    };
    useEffect(function () { refresh(); }, []);
    const act = function (action) {
      setBusy(true); setError("");
      SDK.fetchJSON(apiBase + "/" + action, { method: "POST", body: { reason: "dashboard operator action" } })
        .then(setStatus).catch(function (err) { setError(String(err.message || err)); }).finally(function () { setBusy(false); });
    };
    const metric = function (label, value) { return React.createElement("div", { className: "rounded border p-3" }, React.createElement("div", { className: "text-xs text-muted-foreground" }, label), React.createElement("div", { className: "text-2xl font-semibold" }, value)); };
    const config = status && status.configuration;
    return React.createElement(Card, { className: "max-w-4xl" },
      React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Local First operator controls")),
      React.createElement(CardContent, { className: "space-y-4" },
        React.createElement("p", { className: "text-sm text-muted-foreground" }, "This dashboard uses the one operator-registered ledger. Pause blocks new local admissions only; already running attempts are unaffected."),
        error ? React.createElement("p", { className: "text-sm text-destructive" }, error) : null,
        status ? React.createElement(React.Fragment, null,
          React.createElement("div", { className: "flex items-center gap-3" }, React.createElement("span", { className: "text-sm" }, "Admission:"), React.createElement(Badge, null, status.paused ? "Paused" : "Accepting new work")),
          React.createElement("div", { className: "grid grid-cols-2 gap-3 sm:grid-cols-5" }, metric("Ready local", status.ready_local), metric("Running", status.running), metric("Needs triage", status.needs_triage), metric("Done", status.done), metric("Pending outbox", status.outbox_pending)),
          React.createElement("div", { className: "flex gap-2" }, React.createElement(Button, { onClick: refresh, disabled: busy }, "Refresh"), React.createElement(Button, { onClick: function () { act("pause"); }, disabled: busy || status.paused }, "Pause new work"), React.createElement(Button, { onClick: function () { act("resume"); }, disabled: busy || !status.paused }, "Resume new work")),
          config ? React.createElement("div", { className: "space-y-1 text-xs text-muted-foreground" }, React.createElement("div", null, "Canonical repo: " + config.canonical_repository), React.createElement("div", null, "Allowlist: " + config.repository_allowlist.join(", ")), React.createElement("div", null, "Implementation: " + config.implementation.profile + " / " + config.implementation.provider + " / " + config.implementation.model), React.createElement("div", null, "Review: " + config.review.profile + " / " + config.review.provider + " / " + config.review.model)) : null,
          React.createElement("div", { className: "space-y-1" }, React.createElement("h3", { className: "text-sm font-medium" }, "Active tickets"), status.active.map(function (item) { return React.createElement("div", { key: item.ticket_id, className: "rounded border p-2 text-xs" }, item.ticket_id + " — " + item.state + " — feature " + (item.feature_id || "-") + " — tranche " + (item.tranche_id || "-")); }), status.active_truncated ? React.createElement("div", { className: "text-xs text-muted-foreground" }, "Showing the first 25 active tickets.") : null)) : null));
  }
  window.__HERMES_PLUGINS__.register("local-first-orchestrator", LocalFirstPage);
})();
