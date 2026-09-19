from __future__ import annotations

import json
import subprocess
from typing import Any

from .states import CanonicalState
from .evidence_hash import canonical_sha256


class TrancheNotComplete(ValueError):
    pass


def completion_evidence(ledger: Any, repository: Any, tranche_id: str) -> dict[str, Any]:
    row = ledger.connection.execute("SELECT * FROM tranches WHERE id=?", (tranche_id,)).fetchone()
    if row is None:
        raise TrancheNotComplete("missing tranche")
    tickets = ledger.connection.execute("SELECT id,state FROM tickets WHERE tranche_id=? ORDER BY created_at,id", (tranche_id,)).fetchall()
    if not tickets:
        raise TrancheNotComplete("tranche has no materialized tickets")
    ticket_ids, commits = [], []
    seen_commits: set[str] = set()
    for ticket in tickets:
        ticket_id = str(ticket["id"])
        if ticket["state"] not in {CanonicalState.ACCEPTED.value, CanonicalState.DONE.value}:
            raise TrancheNotComplete("active tranche has unaccepted ticket")
        commit = ledger.accepted_commit(ticket_id)
        if not commit:
            raise TrancheNotComplete("accepted ticket is missing accepted_commit evidence")
        if commit in seen_commits:
            nonadvancing = ledger.connection.execute(
                "SELECT 1 FROM events WHERE entity_type='ticket' AND entity_id=? AND event_type='accepted_evidence_recorded' "
                "AND json_valid(payload_json)=1 AND json_extract(payload_json,'$.commit_sha')=? "
                "AND json_extract(payload_json,'$.integration_advanced')=0 ORDER BY id DESC LIMIT 1",
                (ticket_id, commit),
            ).fetchone()
            if nonadvancing is None:
                raise TrancheNotComplete("duplicate accepted commit lacks non-advancing acceptance evidence")
            continue
        seen_commits.add(commit)
        ticket_ids.append(ticket_id); commits.append(commit)
    def run(*args: str) -> str:
        return subprocess.run(("git", *args), cwd=repository, text=True, capture_output=True, check=True).stdout.strip()
    root = run("rev-parse", "--verify", f"{row['base_sha']}^{{commit}}")
    ref = f"refs/local-first/tranches/{tranche_id}/integration-head"
    final = run("rev-parse", "--verify", f"{ref}^{{commit}}")
    previous = root
    for commit in commits:
        if run("rev-parse", "--verify", f"{commit}^{{commit}}") != commit or run("rev-parse", "--verify", f"{commit}^") != previous:
            raise TrancheNotComplete("accepted commits are not the serialized integration chain")
        previous = commit
    if final != commits[-1]:
        raise TrancheNotComplete("integration head does not match final accepted commit")
    payload = {"tranche_id": tranche_id, "root_planning_sha": root, "final_integration_sha": final, "accepted_ticket_ids": ticket_ids, "accepted_commit_shas": commits}
    return {**payload, "accepted_ticket_ids_json": json.dumps(ticket_ids, separators=(",", ":")), "accepted_commit_shas_json": json.dumps(commits, separators=(",", ":")), "evidence_hash": canonical_sha256(payload)}
