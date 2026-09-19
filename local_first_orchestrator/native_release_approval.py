"""Detached operator approval documents for native release revalidation.

This module has no signing capability. The plugin only verifies signatures made
outside the plugin by an operator-controlled key.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

APPROVAL_DOMAIN = "native-release-revalidation"
ACTIVATION_DOMAIN = "native-release-activation"
APPROVAL_VERSION = 1
_REQUIRED = {"domain", "version", "operation", "request_id", "nonce", "operator_id", "reason", "authority"}


def _history(root: dict[str, Any], canonical: str, legacy: str) -> Any:
    """Read the CLI spelling while retaining the raw root for authority."""
    return root.get(canonical) if canonical in root else root.get(legacy)


def snapshot_authority(snapshot: Any) -> dict[str, Any]:
    """Return the one canonical Hermes ``show --json`` root.

    Typed execution fields are deliberately not a fallback: a projection cannot
    be used as signed or hashed authority.
    """
    if isinstance(snapshot, dict):
        root = snapshot
    else:
        root = getattr(snapshot, "raw_snapshot", None)
    required = {"task", "latest_summary", "parents", "children", "comments", "events", "runs"}
    if not isinstance(root, dict) or not required.issubset(root) or not isinstance(root.get("task"), dict):
        raise ValueError("Hermes execution snapshot raw authority is unavailable")
    return root


def linux_process_identity(pid: int) -> dict[str, Any]:
    """Return a no-follow identity for a live Linux process, or fail closed."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("worker PID must be a positive integer")
    proc = f"/proc/{pid}"
    try:
        st = os.stat(proc, follow_symlinks=False)
        if not os.path.isdir(proc) or st.st_uid != os.getuid():
            raise ValueError("worker process identity is not owned by the controller")
        fields = open(f"{proc}/stat", "rb", buffering=0).read().split()
        if len(fields) < 22:
            raise ValueError("worker process stat is incomplete")
        start_ticks = int(fields[21])
        exe = os.readlink(f"{proc}/exe")
        cmdline = open(f"{proc}/cmdline", "rb", buffering=0).read()
        if not exe or not cmdline:
            raise ValueError("worker process identity is incomplete")
    except (OSError, ValueError, IndexError) as exc:
        raise ValueError("worker PID is not a live, identifiable Linux process") from exc
    return {"pid": pid, "start_ticks": start_ticks, "uid": int(st.st_uid), "exe": exe, "cmdline": cmdline.hex()}


def same_linux_process_identity(identity: dict[str, Any]) -> bool:
    """Re-read /proc and compare every persisted identity field."""
    try:
        return linux_process_identity(identity["pid"]) == identity
    except (KeyError, TypeError, ValueError):
        return False


def _canonical_bytes(document: dict[str, Any], *, domain: str, operation: str) -> bytes:
    if not isinstance(document, dict) or set(document) != _REQUIRED:
        raise ValueError("approval document has unexpected or missing fields")
    if document["domain"] != domain or document["version"] != APPROVAL_VERSION or document["operation"] != operation:
        raise ValueError("approval document domain or version mismatch")
    for key in ("request_id", "nonce", "operator_id", "reason"):
        if not isinstance(document[key], str) or not document[key].strip():
            raise ValueError(f"approval document {key} is required")
    if not isinstance(document["authority"], dict) or not document["authority"]:
        raise ValueError("approval document authority is required")
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def canonical_approval_bytes(document: dict[str, Any]) -> bytes:
    return _canonical_bytes(document, domain=APPROVAL_DOMAIN, operation="revalidate-native-release")


def revalidation_revision_identity(document_bytes: bytes) -> str:
    """Identity for one exact signed revalidation revision."""
    if not isinstance(document_bytes, bytes) or not document_bytes:
        raise ValueError("signed revalidation document bytes are required")
    return hashlib.sha256(document_bytes).hexdigest()


def canonical_activation_bytes(document: dict[str, Any]) -> bytes:
    return _canonical_bytes(document, domain=ACTIVATION_DOMAIN, operation="activate-native-release")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("approval document contains duplicate JSON keys")
        result[key] = value
    return result


def parse_approval_document(raw: bytes | str) -> dict[str, Any]:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not isinstance(data, bytes):
        raise ValueError("approval document must be UTF-8 bytes")
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON number")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("approval document is not valid canonical JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("approval document is not canonical JSON")
    if document.get("domain") == ACTIVATION_DOMAIN and document.get("operation") == "activate-native-release":
        canonical = canonical_activation_bytes(document)
    else:
        canonical = canonical_approval_bytes(document)
    if canonical != data:
        raise ValueError("approval document is not canonical JSON")
    return document


