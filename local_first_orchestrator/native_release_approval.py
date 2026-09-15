"""Detached operator approval documents for native release revalidation.

This module has no signing capability. The plugin only verifies signatures made
outside the plugin by an operator-controlled key.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

APPROVAL_DOMAIN = "native-release-revalidation"
ACTIVATION_DOMAIN = "native-release-activation"
APPROVAL_VERSION = 1
_REQUIRED = {"domain", "version", "operation", "request_id", "nonce", "operator_id", "reason", "authority"}


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


def validate_activation_continuation_snapshot(activation_post: dict[str, Any], current: dict[str, Any], *, acknowledged_at: int, profile: str, workspace_path: str, branch: str, repository_identity: str, base_sha: str, handoff_summary: str) -> str:
    """Validate Hermes' one-run continuation after an acknowledged activation."""
    if not isinstance(activation_post, dict) or not isinstance(current, dict) or type(acknowledged_at) is not int:
        raise ValueError("Hermes activation continuation snapshot is malformed")
    before = json.loads(json.dumps(activation_post, sort_keys=True, separators=(",", ":")))
    after = json.loads(json.dumps(current, sort_keys=True, separators=(",", ":")))
    if before.get("task", {}).get("status") != "ready":
        raise ValueError("Hermes activation continuation does not start from acknowledged ready state")
    if before.get("comments") != after.get("comments"):
        raise ValueError("Hermes activation continuation rewrote prior comments")
    for key in ("session_id", "branch_name", "started_at", "completed_at", "current_run_id"):
        if key not in before or key not in after:
            raise ValueError("Hermes activation continuation snapshot is incomplete")
    if after.get("branch_name") != branch or after.get("repository_identity") not in {None, repository_identity} or after.get("base_sha") not in {None, base_sha}:
        raise ValueError("Hermes activation continuation repository or branch drift")
    if after.get("completed_at") is not None:
        raise ValueError("Hermes activation continuation completed the task")
    prior_runs = before.get("runs")
    later_runs = after.get("runs")
    prior_events = before.get("task_events")
    later_events = after.get("task_events")
    if not isinstance(prior_runs, list) or not isinstance(later_runs, list) or not isinstance(prior_events, list) or not isinstance(later_events, list):
        raise ValueError("Hermes activation continuation histories are malformed")
    if later_runs[:len(prior_runs)] != prior_runs or len(later_runs) != len(prior_runs) + 1:
        raise ValueError("Hermes activation continuation has missing, extra, or rewritten runs")
    run = later_runs[-1]
    if not isinstance(run, dict) or set(run) != {"id", "status", "outcome", "started_at", "ended_at", "summary", "profile", "worker_pid", "metadata"}:
        raise ValueError("Hermes activation continuation run shape is unsupported")
    if run.get("profile") != profile or type(run.get("id")) is not int or run.get("started_at") is None or int(run["started_at"]) <= acknowledged_at:
        raise ValueError("Hermes activation continuation run identity or timing drift")
    if after.get("current_run_id") != run["id"] and after.get("task", {}).get("status") == "running":
        raise ValueError("Hermes activation continuation current run identity drift")
    base_task = before.get("task")
    task = after.get("task")
    if not isinstance(base_task, dict) or not isinstance(task, dict) or {key: value for key, value in task.items() if key != "status"} != {key: value for key, value in base_task.items() if key != "status"}:
        raise ValueError("Hermes activation continuation task identity drift")
    if task.get("assignee") != profile or task.get("workspace_kind") != "worktree" or task.get("workspace_path") != workspace_path:
        raise ValueError("Hermes activation continuation routing or workspace drift")
    run_id = run["id"]
    if task.get("status") == "running":
        if not isinstance(after.get("session_id"), str) or not after["session_id"].strip() or after.get("started_at") != run["started_at"] or after.get("current_run_id") != run_id:
            raise ValueError("Hermes activation continuation running session identity is invalid")
        if run.get("status") != "running" or run.get("outcome") is not None or run.get("ended_at") is not None or run.get("summary") is not None or run.get("metadata") is not None or type(run.get("worker_pid")) is not int or run["worker_pid"] <= 0:
            raise ValueError("Hermes activation continuation running worker shape is invalid")
        if len(later_events) != len(prior_events) + 1 or later_events[:-1] != prior_events:
            raise ValueError("Hermes activation continuation event history was rewritten")
        event = later_events[-1]
        if not isinstance(event, dict) or event.get("kind") != "claimed" or event.get("run_id") != run_id or not isinstance(event.get("created_at"), int) or event["created_at"] != run["started_at"] or event["created_at"] <= acknowledged_at:
            raise ValueError("Hermes activation continuation claimed event is invalid")
        payload = event.get("payload")
        if not isinstance(payload, dict) or set(payload) != {"lock", "expires", "run_id"} or payload.get("run_id") != run_id or not isinstance(payload.get("lock"), str) or not payload["lock"] or type(payload.get("expires")) is not int:
            raise ValueError("Hermes activation continuation claimed payload is invalid")
        return "running"
    if task.get("status") == "blocked":
        if after.get("session_id") is not None or after.get("current_run_id") is not None:
            raise ValueError("Hermes terminal handoff retains current worker authority")
        if run.get("status") != "blocked" or run.get("outcome") != "blocked" or run.get("summary") != handoff_summary or run.get("ended_at") is None or int(run["ended_at"]) <= int(run["started_at"]) or run.get("worker_pid") is not None or run.get("metadata") is not None:
            raise ValueError("Hermes terminal handoff run shape is invalid")
        if len(later_events) != len(prior_events) + 2 or later_events[:-2] != prior_events:
            raise ValueError("Hermes terminal handoff event history was rewritten")
        claimed, blocked = later_events[-2:]
        if not isinstance(claimed, dict) or claimed.get("kind") != "claimed" or claimed.get("run_id") != run_id or not isinstance(blocked, dict) or blocked.get("kind") != "blocked" or blocked.get("run_id") != run_id:
            raise ValueError("Hermes terminal handoff events are invalid")
        return "terminal"
    raise ValueError("Hermes activation continuation task status is unsupported")


def canonical_snapshot_json(snapshot: Any) -> str:
    """Serialize a board execution snapshot without losing scalar identity."""
    from dataclasses import asdict, is_dataclass
    value = asdict(snapshot) if is_dataclass(snapshot) else snapshot
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
        prior_events = before["task_events"]
        later_events = after["task_events"]
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
    before["task_events"] = later_events
    if before != after:
        raise ValueError("native release activation post-state has unauthorized mutation")
    return hashlib.sha256(canonical_snapshot_json(post_snapshot).encode("utf-8")).hexdigest()
