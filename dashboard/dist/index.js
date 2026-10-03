(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  const React = SDK.React;
  const { useEffect, useState } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Button, Badge } = SDK.components;
  const apiBase = "/api/plugins/local-first-orchestrator";

  function text(value) { return value === undefined || value === null ? "—" : String(value); }
  function json(value) { return JSON.stringify(value || [], null, 2); }

  function LocalFirstPage() {
    const [status, setStatus] = useState(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const [result, setResult] = useState(null);
    const [profiles, setProfiles] = useState({});
    const [configDraft, setConfigDraft] = useState(null);

    const refresh = function () {
      setBusy(true); setError("");
      return SDK.fetchJSON(apiBase + "/status")
        .then(function (next) { setStatus(next); setConfigDraft(next.configuration || null); return SDK.fetchJSON(apiBase + "/profiles"); })
        .then(function (next) { setProfiles(next.profiles || {}); })
        .catch(function (err) { setStatus(null); setError(String(err.message || err)); })
        .finally(function () { setBusy(false); });
    };

    useEffect(function () { refresh(); }, []);

    const act = function (action, authorizedClear) {
      if (!status || !status.observation_digest) return;
      setBusy(true); setError(""); setResult(null);
      const payload = { expected_observation_digest: status.observation_digest };
      if (authorizedClear) payload.authorized_clear = true;
      SDK.fetchJSON(apiBase + (action === "enroll" ? "/enroll" : "/actions/" + action), {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
      }).then(function (next) {
        setResult(next.result || next);
        if (next.status) setStatus(next.status);
        else return refresh();
      }).catch(function (err) {
        const detail = err && (err.message || err);
        return refresh().then(function () { setError(String(detail)); });
      }).finally(function () { setBusy(false); });
    };

    const saveConfiguration = function () {
      if (!configDraft || !status) return;
      const budgets = {};
      const intervalText = String(configDraft.poll_interval_seconds).trim();
      if (!intervalText) { setError("Poll interval is required (1–3600 seconds)."); return; }
      const interval = Number(intervalText);
      if (!Number.isFinite(interval) || !Number.isInteger(interval) || interval < 1 || interval > 3600) {
        setError("Poll interval must be a whole number from 1 to 3600 seconds."); return;
      }
      for (const name of Object.keys(configDraft.budgets || {})) {
        const raw = String(configDraft.budgets[name]).trim();
        if (!raw) { setError(name.replace(/_/g, " ") + " budget is required."); return; }
        const value = Number(raw);
        if (!Number.isFinite(value) || !Number.isInteger(value) || value < 0) {
          setError(name.replace(/_/g, " ") + " budget must be a non-negative whole number."); return;
        }
        budgets[name] = value;
      }
      setBusy(true); setError("");
      const payload = { expected_configuration_digest: status.configuration.configuration_digest,
        poll_interval_seconds: interval, budgets: budgets };
      SDK.fetchJSON(apiBase + "/configuration", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
        .then(function (next) { setStatus(next.status); setConfigDraft(next.configuration); setResult(next.configuration); })
        .catch(function (err) { return refresh().then(function () { setError(String(err.message || err)); }); })
        .finally(function () { setBusy(false); });
    };

    const disabled = busy || !status || !status.observation_digest;
    const metric = function (label, value) {
      return React.createElement("div", { className: "rounded border p-3" },
        React.createElement("div", { className: "text-xs text-muted-foreground" }, label),
        React.createElement("div", { className: "text-sm font-medium break-all" }, text(value)));
    };
    const button = function (label, action, extra) {
      return React.createElement(Button, { className: "w-full justify-center", disabled: disabled, onClick: function () { act(action, extra); } }, label);
    };
    const list = function (title, values, render) {
      return React.createElement("div", { className: "space-y-2" },
        React.createElement("h3", { className: "text-sm font-medium" }, title),
        !values || !values.length ? React.createElement("p", { className: "text-xs text-muted-foreground" }, "None.") : values.map(render));
    };
    const updateBudget = function (name, value) {
      setConfigDraft(function (current) {
        return Object.assign({}, current, { budgets: Object.assign({}, current.budgets, { [name]: value }) });
      });
    };
    const budgetUsage = function (usage) {
      const rows = usage && usage.by_finding || [];
      const aggregate = usage && usage.aggregate;
      return React.createElement("div", { className: "space-y-2" },
        React.createElement("h3", { className: "text-sm font-medium" }, "Budget usage by finding"),
        !rows.length ? React.createElement("p", { className: "text-xs text-muted-foreground" }, "No budget ledger rows are recorded for this scope.") : rows.map(function (row) {
          return React.createElement("div", { key: row.root_task_id + ":" + row.finding_id + ":" + row.category, className: "rounded border p-2 text-xs" },
            row.root_task_id + " / " + row.finding_id + " / " + row.category + ": net " + row.consumed_net +
            " of configured limit " + row.configured_limit + "; remaining " + row.remaining);
        }),
        aggregate ? React.createElement("p", { className: "text-xs text-muted-foreground" },
          "Total net consumption: " + aggregate.consumed_net + ". " + aggregate.semantics) : null);
    };

    return React.createElement("div", { className: "space-y-4 max-w-6xl" },
      React.createElement(Card, null,
        React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Local First")),
        React.createElement(CardContent, { className: "space-y-4" },
          React.createElement("p", { className: "text-sm text-muted-foreground" }, "This view is scoped by trusted local bootstrap. Browser requests cannot select paths, profiles, board IDs, or executable settings."),
          error ? React.createElement("p", { className: "text-sm text-destructive" }, error) : null,
          status ? React.createElement(React.Fragment, null,
            React.createElement("div", { className: "flex items-center gap-3" },
              React.createElement(Badge, null, status.operator_intent && status.operator_intent.active ? "Operator pause active" : "No active operator pause"),
              React.createElement("span", { className: "text-xs text-muted-foreground" }, "Observation " + status.observation_digest.slice(0, 19) + "…")),
            React.createElement("div", { className: "grid gap-3 sm:grid-cols-3" }, metric("Managed anchors", status.managed_anchors.length), metric("Active workers", status.active_workers.length), metric("Uncontained workers", status.uncontained_workers.length)),
            React.createElement("div", { className: "grid gap-2 sm:grid-cols-3" },
              React.createElement(Button, { className: "w-full justify-center", disabled: busy, onClick: refresh }, "Refresh observation"),
              button("Enroll configured anchor", "enroll"),
              button("Pause", "pause"), button("Pause and stop", "stop"), button("Reconcile", "reconcile"),
              button("Resume (explicit)", "resume", true), button("Cancel managed scope", "cancel"), button("Run bounded recovery", "recover")),
            React.createElement("p", { className: "text-xs text-muted-foreground" }, "Controls are disabled until a fresh observation exists. A stale response is rejected by the backend and this page refreshes after every action."),
            result ? React.createElement("pre", { className: "max-h-56 overflow-auto rounded border p-2 text-xs" }, json(result)) : null
          ) : null)),
      status && configDraft ? React.createElement(Card, null,
        React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Configured scope and bounded settings")),
        React.createElement(CardContent, { className: "space-y-3" },
          React.createElement("p", { className: "text-xs text-muted-foreground" }, "Scope and profiles are trusted bootstrap selections. Roots, executables, board identity, and profile assignments cannot be changed in this browser."),
          React.createElement("label", { className: "text-sm" }, "Configured scope", React.createElement("select", { disabled: true, value: status.scope.board_id + ":" + status.scope.anchor_task_id, className: "block w-full rounded border p-2" }, React.createElement("option", { value: status.scope.board_id + ":" + status.scope.anchor_task_id }, status.scope.board_id + " / " + status.scope.anchor_task_id))),
          React.createElement("pre", { className: "rounded border p-2 text-xs overflow-auto" }, json(profiles)),
          React.createElement("label", { className: "text-sm" }, "Poll interval (seconds)", React.createElement("input", { type: "number", min: 1, max: 3600, value: configDraft.poll_interval_seconds, onChange: function (event) { setConfigDraft(Object.assign({}, configDraft, { poll_interval_seconds: event.target.value })); }, className: "block w-full rounded border p-2" })),
          React.createElement("div", { className: "grid gap-3 sm:grid-cols-2" }, Object.keys(configDraft.budgets || {}).sort().map(function (name) {
            return React.createElement("label", { key: name, className: "text-sm" }, name.replace(/_/g, " ") + " budget",
              React.createElement("input", { type: "number", min: 0, step: 1, value: configDraft.budgets[name], disabled: busy,
                onChange: function (event) { updateBudget(name, event.target.value); }, className: "block w-full rounded border p-2" }));
          })),
          React.createElement("p", { className: "text-xs text-muted-foreground" }, "Budget limits may only be tightened; the server rejects increases and stale saves."),
          React.createElement(Button, { disabled: busy, onClick: saveConfiguration }, "Save bounded configuration"))) : null,
      status && status.runtime_metrics ? React.createElement(Card, null,
        React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Runtime metrics")),
        React.createElement(CardContent, { className: "space-y-3" },
          React.createElement("p", { className: "text-xs text-muted-foreground" }, "Metrics are read from the current scoped evidence/native-board observation. Worker liveness and loop heartbeat are unknown unless durable evidence exists."),
          React.createElement("div", { className: "grid gap-3 sm:grid-cols-4" },
            metric("Observed native runs", status.runtime_metrics.native_runs.observed_total),
            metric("Observed active run lanes", status.runtime_metrics.native_runs.observed_active),
            metric("Applied operations", status.runtime_metrics.operations.applied),
            metric("Pending / unknown operations", status.runtime_metrics.operations.pending + " / " + status.runtime_metrics.operations.unknown)),
          React.createElement("div", { className: "grid gap-3 sm:grid-cols-3" },
            metric("Recorded reviews", status.runtime_metrics.reviews.total),
            metric("Local / paid reviews", status.runtime_metrics.reviews.local + " / " + status.runtime_metrics.reviews.paid),
            metric("Coordinator heartbeat", status.runtime_metrics.loop.last_heartbeat || "Unknown")),
          budgetUsage(status.runtime_metrics.budget_usage))) : null,
      status ? React.createElement(React.Fragment, null,
        React.createElement(Card, null, React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Managed anchors, head, and workers")),
          React.createElement(CardContent, { className: "space-y-3" },
            list("Managed anchors", status.managed_anchors, function (item) { return React.createElement("pre", { key: item.task_id, className: "rounded border p-2 text-xs overflow-auto" }, json(item)); }),
            React.createElement("div", { className: "space-y-1" }, React.createElement("h3", { className: "text-sm font-medium" }, "Current head"), React.createElement("pre", { className: "rounded border p-2 text-xs overflow-auto" }, json(status.current_head))),
            list("Active / uncontained workers", status.active_workers, function (item, index) { return React.createElement("pre", { key: item.id || index, className: "rounded border p-2 text-xs overflow-auto" }, json(item)); }))),
        React.createElement(Card, null, React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Reviews, repairs, budgets, and preserved work")),
          React.createElement(CardContent, { className: "space-y-3" },
            list("Local review queue", status.review_queues.local, function (item, index) { return React.createElement("pre", { key: item.review_id || index, className: "rounded border p-2 text-xs overflow-auto" }, json(item)); }),
            list("Paid review queue", status.review_queues.paid, function (item, index) { return React.createElement("pre", { key: item.review_id || index, className: "rounded border p-2 text-xs overflow-auto" }, json(item)); }),
            React.createElement("div", { className: "space-y-1" }, React.createElement("h3", { className: "text-sm font-medium" }, "Repair history"), React.createElement("pre", { className: "max-h-56 overflow-auto rounded border p-2 text-xs" }, json(status.repair_history))),
            React.createElement("div", { className: "space-y-1" }, React.createElement("h3", { className: "text-sm font-medium" }, "Budgets"), React.createElement("pre", { className: "max-h-40 overflow-auto rounded border p-2 text-xs" }, json(status.budgets))),
            React.createElement("div", { className: "space-y-1" }, React.createElement("h3", { className: "text-sm font-medium" }, "Preserved work paths"), React.createElement("pre", { className: "max-h-40 overflow-auto rounded border p-2 text-xs" }, json(status.preserved_paths)))))
      ) : null
    );
  }
  window.__HERMES_PLUGINS__.register("local-first-orchestrator", LocalFirstPage);
})();