def fingerprint_public_key(public_key: bytes) -> str:
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise ValueError("Ed25519 public key must be exactly 32 bytes")
    return hashlib.sha256(public_key).hexdigest()


def verify_detached_signature(document_bytes: bytes, signature: bytes, public_key: bytes, expected_fingerprint: str) -> bool:
    if fingerprint_public_key(public_key) != expected_fingerprint:
        raise ValueError("operator signer fingerprint does not match persisted registration")
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise ValueError("detached Ed25519 signature must be 64 bytes")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, document_bytes)
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("detached operator signature verification failed") from exc
    return True


def validate_activation_continuation_snapshot(activation_post: dict[str, Any], current: dict[str, Any], *, acknowledged_at: int, profile: str, workspace_path: str, branch: str, repository_identity: str, base_sha: str, handoff_summary: str, prior_running_observation: dict[str, Any] | None = None) -> str:
    """Validate Hermes' one-run continuation after an acknowledged activation."""
    if not isinstance(activation_post, dict) or not isinstance(current, dict) or type(acknowledged_at) is not int or acknowledged_at < 0:
        raise ValueError("Hermes activation continuation snapshot is malformed")
    before = json.loads(canonical_snapshot_json(snapshot_authority(activation_post)))
    after = json.loads(canonical_snapshot_json(snapshot_authority(current)))
    # Compare the complete raw root first.  Only fields owned by Hermes'
    # documented lifecycle transition may differ; typed projections are never
    # used as a substitute for this check.
    root_allowed = {"task", "latest_summary", "runs", "events", "comments"}
    if {k: before.get(k) for k in before if k not in root_allowed} != {k: after.get(k) for k in after if k not in root_allowed}:
        raise ValueError("Hermes activation continuation unknown root field drift")
    if before.get("task", {}).get("status") != "ready":
        raise ValueError("Hermes activation continuation does not start from acknowledged ready state")
    if before.get("comments") != after.get("comments"):
        raise ValueError("Hermes activation continuation rewrote prior comments")
    before_task = before.get("task")
    after_task = after.get("task")
    if not isinstance(before_task, dict) or not isinstance(after_task, dict):
        raise ValueError("Hermes activation continuation snapshot is incomplete")
    for key in ("session_id", "started_at"):
        if key not in before_task or key not in after_task:
            raise ValueError("Hermes activation continuation snapshot is incomplete")
    if after_task.get("branch_name") != branch or after_task.get("repository_identity") not in {None, repository_identity} or after_task.get("base_sha") not in {None, base_sha}:
        raise ValueError("Hermes activation continuation repository or branch drift")
    if after_task.get("completed_at") is not None:
        raise ValueError("Hermes activation continuation completed the task")
    prior_runs = before.get("runs")
    later_runs = after.get("runs")
    prior_events = _history(before, "events", "task_events")
    later_events = _history(after, "events", "task_events")
    if not isinstance(prior_runs, list) or not isinstance(later_runs, list) or not isinstance(prior_events, list) or not isinstance(later_events, list):
        raise ValueError("Hermes activation continuation histories are malformed")
    if later_runs[:len(prior_runs)] != prior_runs or len(later_runs) != len(prior_runs) + 1:
        raise ValueError("Hermes activation continuation has missing, extra, or rewritten runs")
    run = later_runs[-1]
    if not isinstance(run, dict) or not {"id", "status", "outcome", "started_at", "ended_at", "summary", "profile", "worker_pid", "metadata"}.issubset(run):
        raise ValueError("Hermes activation continuation run shape is unsupported")
    if run.get("profile") != profile or type(run.get("id")) is not int or run.get("id") < 0 or type(run.get("started_at")) is not int or run.get("started_at") < 0 or run["started_at"] <= acknowledged_at:
        raise ValueError("Hermes activation continuation run identity or timing drift")
    base_task = before_task
    task = after_task
    runtime_task_fields = {"status", "started_at", "completed_at", "session_id"}
    task_before = {key: value for key, value in base_task.items() if key not in runtime_task_fields}
    task_after = {key: value for key, value in task.items() if key not in runtime_task_fields}
    if task_after != task_before:
        raise ValueError("Hermes activation continuation task identity drift")
    if task.get("assignee") != profile or task.get("workspace_kind") != "worktree" or task.get("workspace_path") != workspace_path:
        raise ValueError("Hermes activation continuation routing or workspace drift")
    run_id = run["id"]
    if task.get("status") == "running":
        if not isinstance(task.get("session_id"), str) or not task["session_id"].strip() or task.get("started_at") != run["started_at"]:
            raise ValueError("Hermes activation continuation running session identity is invalid")
        if run.get("status") != "running" or run.get("outcome") is not None or run.get("ended_at") is not None or run.get("summary") is not None or run.get("metadata") is not None or type(run.get("worker_pid")) is not int or run["worker_pid"] <= 0:
            raise ValueError("Hermes activation continuation running worker shape is invalid")
        if len(later_events) != len(prior_events) + 1 or later_events[:-1] != prior_events:
            raise ValueError("Hermes activation continuation event history was rewritten")
        if after.get("latest_summary") != before.get("latest_summary"):
            raise ValueError("Hermes activation continuation summary drift")
        event = later_events[-1]
        if not isinstance(event, dict) or set(event) != {"kind", "payload", "created_at", "run_id"} or event.get("kind") != "claimed" or event.get("run_id") != run_id or type(event.get("created_at")) is not int or event["created_at"] != run["started_at"] or event["created_at"] <= acknowledged_at:
            raise ValueError("Hermes activation continuation claimed event is invalid")
        payload = event.get("payload")
        if not isinstance(payload, dict) or set(payload) != {"lock", "expires", "run_id"} or payload.get("run_id") != run_id or not isinstance(payload.get("lock"), str) or not payload["lock"] or type(payload.get("expires")) is not int or payload["expires"] < event["created_at"]:
            raise ValueError("Hermes activation continuation claimed payload is invalid")
        return "running"
    if task.get("status") in {"blocked", "triage"}:
        if not isinstance(prior_running_observation, dict):
            raise ValueError("Hermes terminal handoff has no prior running observation")
        if task.get("session_id") is not None:
            raise ValueError("Hermes terminal handoff retains current worker authority")
        if run.get("status") != "blocked" or run.get("outcome") != "blocked" or run.get("summary") != handoff_summary or type(run.get("ended_at")) is not int or run["ended_at"] < 0 or run["ended_at"] <= run["started_at"] or run.get("worker_pid") is not None or run.get("metadata") is not None or task.get("started_at") != run["started_at"]:
            raise ValueError("Hermes terminal handoff run shape is invalid")
        if len(later_events) != len(prior_events) + 2 or later_events[:-2] != prior_events:
            raise ValueError("Hermes terminal handoff event history was rewritten")
        if after.get("latest_summary") != handoff_summary:
            raise ValueError("Hermes terminal handoff summary is not the run handoff")
        claimed, blocked = later_events[-2:]
        if not isinstance(claimed, dict) or set(claimed) != {"kind", "payload", "created_at", "run_id"} or claimed.get("kind") != "claimed" or claimed.get("run_id") != run_id or not isinstance(blocked, dict) or set(blocked) != {"kind", "payload", "created_at", "run_id"} or blocked.get("kind") != "blocked" or blocked.get("run_id") != run_id:
            raise ValueError("Hermes terminal handoff events are invalid")
        if type(claimed["created_at"]) is not int or type(blocked["created_at"]) is not int or claimed["created_at"] != run["started_at"] or blocked["created_at"] != run["ended_at"] or blocked["created_at"] <= claimed["created_at"] or blocked["payload"] != {"reason": handoff_summary}:
            raise ValueError("Hermes terminal handoff event timing or payload is invalid")
        if (int(prior_running_observation.get("run_id", -1)) != run_id or not isinstance(prior_running_observation.get("session_id"), str) or not prior_running_observation["session_id"].strip() or int(prior_running_observation.get("pid", -1)) <= 0):
            raise ValueError("Hermes terminal handoff is not the observed run/session lineage")
        raw_observation = prior_running_observation.get("snapshot_json")
        if not isinstance(raw_observation, str) or not raw_observation.strip():
            raise ValueError("Hermes terminal handoff has incomplete running snapshot evidence")
        try:
            observed = json.loads(raw_observation, object_pairs_hook=_no_duplicates, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON number")))
            observed_json = canonical_snapshot_json(observed)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("Hermes terminal handoff running snapshot is malformed") from exc
        if observed_json != raw_observation:
            raise ValueError("Hermes terminal handoff running snapshot is not canonical JSON")
        observed_task = observed.get("task") if isinstance(observed, dict) else None
        observed_runs = observed.get("runs") if isinstance(observed, dict) else None
        if not isinstance(observed_task, dict) or not isinstance(observed_runs, list) or not observed_runs or observed_task.get("status") != "running":
            raise ValueError("Hermes terminal handoff observation is not a running snapshot")
        observed_run = observed_runs[-1]
        if not isinstance(observed_run, dict) or observed_run.get("id") != run_id:
            raise ValueError("Hermes terminal handoff observation run identity drift")
        try:
            if validate_activation_continuation_snapshot(
                before, observed, acknowledged_at=acknowledged_at, profile=profile,
                workspace_path=workspace_path, branch=branch,
                repository_identity=repository_identity, base_sha=base_sha,
                handoff_summary=handoff_summary,
            ) != "running":
                raise ValueError("Hermes terminal handoff observation is not an authorized continuation")
        except ValueError as exc:
            raise ValueError("Hermes terminal handoff observation is not an authorized continuation") from exc
        immutable_observed = {k: v for k, v in observed_task.items() if k not in runtime_task_fields}
        immutable_terminal = {k: v for k, v in task.items() if k not in runtime_task_fields}
        if immutable_observed != immutable_terminal:
            raise ValueError("Hermes terminal handoff observation task identity drift")
        if observed_task.get("session_id") != prior_running_observation["session_id"] or observed_run.get("worker_pid") != int(prior_running_observation["pid"]):
            raise ValueError("Hermes terminal handoff observation session or PID drift")
        if prior_running_observation.get("snapshot_hash") != hashlib.sha256(raw_observation.encode("utf-8")).hexdigest():
            raise ValueError("Hermes terminal handoff observation snapshot hash drift")
        if prior_running_observation.get("profile") != observed_run.get("profile") or prior_running_observation.get("workspace_path") != observed_task.get("workspace_path") or prior_running_observation.get("branch") != observed_task.get("branch_name"):
            raise ValueError("Hermes terminal handoff observation routing drift")
        return "terminal"
    raise ValueError("Hermes activation continuation task status is unsupported")


def canonical_snapshot_json(snapshot: Any) -> str:
    """Serialize a board execution snapshot without losing scalar identity."""
    value = snapshot_authority(snapshot)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def validate_activation_post_snapshot(pre_snapshot: dict[str, Any], post_snapshot: dict[str, Any], *, marker_present: bool, marker: str | None = None) -> str:
    """Require the complete, append-only effect of Hermes ``unblock``.

    ``kanban unblock --reason X`` first appends ``UNBLOCK: X`` through the
    supported CLI wrapper and then appends one task-scoped ``unblocked`` event.
    Neither history is optional: a ready-only state, or either partial effect,
    is not an acknowledged activation.
    """
    if not isinstance(pre_snapshot, dict) or not isinstance(post_snapshot, dict):
        raise ValueError("native release activation snapshot is malformed")
    if not marker_present or not isinstance(marker, str) or not marker:
        raise ValueError("native release activation marker is missing or duplicated")
    before = json.loads(json.dumps(pre_snapshot, sort_keys=True, separators=(",", ":")))
    after = json.loads(json.dumps(post_snapshot, sort_keys=True, separators=(",", ":")))
    try:
        if before["task"]["status"] != "scheduled" or after["task"]["status"] != "ready":
            raise ValueError("native release activation status delta is not scheduled-to-ready")
        before["task"]["status"] = after["task"]["status"]
        prior_comments = before["comments"]
        later_comments = after["comments"]
        prior_events = _history(before, "events", "task_events")
        later_events = _history(after, "events", "task_events")
    except (KeyError, TypeError) as exc:
        raise ValueError("native release activation snapshot is malformed") from exc
    if not isinstance(prior_comments, list) or not isinstance(later_comments, list) or not isinstance(prior_events, list) or not isinstance(later_events, list):
        raise ValueError("native release activation histories are malformed")
    if len(later_comments) != len(prior_comments) + 1 or later_comments[:-1] != prior_comments:
        raise ValueError("native release activation comment history was rewritten")
    appended_comment = later_comments[-1]
    if not isinstance(appended_comment, dict) or set(appended_comment) != {"author", "body", "created_at"}:
        raise ValueError("native release activation appended comment shape is unsupported")
    if appended_comment["body"] != f"UNBLOCK: {marker}" or not isinstance(appended_comment["author"], str) or not appended_comment["author"].strip() or type(appended_comment["created_at"]) is not int:
        raise ValueError("native release activation appended comment is not the exact marker comment")
    if len(later_events) != len(prior_events) + 1 or later_events[:-1] != prior_events:
        raise ValueError("native release activation task event history was rewritten")
    appended_event = later_events[-1]
    if not isinstance(appended_event, dict) or set(appended_event) != {"kind", "payload", "created_at", "run_id"}:
        raise ValueError("native release activation appended event shape is unsupported")
    if appended_event != {"kind": "unblocked", "payload": None, "created_at": appended_event.get("created_at"), "run_id": None} or type(appended_event["created_at"]) is not int or appended_event["created_at"] < appended_comment["created_at"]:
        raise ValueError("native release activation appended event is not the exact supported unblock event")
    before["comments"] = later_comments
    event_key = "events" if "events" in before else "task_events"
    before[event_key] = later_events
    if before != after:
        raise ValueError("native release activation post-state has unauthorized mutation")
    return hashlib.sha256(canonical_snapshot_json(post_snapshot).encode("utf-8")).hexdigest()
