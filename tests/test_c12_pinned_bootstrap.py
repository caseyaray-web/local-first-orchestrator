from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import tarfile
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

BOOTSTRAP = Path(__file__).resolve().parents[1] / "scripts" / "c12r1-tk-3-bootstrap.py"
spec = importlib.util.spec_from_file_location("c12_bootstrap_test", BOOTSTRAP)
assert spec is not None and spec.loader is not None
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class PinnedBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"; self.repo.mkdir()
        subprocess.run(("git", "init", "-q"), cwd=self.repo, check=True)
        package = self.repo / "local_first_orchestrator"; package.mkdir()
        (package / "__init__.py").write_text("APPROVED = True\n")
        scripts = self.repo / "scripts"; scripts.mkdir()
        template = Path(__file__).resolve().parents[1] / "scripts" / "c12r1-tk-3-root-launcher.sh.in"
        (scripts / template.name).write_bytes(template.read_bytes())
        subprocess.run(("git", "add", "."), cwd=self.repo, check=True)
        subprocess.run(("git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "approved"), cwd=self.repo, check=True)
        self.approved = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=self.repo, text=True).strip()
        self.bundle = self.root / "bundle"

    def build(self):
        return bootstrap.build_bundle(self.repo, self.approved, self.bundle,
            ledger=self.root / "ledger.db", config=self.root / "operator.json",
            key=self.root / "private" / "key", exchange=self.root / "exchange",
            python_executable=Path("/usr/bin/python3").resolve())

    def test_pins_source_to_commit_despite_changed_worktree_and_ref(self):
        (self.repo / "local_first_orchestrator" / "__init__.py").write_text("APPROVED = False\n")
        subprocess.run(("git", "add", "."), cwd=self.repo, check=True)
        subprocess.run(("git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "malicious"), cwd=self.repo, check=True)
        self.build()
        self.assertEqual((self.bundle / "source" / "local_first_orchestrator" / "__init__.py").read_text(), "APPROVED = True\n")
        manifest = json.loads((self.bundle / "runtime.json").read_text())
        self.assertEqual(manifest["source_root"], str(self.bundle / "source"))
        self.assertEqual(manifest["python_executable"], str(Path("/usr/bin/python3").resolve()))
        self.assertTrue((self.bundle / "launcher").is_file())
        self.assertEqual((self.bundle / "source" / "local_first_orchestrator" / "__init__.py").stat().st_mode & 0o222, 0)
        with self.assertRaises(FileExistsError):
            self.build()

    def test_rejects_archive_symlink_without_publishing(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as stream:
            item = tarfile.TarInfo("local_first_orchestrator/__init__.py")
            item.type = tarfile.SYMTYPE; item.linkname = "/etc/passwd"
            stream.addfile(item)
        archive.seek(0)
        with self.assertRaises(ValueError):
            bootstrap.extract_checked_archive(archive, self.root / "unpublished")
        self.assertFalse((self.root / "unpublished" / "local_first_orchestrator" / "__init__.py").exists())

    def test_rejects_abbreviated_commit_without_publishing(self):
        with self.assertRaises(ValueError):
            bootstrap.build_bundle(self.repo, self.approved[:12], self.bundle,
                ledger=self.root / "ledger.db", config=self.root / "operator.json", key=self.root / "key",
                exchange=self.root / "exchange", python_executable=Path("/usr/bin/python3").resolve())
        self.assertFalse(self.bundle.exists())

    def test_replacement_ref_cannot_override_approved_commit(self):
        (self.repo / "local_first_orchestrator" / "__init__.py").write_text("APPROVED = False\n")
        subprocess.run(("git", "add", "."), cwd=self.repo, check=True)
        subprocess.run(("git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "malicious"), cwd=self.repo, check=True)
        newer = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=self.repo, text=True).strip()
        subprocess.run(("git", "replace", self.approved, newer), cwd=self.repo, check=True)
        self.build()
        self.assertEqual((self.bundle / "source" / "local_first_orchestrator" / "__init__.py").read_text(), "APPROVED = True\n")

    def test_bad_archive_never_publishes_and_removes_private_assembly(self):
        for member_name, member_type in (("../escape", tarfile.REGTYPE),
                                         ("local_first_orchestrator/evil", tarfile.SYMTYPE),
                                         ("local_first_orchestrator/evil", tarfile.FIFOTYPE)):
            with self.subTest(name=member_name, member_type=member_type):
                archive = io.BytesIO()
                with tarfile.open(fileobj=archive, mode="w") as stream:
                    info = tarfile.TarInfo(member_name); info.type = member_type
                    stream.addfile(info)
                fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=archive.getvalue(), stderr=b"")
                with mock.patch.object(bootstrap.subprocess, "run", return_value=fake):
                    with self.assertRaises(ValueError):
                        self.build()
                self.assertFalse(self.bundle.exists())
                self.assertFalse(list(self.root.glob(".c12-assembly-*")))

    def test_publish_failure_never_exposes_bundle(self):
        with mock.patch.object(bootstrap.os, "rename", side_effect=OSError("injected rename failure")):
            with self.assertRaisesRegex(OSError, "injected rename failure"):
                self.build()
        self.assertFalse(self.bundle.exists())
        self.assertFalse(list(self.root.glob(".c12-assembly-*")))

    def test_runtime_manifest_cannot_redirect_helper_source_or_interpreter(self):
        from local_first_orchestrator.keyless_human_recovery import _parse_installed_runtime
        runtime = {"ledger_path": str(self.root / "ledger.db"), "config_path": str(self.root / "operator.json"),
            "source_root": str(self.bundle / "source"), "python_executable": str(Path("/usr/bin/python3").resolve()),
            "key_path": str(self.root / "private" / "key"), "exchange_parent": str(self.root / "exchange")}
        self.assertEqual(_parse_installed_runtime(runtime, bundle_root=self.bundle).installed_source.source_root, self.bundle / "source")
        for name, value in (("source_root", str(self.root / "attacker")), ("python_executable", str(self.root / "python"))):
            with self.subTest(name=name):
                with self.assertRaises(PermissionError):
                    _parse_installed_runtime({**runtime, name: value}, bundle_root=self.bundle)
