import pytest

from local_first_orchestrator.evidence_store import EvidenceStore, SchemaError
from tests.test_m4_plan_evidence import SCOPE, evidence, proposal, request as base_request
from tests.test_m4_planner_creation import native_fixture as _native_fixture, setup
from tests.test_m4_planner_run_binding import _claim
from local_first_orchestrator.decomposition_planner import serialize_proposal


@pytest.fixture
def native_fixture(tmp_path):
    return _native_fixture.__wrapped__(tmp_path)


def test_controlled_paid_planner_proposal_is_accepted_and_replayed(tmp_path, native_fixture, monkeypatch):
    board, anchor, workspace, _adapter, membership, _cli = native_fixture
    controller, store, scope, req = setup(tmp_path, native_fixture)
    try:
        planner = controller.prepare_planner(request_id="acceptance-request")
        released = controller.release_planner(request_id="acceptance-request")
        task = planner["task_id"]
        run_id = _claim(board, task, tmp_path / "home")
        monkeypatch.setenv("HERMES_KANBAN_TASK", task)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
        monkeypatch.setenv("HERMES_SESSION_ID", "controlled-acceptance-session")
        controller.register_planning_request(task, request_id="acceptance-request")
        controller.submit_plan(serialize_proposal(proposal(req)), request_id="acceptance-request")
        plan_id = proposal(req).plan.plan_id
        with pytest.raises(KeyError):
            store.read_accepted_plan(scope, plan_id)
        native_before = store.read_scope(scope)
        result = controller.accept_validated_plan(plan_id, request_id="acceptance-request")
        assert result["active_tranche"] == {"tranche_id": "TR-A", "ordinal": 0}
        assert result["route"] == {"implementation_profile": "implementer", "workspace": workspace}
        assert store.read_accepted_plan(scope, plan_id) == result
        store.close()
        store = EvidenceStore.open(tmp_path / "evidence.sqlite")
        controller.store = store
        membership["store"] = store
        replay = controller.accept_validated_plan(plan_id, request_id="acceptance-request")
        assert replay == result
        after = store.read_scope(scope)
        assert len([event for event in after["budget_events"] if event["event_id"].startswith("paid_capacity:")]) == 1
        assert len([op for op in after["operations"] if op.effect == "accept_validated_plan"]) == 1
        assert len([op for op in after["operations"] if op.effect == "release"]) == 1
        assert native_before["budget_events"] == after["budget_events"]
        intent = next(op for op in after["operations"] if op.effect == "accept_validated_plan")
        payload = intent.to_dict()
        payload["target"]["token"]["proposal_hash"] = "tampered"
        store.connection.execute("UPDATE operation_intents SET intent_json=? WHERE board_id=? AND anchor_task_id=? AND operation_key=?",
            (__import__("json").dumps(payload, sort_keys=True, separators=(",", ":")), scope["board_id"], scope["anchor_task_id"], intent.key))
        with pytest.raises(SchemaError):
            store.read_accepted_plan(scope, plan_id)
    finally:
        store.close()


def test_prepare_active_piece_requires_accepted_tranche_zero_ticket(tmp_path):
    ctl, store, _board, _observed = _pure_acceptance_fixture(tmp_path)
    try:
        ctl.accept_validated_plan("plan-1")
        before = _authority_rows(store)
        with pytest.raises(ValueError, match="ticket must uniquely belong to accepted tranche zero"):
            ctl.prepare_active_piece("plan-1", "future-ticket")
        assert _authority_rows(store) == before
    finally:
        store.close()


def test_recorded_proposal_is_not_accepted_authority(tmp_path):
    path = tmp_path / "evidence.sqlite"
    with EvidenceStore.open(path, create_new=True) as store:
        store.migrate()
        payload = evidence()
        store.record_plan(SCOPE, payload)
        with pytest.raises(KeyError):
            store.read_accepted_plan(SCOPE, "plan-1")


