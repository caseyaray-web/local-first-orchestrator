"""Compact M7 public wiring proof; worker evidence here is explicitly synthetic."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from local_first_orchestrator import cli, plugin_tools
from local_first_orchestrator import composition
from local_first_orchestrator.composition import build_runtime
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.decomposition_planner import PlanningRequest
from local_first_orchestrator.contracts import ConflictError
from local_first_orchestrator.evidence_store import EvidenceStore
from tests.test_operator_api import FixtureBoard, SCOPE, _config, _write_config


class _Coordinator:
    def __init__(self):
        self.calls = []
        self._native_worker_context = None
    def _bind_native_worker_context(self, context):
        self._native_worker_context = context
    def _current_native_worker_context(self):
        if self._native_worker_context is None:
            raise ValueError("native_worker_context_unbound")
        return self._native_worker_context
    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs)); return {"outcome": "verified", "method": name}
        return call


def _bootstrap_request(objective: str = "Deliver bounded bootstrap") -> PlanningRequest:
    return PlanningRequest(
        board_id=SCOPE["board_id"], anchor_id=SCOPE["anchor_task_id"], repository_identity="repo-A",
        base_sha="a" * 40, snapshot_hash="b" * 64, root_contract_hash="c" * 64,
        expected_criteria=frozenset({"AC-1"}), authorized_paths=frozenset({"src/a.py"}),
        max_tranches=2, max_tickets=4, max_context_tokens=4096, max_patch_files=2,
        max_patch_lines=100, max_attempts=2, verification_commands=(("python", "-m", "pytest"),),
        verification_timeout_seconds=60, verification_output_limit=20000, max_payload_bytes=65536,
        max_json_depth=16, objective=objective, non_goals=("No unrelated changes",),
        criterion_statements=(("AC-1", "Complete the bounded bootstrap"),),
    )


@pytest.mark.parametrize(("command", "expected"), [
    (["prepare-planner", "--request-id", "r"], "prepare_planner"),
    (["release-planner", "--request-id", "r"], "release_planner"),
    (["accept-plan", "--plan-id", "p"], "accept_validated_plan"),
    (["prepare-piece", "--plan-id", "p", "--ticket-id", "t"], "prepare_active_piece"),
    (["link-piece", "--plan-id", "p", "--child-ticket-id", "c", "--parent-ticket-id", "q"], "execute_accepted_dependency_link"),
    (["release-piece", "--plan-id", "p", "--ticket-id", "t"], "release_active_piece"),
    (["integrate-piece", "--plan-id", "p", "--ticket-id", "t", "--review-id", "r"], "integrate_persisted_active_piece"),
    (["prepare-paid-review", "--plan-id", "p"], "prepare_paid_integrated_review"),
    (["release-paid-review", "--plan-id", "p"], "release_paid_integrated_review"),
    (["prepare-paid-correction", "--plan-id", "p", "--review-id", "r"], "prepare_paid_correction"),
    (["release-paid-correction", "--plan-id", "p", "--review-id", "r"], "release_paid_correction"),
    (["accept-tranche", "--plan-id", "p", "--review-id", "r", "--authorize-successor"], "accept_tranche"),
])
def test_m7_operator_commands_route_only_to_identity_facades(monkeypatch, capsys, command, expected):
    coordinator = _Coordinator()
    config = object()
    runtime = SimpleNamespace(coordinator=coordinator)
    closed = []
    monkeypatch.setattr(cli.PluginConfig, "from_file", classmethod(lambda _cls, _path: config))
    monkeypatch.setattr(cli, "build_runtime", lambda _config, *, scope: runtime)
    monkeypatch.setattr(cli, "close_runtime", lambda value: closed.append(value))
    monkeypatch.setattr(cli, "build_git_adapter", lambda value: "trusted-git")
    assert cli.main(["--config", "/trusted.json", "--board", "board", "--anchor-task-id", "anchor", *command]) == 0
    assert coordinator.calls == [(expected, coordinator.calls[0][1], coordinator.calls[0][2])]
    assert closed == [runtime]
    assert json.loads(capsys.readouterr().out)["method"] == expected


def test_m7_parser_rejects_missing_successor_authorization():
    with pytest.raises(SystemExit):
        cli.main(["--config", "/trusted.json", "--board", "board", "--anchor-task-id", "anchor",
                  "accept-tranche", "--plan-id", "p", "--review-id", "r"])


@pytest.mark.parametrize("outcome", ("held", "invalid", "accepted"))
def test_m7_accept_plan_preserves_fail_closed_result_outcome(monkeypatch, capsys, outcome):
    """The CLI may supply a default only; it must not turn a held/invalid plan into acceptance."""
    coordinator = SimpleNamespace(accept_validated_plan=lambda *_args, **_kwargs: {"outcome": outcome, "proof": outcome})
    runtime = SimpleNamespace(coordinator=coordinator)
    config = SimpleNamespace()
    monkeypatch.setattr(cli.PluginConfig, "from_file", classmethod(lambda _cls, _path: config))
    monkeypatch.setattr(cli, "build_runtime", lambda _config, *, scope: runtime)
    monkeypatch.setattr(cli, "close_runtime", lambda _runtime: None)

    assert cli.main(("--config", "/trusted.json", "--board", "board", "--anchor-task-id", "anchor",
                     "accept-plan", "--plan-id", "plan")) == {"held": 3, "invalid": 2, "accepted": 0}[outcome]
    assert json.loads(capsys.readouterr().out) == {"outcome": outcome, "proof": outcome}


def test_m7_bootstrap_cli_routes_through_the_locked_coordinator_facade(monkeypatch, capsys, tmp_path):
    coordinator = _Coordinator()
    runtime = SimpleNamespace(coordinator=coordinator)
    request_file = tmp_path / "request.json"; request_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli.PluginConfig, "from_file", classmethod(lambda _cls, _path: object()))
    monkeypatch.setattr(cli, "build_runtime", lambda _config, *, scope: runtime)
    monkeypatch.setattr(cli, "close_runtime", lambda _runtime: None)
    monkeypatch.setattr(cli, "operator_planning_request", lambda _config, path: _bootstrap_request())
    assert cli.main(("--config", "/trusted.json", "--board", "board", "--anchor-task-id", "anchor",
                     "bootstrap-planning", "--request-file", str(request_file), "--request-id", "initial")) == 0
    name, args, kwargs = coordinator.calls[-1]
    assert name == "bootstrap_planning_request" and kwargs["request_id"] == "initial"
    assert len(args) == 1 and callable(args[0])
    assert json.loads(capsys.readouterr().out)["method"] == "bootstrap_planning_request"


@pytest.mark.parametrize("intent", ("pause", "cancel"))
def test_m7_bootstrap_is_held_before_request_observation_when_paused_or_cancelled(tmp_path, intent):
    config = _config(tmp_path)
    store = EvidenceStore.open(config.evidence_store_path, create_new=True); store.migrate(); store.close()
    runtime = build_runtime(config, board=FixtureBoard(), scope=SCOPE)
    try:
        assert runtime.coordinator.enroll()["outcome"] in {"verified", "recorded", "deduplicated"}
        assert getattr(runtime.coordinator, intent)()["outcome"] == "verified"
        baseline = tuple(runtime.store.connection.iterdump())
        observed = []
        result = runtime.coordinator.bootstrap_planning_request(
            lambda: observed.append("git-observation") or _bootstrap_request(), request_id="initial")
        assert result == {"outcome": "held", "reason": "operator_pause_or_cancellation_active", "actions_attempted": 0}
        assert observed == []
        assert tuple(runtime.store.connection.iterdump()) == baseline
        assert not runtime.store.read_scope(SCOPE)["budget_events"]
    finally:
        runtime.store.close()


def test_m7_bootstrap_store_scope_immutability_is_atomic_across_two_connections(tmp_path):
    path = tmp_path / "evidence.sqlite"
    store = EvidenceStore.open(path, create_new=True); store.migrate(); store.close()
    barrier = threading.Barrier(2)
    outcomes, errors = [], []

    def persist(request_id, objective):
        connection = EvidenceStore.open(path)
        try:
            barrier.wait(timeout=10)
            outcomes.append(connection.bootstrap_planning_request(SCOPE, _bootstrap_request(objective), request_id=request_id))
        except BaseException as error:
            errors.append(error)
        finally:
            connection.close()

    workers = [threading.Thread(target=persist, args=("first", "First bootstrap")),
               threading.Thread(target=persist, args=("second", "Second bootstrap"))]
    for worker in workers: worker.start()
    for worker in workers: worker.join(timeout=20)
    assert not [worker for worker in workers if worker.is_alive()]
    assert len(outcomes) == 1 and len(errors) == 1 and isinstance(errors[0], ConflictError)
    reopened = EvidenceStore.open(path)
    try:
        operations = [op for op in reopened.read_scope(SCOPE)["operations"] if op.effect == "bootstrap_planning_request"]
        assert len(operations) == 1
        assert reopened.read_bootstrap_planning_request(SCOPE)["request"]["objective"] in {"First bootstrap", "Second bootstrap"}
    finally:
        reopened.close()


def test_m7_operator_bootstrap_rejects_head_drift_before_persistence(tmp_path, monkeypatch):
    config = _config(tmp_path)
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps({"version": 1, "objective": "Bounded request", "non_goals": [],
                                        "criteria": [{"id": "AC-1", "statement": "Complete it"}],
                                        "authorized_paths": ["src/a.py"]}), encoding="utf-8")
    heads = iter(("a" * 40, "b" * 40))
    calls = []
    def observed_git(_config, *args):
        calls.append(args)
        if args == ("rev-parse", "HEAD"): return next(heads)
        if args == ("status", "--porcelain=v1", "--untracked-files=all"): return ""
        if args == ("ls-files", "-z"): return "src/a.py\0"
        if args == ("ls-tree", "-r", "a" * 40): return "100644 blob deadbeef\tsrc/a.py"
        raise AssertionError(args)
    monkeypatch.setattr(composition, "_git", observed_git)
    with pytest.raises(ValueError, match="HEAD changed"):
        composition.operator_planning_request(config, request_file)
    assert calls == [("rev-parse", "HEAD"), ("status", "--porcelain=v1", "--untracked-files=all"),
                     ("ls-files", "-z"), ("ls-tree", "-r", "a" * 40), ("rev-parse", "HEAD")]


def test_m7_worker_tools_derive_identity_and_reject_forged_paid_provenance(monkeypatch):
    registered = {}
    class Context:
        def register_tool(self, **kwargs): registered[kwargs["name"]] = kwargs
    coordinator = _Coordinator()
    runtime = SimpleNamespace(scope={"board_id": "b", "anchor_task_id": "a"}, coordinator=coordinator,
                              config=SimpleNamespace(roles={"paid_review_profile": "paid"}))
    closed = []
    monkeypatch.setattr(plugin_tools, "close_runtime", lambda value: closed.append(value))
    monkeypatch.setattr("local_first_orchestrator.composition.build_git_adapter", lambda config: "trusted-git")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "paid-task")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "paid-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "paid-session")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "b")
    plugin_tools.register_tools(Context(), runtime_factory=lambda scope: runtime)
    assert set(plugin_tools.TOOL_NAMES) == set(registered)
    registration = json.loads(registered["local_first_register_planning_request"]["handler"](
        {"board_id": "b", "anchor_task_id": "a", "request_id": "request"}))
    assert registration["ok"] and coordinator.calls[-1] == ("register_planning_request", ("paid-task",), {"request_id": "request"})
    forged = json.loads(registered["local_first_submit_paid_review"]["handler"]({"board_id": "b", "anchor_task_id": "a", "plan_id": "p", "review": {"native_review": {"task_id": "other", "run_id": "paid-run", "session_id": "paid-session", "profile": "paid"}}}))
    assert not forged["ok"]
    assert closed


def test_m7_initialization_and_enrollment_use_the_real_public_cli_runtime(tmp_path, monkeypatch, capsys):
    """Public starting authority is real SQLite/coordinator state, never a mock.

    The broader disposable-Git lifecycle is exercised by the retained M4 public
    core fixture; this regression specifically prevents the new CLI lifecycle
    surface from bypassing initialization, configured scope validation, or the
    actual enrollment authority.
    """
    config = _config(tmp_path)
    config_path = tmp_path / "trusted-config.json"
    _write_config(config, config_path)
    board = FixtureBoard()

    assert cli.main(("--config", str(config_path), "--board", SCOPE["board_id"],
                     "--anchor-task-id", SCOPE["anchor_task_id"], "initialize-store")) == 0
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["outcome"] == "verified"

    # Use a fresh production composition on every command, as a real CLI
    # invocation does; only the disposable native-shaped transport is synthetic.
    monkeypatch.setattr(cli, "build_runtime", lambda parsed, *, scope: build_runtime(parsed, board=board, scope=scope))
    assert cli.main(("--config", str(config_path), "--board", SCOPE["board_id"],
                     "--anchor-task-id", SCOPE["anchor_task_id"], "enroll")) == 0
    enrolled = json.loads(capsys.readouterr().out)
    assert enrolled["outcome"] in {"verified", "recorded", "deduplicated"}
    assert board.writes
    with instance_lock(config.lock_path) as lock:
        lock.assert_held()

    # initialize-store is an effect and must reject a selector scope that does
    # not equal the signed bootstrap configuration before opening/replacing it.
    assert cli.main(("--config", str(config_path), "--board", "other-board",
                     "--anchor-task-id", SCOPE["anchor_task_id"], "initialize-store")) == 2
    assert json.loads(capsys.readouterr().out)["outcome"] == "invalid"


def test_m7_tool_handlers_reject_extra_selectors_before_runtime_creation(monkeypatch):
    registered = {}
    class Context:
        def register_tool(self, **kwargs): registered[kwargs["name"]] = kwargs
    calls = []
    plugin_tools.register_tools(Context(), runtime_factory=lambda _scope: calls.append(_scope))
    result = json.loads(registered["local_first_register_planning_request"]["handler"](
        {"board_id": "b", "anchor_task_id": "a", "request_id": "r", "profile": "forged"}))
    assert result["ok"] is False
    assert "unsupported selectors" in result["error"]
    assert calls == []


@pytest.mark.parametrize("public_intent", (None, "pause", "cancel"), ids=("joined", "pause-resume", "cancellation"))
def test_m7_public_driver_executes_disposable_git_correction_and_acceptance(tmp_path, monkeypatch, capsys, public_intent):
    """A fresh public startup produces the accepted plan consumed by the full core flow."""
    import dataclasses
    from local_first_orchestrator.config import PluginConfig
    from local_first_orchestrator.decomposition_planner import serialize_proposal
    from local_first_orchestrator.planning_coordinator import request_from_payload
    from tests.fixtures.m7_public_lifecycle import PublicLifecycleDriver
    from tests.test_m4_active_piece_preparation import _batch_proposal

    from local_first_orchestrator.contracts import ActionResult
    from tests.test_m4_public_core_flow import _exercise_public_m4_core_flow, _install_board, _snapshot

    driver = {}

    def bootstrap_batch_proposal(request):
        """Keep the retained plan shape while binding its checks to server policy."""
        proposal = _batch_proposal(request)
        from local_first_orchestrator.ticket import VerificationProfile
        verification = VerificationProfile(request.verification_commands,
                                           request.verification_timeout_seconds,
                                           request.verification_output_limit)
        tranches = tuple(dataclasses.replace(tranche, tickets=tuple(
            dataclasses.replace(ticket, verification=verification) for ticket in tranche.tickets
        )) for tranche in proposal.plan.tranches)
        return dataclasses.replace(proposal, plan=dataclasses.replace(proposal.plan, tranches=tranches))

    def public_setup(repository, git_adapter, base_sha):
        state = tmp_path / "public-state"; state.mkdir(mode=0o700)
        home = tmp_path / "public-home"; home.mkdir(mode=0o700)
        git_adapter.worktree_root.mkdir(mode=0o700)
        check = tmp_path / "public-check"; check.write_text("#!/bin/sh\nexit 0\n"); check.chmod(0o700)
        config = PluginConfig.from_mapping({
            "version": 1, "state_root": str(state), "hermes_executable": str(check),
            "hermes_home": str(home), "kanban_home": str(home), "scope": dict(SCOPE),
            "trusted_roots": {"repository": str(repository), "workspace": str(git_adapter.worktree_root)},
            "roles": {"implementation_profile": "implementer", "local_review_profile": "local",
                      "planning_profile": "planner", "paid_review_profile": "paid"},
            "budgets": {"implementation_attempts": 5, "review_corrections": 5,
                        "infrastructure_retries": 5, "workflow_repairs": 5, "paid_capacity": 5},
            "poll_interval_seconds": 1, "check_commands": [{"check_id": "smoke", "argv": [str(check)]}],
        })
        config_path = tmp_path / "public-config.json"; _write_config(config, config_path)
        assert cli.main(("--config", str(config_path), "--board", SCOPE["board_id"],
                         "--anchor-task-id", SCOPE["anchor_task_id"], "initialize-store")) == 0
        assert json.loads(capsys.readouterr().out)["outcome"] == "verified"

        board = FixtureBoard()
        runtime = build_runtime(config, board=board, scope=SCOPE)
        cards = board.cards
        runs = _install_board(runtime.coordinator, runtime.store, board, cards)
        # Preserve the full native-shaped card envelope on public pause so the
        # subsequent public resume validates pause receipts rather than a fake
        # transport's reduced snapshot.
        def hold(action, task_id, _reason):
            board.writes.append("hold:" + task_id)
            cards[task_id] = _snapshot(cards[task_id], status="blocked")
            return ActionResult(action.key, "verified", "fixture hold", board.read_task(task_id).to_dict())
        board.hold = hold
        board._verify_native_hold_release = lambda _action, _snapshot: None
        board._workspace_routing = lambda native, expected: None if native.get("workspace") == expected else "workspace mismatch"
        value = PublicLifecycleDriver(tmp_path, monkeypatch, board=board, scope=SCOPE,
                                      repository=repository, config_path=config_path, config=config)
        value.combined_check_runner = lambda context: {
            "head_sha": context["head_sha"],
            "checks": [{"check_id": "smoke", "command": "fixture trusted check", "exit_code": 0,
                        "output_sha256": "sha256:" + context["head_sha"]}],
        }
        driver["value"] = value
        driver["base"] = base_sha
        operator_request = tmp_path / "public-planning-request.json"
        operator_request.write_text(json.dumps({"version": 1, "objective": "Deliver bounded public lifecycle wiring",
                                                 "non_goals": ["No unrelated changes"],
                                                 "authorized_paths": ["src/a.py", "tests/a.py"],
                                                 "criteria": [{"id": "AC-1", "statement": "Complete the bounded public flow"},
                                                              {"id": "AC-2", "statement": "Preserve explicit authority"}]}), encoding="utf-8")
        bootstrap = value.bootstrap_planning(operator_request, "public-plan-request")
        assert bootstrap["outcome"] == "recorded"
        # The retained direct coordinator is only a read/assertion harness. Reopen
        # it after the public command so its SQLite snapshot cannot mask the
        # durable bootstrap written by the fresh public runtime.
        runtime.store.close()
        runtime = build_runtime(config, board=board, scope=SCOPE)
        value._base_claim_create_attempt = board.claim_create_attempt
        request = request_from_payload(runtime.coordinator.planning_observer(SCOPE)["request"])
        assert value.enroll()["outcome"] == "verified"
        planner = value.prepare_planner("public-plan-request")
        assert planner["outcome"] == "held"
        assert value.release_planner("public-plan-request")["outcome"] == "released"
        planner_task = planner["task_id"]
        runs[planner_task] = ({"id": "planner-run", "task_id": planner_task, "profile": "planner",
                               "status": "running", "metadata": {"worker_session_id": "planner-session"}},)
        monkeypatch.setenv("HERMES_KANBAN_TASK", planner_task)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "planner-run")
        monkeypatch.setenv("HERMES_SESSION_ID", "planner-session")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", SCOPE["board_id"])
        assert value.register_planning_request("public-plan-request")["planner_task_id"] == planner_task
        decisions = json.loads(serialize_proposal(bootstrap_batch_proposal(request)))["plan"]
        assert value.submit_plan(decisions, "public-plan-request")["request_identity"] == request.identity
        accepted = value.accept_plan("plan-1", "public-plan-request")
        assert accepted["base_sha"] == base_sha

        # A response lost after the native held-card write leaves a durable
        # unknown effect.  The next *fresh* CLI composition reconciles the exact
        # marker/readback without issuing a second create.
        creates = []
        native_create = board.create_held
        def create_then_lose_response(*args, **kwargs):
            creates.append(args[0].key)
            native_create(*args, **kwargs)
            raise RuntimeError("fixture response lost after native create")
        board.create_held = create_then_lose_response
        assert value.prepare_piece("plan-1", "TK-A")["outcome"] == "partial"
        board.create_held = native_create
        assert value.prepare_piece("plan-1", "TK-A")["outcome"] == "held"
        assert len(creates) == 1

        return runtime.coordinator, runtime.store, board, cards, runs, value

    def before_accept(ctl, store, board, cards, runs, git_adapter, public, plan_id, review_id):
        if public_intent is None:
            return True
        # The synthetic workers have produced their durable review evidence.  Give
        # the pause boundary a clean terminal native snapshot (the production
        # public pause command remains the only intent/effect authority).
        for task_id, task_runs in runs.items():
            runs[task_id] = tuple({**run, "status": "completed"} for run in task_runs)
        for task_id, card in tuple(cards.items()):
            cards[task_id] = _snapshot(card, status="blocked")
        intent = public.pause() if public_intent == "pause" else public.cancel()
        for _ in range(16):
            if intent["outcome"] == "verified":
                break
            assert intent["outcome"] == "partial", intent
            intent = public.pause() if public_intent == "pause" else public.cancel()
        else:
            pytest.fail(f"public pause/cancellation did not contain all managed work: {intent}")
        paused_state = store.read_scope(ctl.scope)
        assert paused_state["operator_intent"] is not None and paused_state["operator_intent"].active
        assert paused_state["operator_intent"].cancellation_requested is (public_intent == "cancel")
        post_pause = (tuple(store.connection.iterdump()), dict(cards),
                      git_adapter.existing_execution_base("TR-A", driver["base"]))
        releases_before = tuple(op.key for op in paused_state["operations"] if op.effect == "release")
        for late in (public.release_piece(plan_id, "TK-A"), public.release_paid_review(plan_id),
                     public.accept_tranche(plan_id, review_id)):
            assert late["outcome"] == "held"
            assert late["reason"] == "operator_pause_or_cancellation_active"
        current = store.read_scope(ctl.scope)
        assert (tuple(store.connection.iterdump()), dict(cards),
                git_adapter.existing_execution_base("TR-A", driver["base"])) == post_pause
        assert tuple(op.key for op in current["operations"] if op.effect == "release") == releases_before
        if public_intent == "cancel":
            assert current["operator_intent"] is not None
            assert current["operator_intent"].active and current["operator_intent"].cancellation_requested
            return False
        for _ in range(16):
            resumed = public.resume()
            if resumed["outcome"] == "verified":
                break
            assert resumed["outcome"] == "partial", resumed
        else:
            pytest.fail("public authorized resume did not clear the paused intent")
        assert store.read_scope(ctl.scope)["operator_intent"].active is False
        assert public.accept_tranche(plan_id, review_id)["outcome"] == "accepted"
        return False

    _exercise_public_m4_core_flow(tmp_path, monkeypatch, public_setup_factory=public_setup, before_accept=before_accept)
    assert set(driver["value"]._registered) == set(plugin_tools.TOOL_NAMES)
    # Every CLI/tool dispatch constructed and closed its own production runtime;
    # no coordinator/store singleton carries authority across public calls.
    assert driver["value"].runtime_opens > 1
    assert driver["value"].runtime_opens == driver["value"].runtime_closes

