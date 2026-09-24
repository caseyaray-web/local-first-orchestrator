from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


REPOSITORY = Path(__file__).resolve().parents[1]
TEMPLATE = REPOSITORY / "scripts" / "c12r1-tk-3-root-launcher.sh.in"
STAGER = REPOSITORY / "scripts" / "c12r1-tk-3-stage-install.sh"
INSTALLER = REPOSITORY / "scripts" / "c12r1-tk-3-root-install.sh"


class ProtectedLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "snapshot"
        package = self.source / "local_first_orchestrator"
        package.mkdir(parents=True)
        (package / "keyless_human_recovery.py").write_text("# reviewed payload\n", encoding="utf-8")
        self.source_manifest = self.root / "source.sha256"
        digest = hashlib.sha256((package / "keyless_human_recovery.py").read_bytes()).hexdigest()
        self.source_manifest.write_text(f"{digest}  local_first_orchestrator/keyless_human_recovery.py\n", encoding="ascii")
        self.runtime_manifest = self.root / "runtime.json"
        self.runtime_manifest.write_text('{"fixed":"manifest"}\n', encoding="utf-8")
        self.called = self.root / "python-called"
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self._write_tool("id", "#!/bin/sh\nprintf '0\\n'\n")
        self._write_tool("stat", "#!/bin/sh\nfor value do last=$value; done\ncase $last in *runtime.json) printf '0:600:regular file\\n' ;; *python3) printf '0:755:regular file\\n' ;; *) if [ -d \"$last\" ]; then printf '0:555:directory\\n'; else printf '0:444:regular file\\n'; fi ;; esac\n")
        self._write_tool("python3", "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$C12_TEST_CALLED\"\n")

    def _write_tool(self, name: str, text: str) -> Path:
        path = self.tools / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        return path

    def _launcher(self, *, expected_source_hash: str | None = None) -> Path:
        rendered = TEMPLATE.read_text(encoding="utf-8")
        values = {
            "@SOURCE_ROOT@": str(self.source),
            "@SOURCE_MANIFEST@": str(self.source_manifest),
            "@SOURCE_MANIFEST_SHA256@": expected_source_hash or hashlib.sha256(self.source_manifest.read_bytes()).hexdigest(),
            "@RUNTIME_MANIFEST@": str(self.runtime_manifest),
            "@RUNTIME_MANIFEST_SHA256@": hashlib.sha256(self.runtime_manifest.read_bytes()).hexdigest(),
            "@PYTHON_BIN@": str(self.tools / "python3"),
            "@PYTHON_SHA256@": hashlib.sha256((self.tools / "python3").read_bytes()).hexdigest(),
            "@PYTHON_MODE@": "755",
            "@ID_BIN@": str(self.tools / "id"),
            "@STAT_BIN@": str(self.tools / "stat"),
            "@SHA256SUM_BIN@": shutil.which("sha256sum") or "/usr/bin/sha256sum",
            "@GREP_BIN@": shutil.which("grep") or "/usr/bin/grep",
        }
        for old, new in values.items():
            rendered = rendered.replace(old, new)
        launcher = self.root / "launcher"
        launcher.write_text(rendered, encoding="utf-8")
        launcher.chmod(0o755)
        return launcher

    def _run(self, launcher: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(("/bin/sh", str(launcher)), text=True, capture_output=True,
            env={"PATH": "/usr/bin:/bin", "C12_TEST_CALLED": str(self.called)})

    def test_template_refuses_to_import_until_fixed_snapshot_and_manifests_validate(self) -> None:
        launcher = self._launcher()
        result = self._run(launcher)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.called.exists())
        invocation = self.called.read_text(encoding="utf-8")
        self.assertIn("-I", invocation)
        self.assertIn("keyless_human_recovery", invocation)
        self.assertNotIn("--", invocation)

    def test_root_owned_0755_ancestors_are_accepted(self) -> None:
        self._write_tool("stat", "#!/bin/sh\nfor value do last=$value; done\ncase $last in *runtime.json) printf '0:600:regular file\\n' ;; *python3) printf '0:755:regular file\\n' ;; *snapshot) printf '0:555:directory\\n' ;; *) if [ -d \"$last\" ]; then printf '0:755:directory\\n'; else printf '0:444:regular file\\n'; fi ;; esac\n")
        result = self._run(self._launcher())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.called.exists())

    def test_runtime_manifest_hash_failure_prevents_python_execution(self) -> None:
        launcher = self._launcher()
        self.runtime_manifest.write_text('{"tampered":true}\n', encoding="utf-8")
        result = self._run(launcher)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runtime manifest digest mismatch", result.stderr)
        self.assertFalse(self.called.exists())

    def test_source_file_hash_failure_prevents_python_execution(self) -> None:
        launcher = self._launcher()
        (self.source / "local_first_orchestrator" / "keyless_human_recovery.py").write_text("# tampered\n", encoding="utf-8")
        result = self._run(launcher)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source file digest mismatch", result.stderr)
        self.assertFalse(self.called.exists())

    def test_owner_or_mode_failure_prevents_python_execution(self) -> None:
        launcher = self._launcher()
        self._write_tool("stat", "#!/bin/sh\nprintf '1000:777:regular file\\n'\n")
        result = self._run(launcher)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owner/mode/type mismatch", result.stderr)
        self.assertFalse(self.called.exists())

    def test_staging_and_root_installer_are_shell_only_and_do_not_accept_runtime_source_arguments(self) -> None:
        for path in (STAGER, INSTALLER, TEMPLATE):
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("#!/bin/sh"))
        for path in (STAGER, INSTALLER):
            self.assertNotIn("python -c", path.read_text(encoding="utf-8"))
        self.assertNotIn('rm -rf -- "$DESTINATION"\ninstall -d', INSTALLER.read_text(encoding="utf-8"))
        self.assertIn('mkdir -m 0700 -- "$DESTINATION"', INSTALLER.read_text(encoding="utf-8"))
        template = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("exec \"$PYTHON_BIN\" -I -c", template)
        self.assertIn("runtime manifest digest mismatch", template)
        self.assertIn("source file digest mismatch", template)
        self.assertIn("expected no launcher arguments", template)


if __name__ == "__main__":
    unittest.main()
