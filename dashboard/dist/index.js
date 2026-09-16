(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  const React = SDK.React;
  const { useEffect, useState } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Button, Badge } = SDK.components;
  const apiBase = "/api/plugins/local-first-orchestrator";

  function configDraft(config) {
    if (!config) return null;
    const decomposition = config.decomposition || {};
    return {
      canonical_repository: config.canonical_repository,
      repository_allowlist: config.repository_allowlist || [],
      implementation_profile: config.implementation.profile,
      review_profile: config.review.profile,
      local_review_profile: config.local_review ? config.local_review.profile : config.implementation.profile,
      decomposition_local_profile: decomposition.local ? decomposition.local.profile : "",
      decomposition_standard_profile: decomposition.standard ? decomposition.standard.profile : "",
      paid_checkpoint_profile: config.paid_checkpoint ? config.paid_checkpoint.profile : "",
      paid_escalation_profile: config.paid_escalation ? config.paid_escalation.profile : "",
      implementation_timeout_seconds: config.implementation_timeout_seconds,
      review_timeout_seconds: config.review_timeout_seconds
    };
  }

  function ProfileSelect(props) {
    return React.createElement("label", { className: "space-y-1" },
      React.createElement("div", { className: "text-xs font-medium" }, props.label),
      React.createElement("select", {
        className: "w-full rounded border bg-background px-2 py-2 text-sm",
        value: props.value || "",
        disabled: props.disabled,
        onChange: function (event) { props.onChange(event.target.value); }
      },
        props.optional ? React.createElement("option", { value: "" }, "Not configured") : null,
        props.profiles.map(function (profile) {
          return React.createElement("option", { key: profile.profile, value: profile.profile }, profile.profile + " — " + profile.model + " (" + profile.provider + ")");
        })
      )
    );
  }

  function LocalFirstPage() {
    const [status, setStatus] = useState(null);
    const [profiles, setProfiles] = useState([]);
    const [draft, setDraft] = useState(null);
    const [error, setError] = useState("");
    const [message, setMessage] = useState("");
    const [busy, setBusy] = useState(false);

    const refresh = function () {
      setError("");
      return Promise.all([
        SDK.fetchJSON(apiBase + "/status"),
        SDK.fetchJSON(apiBase + "/profiles")
      ]).then(function (values) {
        setStatus(values[0]);
        setProfiles(values[1].profiles || []);
        setDraft(configDraft(values[0].configuration));
      }).catch(function (err) {
        setStatus(null);
        setError(String(err.message || err));
      });
    };

    useEffect(function () { refresh(); }, []);

    const act = function (action) {
      setBusy(true); setError(""); setMessage("");
      SDK.fetchJSON(apiBase + "/" + action, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "dashboard operator action" })
      })
        .then(function (next) { setStatus(next); setDraft(configDraft(next.configuration)); })
        .catch(function (err) { setError(String(err.message || err)); })
        .finally(function () { setBusy(false); });
    };

    const saveConfiguration = function () {
      if (!draft) return;
      setBusy(true); setError(""); setMessage("");
      SDK.fetchJSON(apiBase + "/configuration", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(draft)
      })
        .then(function (configuration) {
          setStatus(function (current) { return current ? Object.assign({}, current, { configuration: configuration }) : current; });
          setDraft(configDraft(configuration));
          setMessage("Configuration saved. New scheduler/model work will use the selected Hermes profiles.");
        })
        .catch(function (err) { setError(String(err.message || err)); })
        .finally(function () { setBusy(false); });
    };

    const metric = function (label, value) {
      return React.createElement("div", { className: "rounded border p-3" },
        React.createElement("div", { className: "text-xs text-muted-foreground" }, label),
        React.createElement("div", { className: "text-2xl font-semibold" }, value));
    };

    const updateDraft = function (field, value) {
      setDraft(function (current) { return Object.assign({}, current, { [field]: value }); });
    };

    const config = status && status.configuration;
    return React.createElement("div", { className: "space-y-4 max-w-5xl" },
      React.createElement(Card, null,
        React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Local First operator controls")),
        React.createElement(CardContent, { className: "space-y-4" },
          React.createElement("p", { className: "text-sm text-muted-foreground" }, "This dashboard uses the one operator-registered ledger. Pause before changing model-role configuration or performing manual recovery."),
          error ? React.createElement("p", { className: "text-sm text-destructive" }, error) : null,
          status ? React.createElement(React.Fragment, null,
            React.createElement("div", { className: "flex items-center gap-3" }, React.createElement("span", { className: "text-sm" }, "Admission:"), React.createElement(Badge, null, status.paused ? "Paused" : "Accepting new work")),
            React.createElement("div", { className: "grid grid-cols-2 gap-3 sm:grid-cols-5" }, metric("Ready local", status.ready_local), metric("Running", status.running), metric("Needs triage", status.needs_triage), metric("Done", status.done), metric("Pending outbox", status.outbox_pending)),
            React.createElement("div", { className: "grid grid-cols-1 gap-2 sm:grid-cols-3" },
              React.createElement(Button, { className: "w-full whitespace-nowrap justify-center", onClick: refresh, disabled: busy }, "Refresh"),
              React.createElement(Button, { className: "w-full whitespace-nowrap justify-center", onClick: function () { act("pause"); }, disabled: busy || status.paused }, "Pause new work"),
              React.createElement(Button, { className: "w-full whitespace-nowrap justify-center", onClick: function () { act("resume"); }, disabled: busy || !status.paused }, "Resume new work")),
            React.createElement("div", { className: "space-y-1" }, React.createElement("h3", { className: "text-sm font-medium" }, "Active tickets"), status.active.map(function (item) { return React.createElement("div", { key: item.ticket_id, className: "rounded border p-2 text-xs" }, item.ticket_id + " — " + item.state + " — feature " + (item.feature_id || "-") + " — tranche " + (item.tranche_id || "-")); }), status.active_truncated ? React.createElement("div", { className: "text-xs text-muted-foreground" }, "Showing the first 25 active tickets.") : null)
          ) : null)),
      React.createElement(Card, null,
        React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Runtime metrics & adaptive sizing")),
        React.createElement(CardContent, { className: "space-y-3" },
          status && status.runtime_metrics && status.adaptive_sizing ? React.createElement(React.Fragment, null,
            React.createElement("div", { className: "grid grid-cols-2 gap-3 sm:grid-cols-4" },
              metric("Metric samples", status.runtime_metrics.ticket_count),
              metric("1st-attempt accept", Math.round(status.runtime_metrics.first_attempt_acceptance_rate * 100) + "%"),
              metric("Avg attempts", Number(status.runtime_metrics.average_attempts).toFixed(2)),
              metric("Target context", status.adaptive_sizing.target_context_tokens)
            ),
            React.createElement("div", { className: "text-xs text-muted-foreground" },
              "Planner recommendation: up to " + status.adaptive_sizing.max_active_tickets + " active tickets · " + status.adaptive_sizing.reason + " · sample count " + status.adaptive_sizing.sample_count + ". Context tokens are deterministic estimates, not provider billing telemetry."
            )
          ) : React.createElement("p", { className: "text-sm text-muted-foreground" }, "Runtime metrics unavailable."))),
      React.createElement(Card, null,
        React.createElement(CardHeader, null, React.createElement(CardTitle, null, "Configuration")),
        React.createElement(CardContent, { className: "space-y-4" },
          config && draft ? React.createElement(React.Fragment, null,
            React.createElement("p", { className: "text-sm text-muted-foreground" }, "Choose Hermes profiles for each model-using role. Provider and model are resolved from Hermes when you save, so this page does not duplicate Hermes model configuration."),
            React.createElement("div", { className: "grid gap-3" },
              React.createElement("label", { className: "space-y-1" },
                React.createElement("div", { className: "text-xs font-medium" }, "Canonical repository"),
                React.createElement("input", { className: "w-full rounded border bg-background px-2 py-2 font-mono text-xs", value: draft.canonical_repository || "", disabled: busy, onChange: function (event) { updateDraft("canonical_repository", event.target.value); } })),
              React.createElement("label", { className: "space-y-1" },
                React.createElement("div", { className: "text-xs font-medium" }, "Repository allowlist (one path per line)"),
                React.createElement("textarea", { className: "min-h-24 w-full rounded border bg-background px-2 py-2 font-mono text-xs", value: (draft.repository_allowlist || []).join("\n"), disabled: busy, onChange: function (event) { updateDraft("repository_allowlist", event.target.value.split(/\r?\n/).map(function (value) { return value.trim(); }).filter(Boolean)); } }))),
            React.createElement("div", { className: "grid gap-3 md:grid-cols-2" },
              React.createElement(ProfileSelect, { label: "Local implementation", value: draft.implementation_profile, profiles: profiles, disabled: busy, onChange: function (value) { updateDraft("implementation_profile", value); } }),
              React.createElement(ProfileSelect, { label: "Review", value: draft.review_profile, profiles: profiles, disabled: busy, onChange: function (value) { updateDraft("review_profile", value); } }),
              React.createElement(ProfileSelect, { label: "Local review", value: draft.local_review_profile, profiles: profiles, disabled: busy, onChange: function (value) { updateDraft("local_review_profile", value); } }),
              React.createElement(ProfileSelect, { label: "Decomposition · local", value: draft.decomposition_local_profile, profiles: profiles, optional: true, disabled: busy, onChange: function (value) { updateDraft("decomposition_local_profile", value); } }),
              React.createElement(ProfileSelect, { label: "Decomposition · standard (primary / triage / successor)", value: draft.decomposition_standard_profile, profiles: profiles, optional: true, disabled: busy, onChange: function (value) { updateDraft("decomposition_standard_profile", value); } }),
              React.createElement(ProfileSelect, { label: "Paid checkpoint", value: draft.paid_checkpoint_profile, profiles: profiles, optional: true, disabled: busy, onChange: function (value) { updateDraft("paid_checkpoint_profile", value); } }),
              React.createElement(ProfileSelect, { label: "Paid escalation", value: draft.paid_escalation_profile, profiles: profiles, optional: true, disabled: busy, onChange: function (value) { updateDraft("paid_escalation_profile", value); } }),
              React.createElement("label", { className: "space-y-1" }, React.createElement("div", { className: "text-xs font-medium" }, "Implementation timeout (seconds)"), React.createElement("input", { className: "w-full rounded border bg-background px-2 py-2 text-sm", type: "number", min: 1, max: 86400, value: draft.implementation_timeout_seconds, disabled: busy, onChange: function (event) { updateDraft("implementation_timeout_seconds", Number(event.target.value)); } })),
              React.createElement("label", { className: "space-y-1" }, React.createElement("div", { className: "text-xs font-medium" }, "Review timeout (seconds)"), React.createElement("input", { className: "w-full rounded border bg-background px-2 py-2 text-sm", type: "number", min: 1, max: 86400, value: draft.review_timeout_seconds, disabled: busy, onChange: function (event) { updateDraft("review_timeout_seconds", Number(event.target.value)); } }))
            ),
            !status.paused ? React.createElement("p", { className: "text-xs text-muted-foreground" }, "Pause Local First before saving role changes.") : null,
            React.createElement("div", { className: "flex flex-col items-stretch gap-3 sm:flex-row sm:items-center" }, React.createElement(Button, { className: "w-full whitespace-nowrap justify-center sm:w-auto", onClick: saveConfiguration, disabled: busy || !status.paused || !profiles.length }, busy ? "Saving…" : "Save configuration"), message ? React.createElement("span", { className: "text-xs text-muted-foreground" }, message) : null),
            React.createElement("div", { className: "space-y-1 text-xs text-muted-foreground" },
              React.createElement("div", null, "Worktrees: " + (config.worktree_root || "—")),
              React.createElement("div", null, "Artifacts: " + (config.artifact_root || "—")))
          ) : React.createElement("p", { className: "text-sm text-muted-foreground" }, "Configuration unavailable.")))
    );
  }

  window.__HERMES_PLUGINS__.register("local-first-orchestrator", LocalFirstPage);
})();
