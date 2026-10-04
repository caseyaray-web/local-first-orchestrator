from __future__ import annotations

from local_first_orchestrator.contracts import ActionResult, BoardSnapshot, ManagedMember, PauseIntent
from local_first_orchestrator.operator_controls import verify_containment


SCOPE = {"board_id": "board", "anchor_task_id": "anchor"}
MEMBER = ManagedMember("board", "anchor", "work", "implementation", 0, (), "work")


def _snapshot(status: str, run_status: str) -> BoardSnapshot:
    return BoardSnapshot(
        {"id": "work", "status": status}, (),
        ({"id": "run-1", "status": run_status, "worker_pid": 1234, "stop_supported": True},),
        (), (), (), "now", f"work:{status}:{run_status}",
    )


def _cancellation() -> PauseIntent:
    return PauseIntent(SCOPE, "operator", 0, True, True, active=True,
                       managed_task_ids=("work",), baseline_digests={"work": "work:running:running"})


def test_empty_post_stop_run_list_does_not_hide_unverified_owned_worker() -> None:
    before = _snapshot("running", "running")
    after = _snapshot("blocked", "stopped")
    unsupported = ActionResult("stop_run:work:run-1:work:running:running", "unsupported",
                               "native stop unsupported", after.to_dict())

    result = verify_containment(SCOPE, (MEMBER,), (after,), (), pause_intent=_cancellation(),
                                prior_snapshots=(before,), effect_results=(unsupported,))

    assert result.outcome == "partial"
    assert result.report.problem == "containment_unverified"
    assert result.report.active_workers == ("work/run-1/pid:1234",)
    assert result.report.required_operator_action == "inspect_exact_task_and_run_ids"


def test_exact_stop_readback_clears_prior_worker_observation() -> None:
    before = _snapshot("running", "running")
    after = _snapshot("blocked", "stopped")
    verified = ActionResult("stop_run:work:run-1:work:running:running", "verified",
                            "exact stop readback", {"task_id": "work", "run_id": "run-1",
                                                     "stop_supported": True, "process_exited": True,
                                                     "status": "stopped"})

    result = verify_containment(SCOPE, (MEMBER,), (after,), (), pause_intent=_cancellation(),
                                prior_snapshots=(before,), effect_results=(verified,))

    assert result.outcome == "verified"
    assert result.report.active_workers == ()