def test_pure_generic_acceptance_apis_cannot_promote_or_write_evidence(tmp_path):
    import dataclasses, json
    from local_first_orchestrator.contracts import ConflictError
    ctl, store, board, observed = _pure_acceptance_fixture(tmp_path)
    try:
        token = ctl.accept_validated_plan("plan-1")
        assert store.read_accepted_plan(SCOPE,"plan-1") == token
        valid = next(op for op in store.read_scope(SCOPE)["operations"] if op.effect == "accept_validated_plan")
        event = dict(store.read_scope(SCOPE)["budget_events"][0])
        event.update(event_id="paid_capacity:" + valid.key, native_source_id=valid.key)
        before = _authority_rows(store)
        with pytest.raises(ConflictError, match="local plan acceptance authority"):
            store.reserve_operation(valid)
        assert _authority_rows(store) == before
        # Use a canonical, reconstructable dedicated receipt, not a malformed
        # lookalike. Only its phase is changed to model a pending generic forgery.
        pending = dataclasses.replace(valid, phase="pending", outcome=None, readback=None)
        store.connection.execute("UPDATE operation_intents SET intent_json=? WHERE operation_key=?",(json.dumps(pending.to_dict(),sort_keys=True,separators=(",",":")),valid.key))
        def snapshot():
            return (_authority_rows(store),tuple(tuple(row) for row in store.connection.execute("SELECT * FROM effect_observations")),tuple(tuple(row) for row in store.connection.execute("SELECT * FROM budget_reconciliation_evidence")))
        before = snapshot()
        calls = (
            lambda: store.begin_effect_attempt(SCOPE,pending.key),
            lambda: store.record_effect_observation(SCOPE,pending.key,outcome="verified",details="forged",readback=valid.readback),
            lambda: store.observe_effect(SCOPE,pending.key,outcome="verified",readback=valid.readback,phase="applied"),
            lambda: store.ack_effect(SCOPE,pending.key,readback=valid.readback),
            lambda: store.reserve_budgeted_repair_operation(pending,event,policy_limit=10),
            lambda: store.reserve_paid_release(SCOPE,pending,event,policy_limit=10),
            lambda: store.record_budget_event(SCOPE,event),
        )
        for call in calls:
            with pytest.raises(ConflictError, match="local plan acceptance authority"):
                call()
            assert snapshot() == before
    finally:
        store.close()


def test_acceptance_authority_roundtrips_after_explicit_writer_and_reopen(tmp_path):
    ctl, store, _board, _observed = _pure_acceptance_fixture(tmp_path)
    path = tmp_path / "accept-pure.sqlite"
    try:
        token = ctl.accept_validated_plan("plan-1")
        assert store.read_accepted_plan(SCOPE, "plan-1") == token
        before = _authority_rows(store)
        store.close()
        reopened = EvidenceStore.open(path)
        try:
            assert reopened.read_accepted_plan(SCOPE, "plan-1") == token
            assert _authority_rows(reopened) == before
        finally:
            reopened.close()
    finally:
        try: store.close()
        except Exception: pass


