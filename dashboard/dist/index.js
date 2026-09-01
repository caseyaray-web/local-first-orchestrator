(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  const React = SDK.React;
  const { useEffect, useState } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Button, Input, Label, Badge } = SDK.components;
  const apiBase = "/api/plugins/local-first-orchestrator";

  function LocalFirstPage() {
    const [database, setDatabase] = useState(function () { return window.localStorage.getItem("local-first-orchestrator.database") || ""; });
    const [status, setStatus] = useState(null);
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);

    const query = function () { return "?database=" + encodeURIComponent(database); };
    const refresh = function () {
      if (!database) { setStatus(null); setError("Enter the separate ledger database path."); return; }
      setError("");
      SDK.fetchJSON(apiBase + "/status" + query()).then(setStatus).catch(function (err) { setStatus(null); setError(String(err.message || err)); });
    };
    useEffect(function () { if (database) refresh(); }, []);
    const act = function (action) {
      if (!database) { setError("Enter the separate ledger database path."); return; }
      setBusy(true); setError("");
      SDK.fetchJSON(apiBase + "/" + action + query(), { method: "POST", body: { reason: "dashboard operator action" } })
        .then(setStatus).catch(function (err) { setError(String(err.message || err)); }).finally(function () { setBusy(false); });
    };
    const saveDatabase = function (value) { setDatabase(value); window.localStorage.setItem("local-first-orchestrator.database", value); };
    const metric = function (label, value) { return React.createElement("div", { className: "rounded border p-3" }, React.createElement("div", { className: "text-xs text-muted-foreground" }, label), React.createElement("div", { className: "text-2xl font-semibold" }, value)); };

    return React.createElement(Card, { className: "max-w-3xl" },
      React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Local First operator controls")),
      React.createElement(CardContent, { className: "space-y-4" },
        React.createElement("p", { className: "text-sm text-muted-foreground" }, "Pause blocks new local admissions only; already running attempts are unaffected."),
        React.createElement("div", { className: "space-y-2" },
          React.createElement(Label, { htmlFor: "local-first-ledger" }, "Separate ledger database"),
          React.createElement("div", { className: "flex gap-2" },
            React.createElement(Input, { id: "local-first-ledger", value: database, placeholder: "/path/to/local-first-ledger.db", onChange: function (event) { saveDatabase(event.target.value); } }),
            React.createElement(Button, { onClick: refresh, disabled: busy }, "Refresh"))),
        error ? React.createElement("p", { className: "text-sm text-destructive" }, error) : null,
        status ? React.createElement(React.Fragment, null,
          React.createElement("div", { className: "flex items-center gap-3" }, React.createElement("span", { className: "text-sm" }, "Admission:"), React.createElement(Badge, null, status.paused ? "Paused" : "Accepting new work")),
          React.createElement("div", { className: "grid grid-cols-1 gap-3 sm:grid-cols-3" }, metric("Ready local", status.ready_local), metric("Running", status.running), metric("Pending outbox", status.outbox_pending)),
          React.createElement("div", { className: "flex gap-2" },
            React.createElement(Button, { onClick: function () { act("pause"); }, disabled: busy || status.paused }, "Pause new work"),
            React.createElement(Button, { onClick: function () { act("resume"); }, disabled: busy || !status.paused }, "Resume new work"))) : null));
  }
  window.__HERMES_PLUGINS__.register("local-first-orchestrator", LocalFirstPage);
})();
