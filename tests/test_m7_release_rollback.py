from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from local_first_orchestrator.contracts import ManagedMember, PauseIntent
from local_first_orchestrator.evidence_store import EvidenceStore
from tests.fixtures.m7_rollback import (
    RollbackArchiveError,
    build_fixture_rollback_archive,
    restore_fixture_rollback_archive,
)


PLUGIN_FILES = (
    "plugin.yaml",
    "__init__.py",
    "dashboard/manifest.json",
    "dashboard/plugin_api.py",
    "dashboard/dist/index.js",
    "python/local_first_orchestrator/__init__.py",
)


def _checkpoint() -> dict[str, object]:
    return {
        "version": 1,
        "scope": {"board_id": "fixture-board", "anchor_task_id": "fixture-anchor"},
        "cutoff": "pre-activation",
        "native_effects": "none",
        "installed_plugin_snapshot": "not-captured-fixture-only",
        "operator_intent": "active-paused",
        "restore_policy": "fixture-only-no-native-board-state",
    }


def _fixture_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "fixture-root"
    root.mkdir(mode=0o700)
    plugin = root / "plugin-source"
    for relative in PLUGIN_FILES:
        path = plugin / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture artifact: {relative}\n", encoding="utf-8")
    config = root / "trusted/local-first.json"
    config.parent.mkdir(mode=0o700)
    config.write_text(json.dumps({"fixture": "trusted-bootstrap", "scope": _checkpoint()["scope"]}), encoding="utf-8")
    state = root / "state"
    state.mkdir(mode=0o700)
    evidence = state / "evidence.sqlite3"
    with EvidenceStore.open(evidence, create_new=True) as store:
        store.migrate()
        store.set_operator_intent(PauseIntent(_checkpoint()["scope"], "operator", 1, True, True))
        store.register_member(ManagedMember("fixture-board", "fixture-anchor", "fixture-implementation", "implementation", 0, (), "fixture-work"))
        store.record_budget_event(
            _checkpoint()["scope"],
            {
                "event_id": "implementation_attempts:fixture-run",
                "lineage_id": "fixture-anchor:__general_attempt__",
                "root_task_id": "fixture-anchor",
                "finding_id": "__general_attempt__",
                "generation": 0,
                "source_task_id": "fixture-implementation",
                "source_kind": "native_run",
                "native_source_id": "fixture-run",
                "count": 1,
            },
        )
        assert store.read_scope(_checkpoint()["scope"])["operator_intent"].active is True
    return root, plugin, config


def test_m7_fixture_rollback_archive_round_trip_and_refuses_bad_checkpoints(tmp_path: Path) -> None:
    root, plugin, config = _fixture_inputs(tmp_path)
    evidence = root / "state/evidence.sqlite3"
    checkpoint = _checkpoint()
    built = build_fixture_rollback_archive(
        fixture_root=root,
        plugin_root=plugin,
        plugin_files=PLUGIN_FILES,
        config_path=config,
        evidence_path=evidence,
        checkpoint=checkpoint,
        archive_path=root / "rollback/m7-pre-activation.tar.gz",
    )
    restored = restore_fixture_rollback_archive(
        fixture_root=root,
        archive_path=Path(built["archive"]),
        destination=root / "restore",
        expected_checkpoint=checkpoint,
    )

    assert restored["archive_sha256"] == built["archive_sha256"]
    assert (root / "restore/config/local-first.json").read_bytes() == config.read_bytes()
    for relative in PLUGIN_FILES:
        assert (root / "restore/plugin" / relative).read_bytes() == (plugin / relative).read_bytes()
    with EvidenceStore.open(root / "restore/state/evidence.sqlite3", create_new=False) as store:
        store.migrate()
        scope = store.read_scope(checkpoint["scope"])
    assert scope["operator_intent"].active is True
    assert scope["operator_intent"].stop_requested is True
    assert [member.task_id for member in scope["members"]] == ["fixture-implementation"]
    assert scope["budget_events"] == ({
        "event_id": "implementation_attempts:fixture-run",
        "lineage_id": "fixture-anchor:__general_attempt__",
        "root_task_id": "fixture-anchor",
        "finding_id": "__general_attempt__",
        "generation": 0,
        "source_task_id": "fixture-implementation",
        "source_kind": "native_run",
        "native_source_id": "fixture-run",
        "count": 1,
    },)

    with pytest.raises(RollbackArchiveError, match="closed M7 fixture schema"):
        build_fixture_rollback_archive(
            fixture_root=root, plugin_root=plugin, plugin_files=PLUGIN_FILES, config_path=config,
            evidence_path=evidence, checkpoint={**checkpoint, "unexpected": "no"},
            archive_path=root / "rollback/unknown.tar.gz",
        )
    with pytest.raises(RollbackArchiveError, match="closed M7 fixture schema"):
        restore_fixture_rollback_archive(
            fixture_root=root, archive_path=Path(built["archive"]), destination=root / "missing",
            expected_checkpoint={key: value for key, value in checkpoint.items() if key != "cutoff"},
        )
    with pytest.raises(RollbackArchiveError, match="does not match"):
        restore_fixture_rollback_archive(
            fixture_root=root, archive_path=Path(built["archive"]), destination=root / "mismatch",
            expected_checkpoint={**checkpoint, "cutoff": "post-effect"},
        )