@pytest.mark.parametrize("corruption", [
    "phase-pending", "phase-unknown", "outcome-invalid", "before-writer", "before-source",
    "proposal-hash", "request-identity", "planner-task", "tranche-ordinal", "route-profile",
    "route-workspace", "acceptance-digest", "readback-token",
])
def test_accepted_plan_reader_rejects_corrupt_receipt_without_writes(tmp_path, corruption):
    import json
    ctl, store, _board, _observed = _pure_acceptance_fixture(tmp_path)
    try:
        token = ctl.accept_validated_plan("plan-1")
        assert store.read_accepted_plan(SCOPE, "plan-1") == token
        op = next(op for op in store.read_scope(SCOPE)["operations"] if op.effect == "accept_validated_plan")
        payload = op.to_dict()
        if corruption.startswith("phase-"): payload["phase"] = corruption.removeprefix("phase-")
        elif corruption == "outcome-invalid": payload["outcome"] = "invalid"
        elif corruption == "before-writer": payload["before_evidence"]["authority_writer"] = "other:v1"
        elif corruption == "before-source": payload["before_evidence"]["source_plan_id"] = "other-plan"
        elif corruption == "readback-token": payload["readback"]["token"]["plan_id"] = "other-plan"
        else:
            key, value = {
                "proposal-hash": ("proposal_hash", "f" * 64),
                "request-identity": ("request_identity", "other-request"),
                "planner-task": ("planner", {**token["planner"], "task_id": "other-task"}),
                "tranche-ordinal": ("active_tranche", {**token["active_tranche"], "ordinal": 1}),
                "route-profile": ("route", {**token["route"], "implementation_profile": "other"}),
                "route-workspace": ("route", {**token["route"], "workspace": "/other/workspace"}),
                "acceptance-digest": ("acceptance_identity", "0" * 64),
            }[corruption]
            payload["target"]["token"][key] = value
            payload["readback"]["token"][key] = value
        store.connection.execute("UPDATE operation_intents SET intent_json=? WHERE board_id=? AND anchor_task_id=? AND operation_key=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), SCOPE["board_id"], SCOPE["anchor_task_id"], op.key))
        before = _authority_rows(store)
        with pytest.raises(SchemaError): store.read_accepted_plan(SCOPE, "plan-1")
        assert _authority_rows(store) == before
    finally: store.close()


def test_reader_rejects_duplicate_and_consistently_aliased_acceptance_keys(tmp_path):
    import json
    ctl, store, _board, _observed = _pure_acceptance_fixture(tmp_path)
    try:
        ctl.accept_validated_plan("plan-1")
        op = next(op for op in store.read_scope(SCOPE)["operations"] if op.effect == "accept_validated_plan")
        raw = store.connection.execute("SELECT intent_json FROM operation_intents WHERE operation_key=?", (op.key,)).fetchone()[0]
        duplicate = "duplicate:" + op.key
        store.connection.execute("INSERT INTO operation_intents VALUES (?,?,?,?)", (*SCOPE.values(), duplicate, raw))
        before = _authority_rows(store)
        with pytest.raises(SchemaError): store.read_accepted_plan(SCOPE, "plan-1")
        assert _authority_rows(store) == before
        store.connection.execute("DELETE FROM operation_intents WHERE operation_key=?", (duplicate,))
        aliased = "accept-plan:" + "f" * 64
        payload = json.loads(raw)
        payload["key"] = aliased
        payload["expected_observed_identity"] = "f" * 64
        store.connection.execute("UPDATE operation_intents SET operation_key=?,intent_json=? WHERE operation_key=?",
            (aliased, json.dumps(payload, sort_keys=True, separators=(",", ":")), op.key))
        before = _authority_rows(store)
        with pytest.raises(SchemaError): store.read_accepted_plan(SCOPE, "plan-1")
        assert _authority_rows(store) == before
    finally: store.close()


def test_exact_replay_with_changed_route_fails_without_new_accounting(tmp_path):
    ctl, store, _board, _observed = _pure_acceptance_fixture(tmp_path)
    try:
        token = ctl.accept_validated_plan("plan-1")
        before = _authority_rows(store)
        changed_workspace = tmp_path / "other-workspace"
        changed_workspace.mkdir()
        ctl.planning_workspace = str(changed_workspace)
        with pytest.raises((ValueError, SchemaError)):
            ctl.accept_validated_plan("plan-1")
        assert store.read_accepted_plan(SCOPE, "plan-1") == token
        assert _authority_rows(store) == before
    finally: store.close()


def _pure_acceptance_fixture(tmp_path):
    import hashlib, json
    from local_first_orchestrator.contracts import BoardSnapshot, ManagedMember, OperationIntent
    from local_first_orchestrator.coordinator import Coordinator
    from local_first_orchestrator.daemon import instance_lock
    from local_first_orchestrator.budgets import BudgetPolicy
    from local_first_orchestrator.decomposition_planner import request_payload
    store = EvidenceStore.open(tmp_path / "accept-pure.sqlite", create_new=True)
    store.migrate()
    req = base_request()
    payload = evidence()
    planner = payload["planner"]
    task = planner["task_id"]
    store.register_member(ManagedMember("board-A", "anchor-A", "anchor-A", "root", 0, (), "root"))
    store.register_member(ManagedMember("board-A", "anchor-A", task, "planner", 0, ("__general_attempt__",), req.identity))
    intent = OperationIntent("pure-release", SCOPE, {"task_id":task,"request_id":req.identity,"member_generation":0,"profile":"default"}, "release", "held-digest", {"task_id":task}, None, None, {}, "pending")
    event = {"event_id":"paid_capacity:pure-release","lineage_id":"anchor-A:__general_attempt__","root_task_id":"anchor-A","finding_id":"__general_attempt__","generation":0,"source_task_id":task,"source_kind":"native_operation","native_source_id":intent.key,"count":1}
    store.reserve_paid_release(SCOPE, intent, event, policy_limit=1)
    marker_payload = json.dumps({"action_key":intent.key,"anchor_task_id":"anchor-A","board_id":"board-A","effect":"release"},sort_keys=True,separators=(",",":"))
    marker = "<!-- local-first-native:v1:sha256:" + hashlib.sha256(marker_payload.encode()).hexdigest() + " -->"
    receipt = BoardSnapshot(native_task={"id":task,"assignee":"default","status":"ready"},parents=(),runs=(),comments=({"body":"UNBLOCK: "+marker},),events=(),attachments=(),observed_at="now",digest="ready-digest")
    store.ack_effect(SCOPE, intent.key, readback=receipt.to_dict())
    run = {"id":planner["run_id"],"task_id":task,"profile":"default","status":"running","started":True}
    store.bind_paid_release_to_native_run(SCOPE,intent.key,task,0,"default",planner["run_id"],planner["session_id"],run)
    store.register_planning_request(SCOPE,req,task,"default")
    store.record_plan(SCOPE,payload)
    class Board:
        is_fake = True
        task_id = task
        observed_run = dict(run)
        def read_scoped_run(self, *args): return dict(self.observed_run)
        def read_task(self, task_id):
            return BoardSnapshot(native_task={"id":self.task_id,"assignee":"default","status":"running"},parents=(),runs=(dict(self.observed_run),),comments=(),events=(),attachments=(),observed_at="now",digest="active-digest")
        def hold(self,*args,**kwargs): raise AssertionError("mutation")
        def release(self,*args,**kwargs): raise AssertionError("mutation")
        def stop_run(self,*args,**kwargs): raise AssertionError("mutation")
    board = Board()
    observed = {"request":request_payload(req)}
    ctl = Coordinator(SCOPE,board=board,store=store,lock=instance_lock(tmp_path / "pure.lock"),budget_policy=BudgetPolicy(2,2,2,2,1),configured_roles={"implementation_profile":"implementer","local_review_profile":"reviewer","planning_profile":"default"},planning_observer=lambda _:observed,planning_profile="default",planning_workspace=str(tmp_path))
    return ctl, store, board, observed


def _authority_rows(store):
    return tuple((table,tuple(tuple(row) for row in store.connection.execute("SELECT * FROM "+table))) for table in ("operation_intents","budget_events","paid_release_run_bindings","plan_proposals"))


def test_pure_acceptance_writer_and_exact_replay(tmp_path):
    ctl, store, board, observed = _pure_acceptance_fixture(tmp_path)
    try:
        before = store.read_scope(SCOPE)["budget_events"]
        token = ctl.accept_validated_plan("plan-1")
        assert store.read_accepted_plan(SCOPE,"plan-1") == token
        state = _authority_rows(store)
        assert ctl.accept_validated_plan("plan-1") == token
        assert _authority_rows(store) == state
        assert store.read_scope(SCOPE)["budget_events"] == before
    finally: store.close()


@pytest.mark.parametrize("change", ["pause","cancel","request","completed","wrong-task","run-task","profile","session","route"])
def test_pure_acceptance_gates_write_nothing(tmp_path, change):
    import dataclasses
    from local_first_orchestrator.contracts import PauseIntent, ConflictError
    from local_first_orchestrator.decomposition_planner import request_payload
    ctl, store, board, observed = _pure_acceptance_fixture(tmp_path)
    try:
        if change in {"pause","cancel"}: store.set_operator_intent(PauseIntent(SCOPE,"operator",1,True,change=="cancel"))
        elif change == "request": observed["request"] = request_payload(dataclasses.replace(base_request(),base_sha="d"*40))
        elif change == "completed": board.observed_run["status"] = "completed"
        elif change == "wrong-task": board.task_id = "substituted-task"
        elif change == "run-task": board.observed_run["task_id"] = "substituted-task"
        elif change == "profile": board.observed_run["profile"] = "other"
        elif change == "session": board.observed_run["metadata"] = {"worker_session_id": "other"}
        elif change == "route": ctl.planning_workspace = "relative"
        before = _authority_rows(store)
        with pytest.raises((ValueError,ConflictError,SchemaError,KeyError)): ctl.accept_validated_plan("plan-1")
        assert _authority_rows(store) == before
    finally: store.close()