@pytest.mark.parametrize(
    ("allowlist", "path_kind"),
    [
        (("plugin.yaml", "plugin.yaml"), "duplicate"),
        (("../plugin.yaml",), "traversal"),
        (("plugin.yaml",), "config-symlink"),
        (("dashboard/manifest.json",), "allowlist-symlink"),
        (("dashboard/manifest.json",), "ancestor-symlink"),
    ],
)
def test_m7_fixture_archive_rejects_allowlist_and_symlink_boundaries(
    tmp_path: Path, allowlist: tuple[str, ...], path_kind: str,
) -> None:
    root, plugin, config = _fixture_inputs(tmp_path)
    evidence = root / "state/evidence.sqlite3"
    if path_kind == "config-symlink":
        outside = tmp_path / "outside-config.json"
        outside.write_text("outside", encoding="utf-8")
        config.unlink()
        config.symlink_to(outside)
    elif path_kind == "allowlist-symlink":
        target = plugin / "dashboard/manifest-target.json"
        target.write_text("fixture target", encoding="utf-8")
        (plugin / "dashboard/manifest.json").unlink()
        (plugin / "dashboard/manifest.json").symlink_to(target.name)
    elif path_kind == "ancestor-symlink":
        target = root / "dashboard-target"
        (plugin / "dashboard").rename(target)
        (plugin / "dashboard").symlink_to(target, target_is_directory=True)

    with pytest.raises(RollbackArchiveError, match="unsafe or duplicate|must not be a symlink"):
        build_fixture_rollback_archive(
            fixture_root=root, plugin_root=plugin, plugin_files=allowlist,
            config_path=config, evidence_path=evidence, checkpoint=_checkpoint(),
            archive_path=root / f"rollback/{path_kind}.tar.gz",
        )


def test_m7_fixture_restore_rejects_duplicate_member_inventory(tmp_path: Path) -> None:
    root, plugin, config = _fixture_inputs(tmp_path)
    built = build_fixture_rollback_archive(
        fixture_root=root, plugin_root=plugin, plugin_files=PLUGIN_FILES, config_path=config,
        evidence_path=root / "state/evidence.sqlite3", checkpoint=_checkpoint(),
        archive_path=root / "rollback/original.tar.gz",
    )
    duplicate = root / "rollback/duplicate-member.tar.gz"
    with tarfile.open(Path(built["archive"]), "r:gz") as source, tarfile.open(duplicate, "x:gz") as target:
        members = source.getmembers()
        for member in members:
            payload = source.extractfile(member)
            assert payload is not None
            target.addfile(member, payload)
        repeated = source.getmember("config/local-first.json")
        payload = source.extractfile(repeated)
        assert payload is not None
        target.addfile(repeated, payload)

    with pytest.raises(RollbackArchiveError, match="duplicate members"):
        restore_fixture_rollback_archive(
            fixture_root=root, archive_path=duplicate, destination=root / "duplicate-restore",
            expected_checkpoint=_checkpoint(),
        )
