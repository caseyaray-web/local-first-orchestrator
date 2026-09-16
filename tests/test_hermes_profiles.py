from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest import mock
import unittest

from local_first_orchestrator.hermes_profiles import discover_profiles, list_profile_names, resolve_registration, review_profile_identity, show_profile


class HermesProfileDiscoveryTests(unittest.TestCase):
    def runner(self, argv, **kwargs):
        args = tuple(argv)
        if args[-2:] == ("profile", "list"):
            return subprocess.CompletedProcess(
                args,
                0,
                """
 Profile          Model                        Gateway      Alias        Distribution
 ───────────────    ───────────────────────────    ───────────    ───────────    ────────────────────
 ◆default         gpt-5.6-luna                 running      —            —
  worker-architect-sol gpt-5.6-sol                  stopped      worker-architect-sol —
  worker-code-local qwen3.8-27b@iq3_s            stopped      worker-code-local —

⚠ Profile 'worker-code-local' shares its telegram credential with default: the bot can only belong to one profile.
""",
                "",
            )
        if args[-3:] == ("profile", "show", "default"):
            return subprocess.CompletedProcess(args, 0, "Profile: default\nModel:   gpt-5.6-luna (openai-codex)\nGateway: running\n", "")
        if args[-3:] == ("profile", "show", "worker-architect-sol"):
            return subprocess.CompletedProcess(args, 0, "Profile: worker-architect-sol\nModel:   gpt-5.6-sol (openai-codex)\nGateway: stopped\nAlias:   worker-architect-sol → hermes -p worker-architect-sol\n", "")
        if args[-3:] == ("profile", "show", "worker-code-local"):
            return subprocess.CompletedProcess(args, 0, "Profile: worker-code-local\nModel:   qwen3.8-27b@iq3_s (custom:lm-studio)\nGateway: stopped\n", "")
        raise AssertionError(args)

    def test_discovers_profile_names_and_resolves_provenance(self) -> None:
        self.assertEqual(list_profile_names(runner=self.runner), ("default", "worker-architect-sol", "worker-code-local"))
        profile = show_profile("worker-architect-sol", runner=self.runner)
        self.assertEqual((profile.profile, profile.provider, profile.model), ("worker-architect-sol", "openai-codex", "gpt-5.6-sol"))
        self.assertEqual(profile.alias, "worker-architect-sol")
        local = resolve_registration("worker-code-local", runner=self.runner)
        self.assertEqual((local.profile, local.provider, local.model), ("worker-code-local", "custom:lm-studio", "qwen3.8-27b@iq3_s"))
        self.assertEqual(len(discover_profiles(runner=self.runner)), 3)

    def test_review_profile_identity_binds_live_routing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            profile = home / ".hermes" / "profiles" / "worker-code-local"
            profile.mkdir(parents=True)
            (profile / "profile.yaml").write_text("name: worker-code-local\n", encoding="utf-8")
            config = profile / "config.yaml"
            config.write_text("model: first\n", encoding="utf-8")
            with mock.patch("local_first_orchestrator.hermes_profiles.Path.home", return_value=home):
                first = review_profile_identity("worker-code-local", runner=self.runner)
                config.write_text("model: second\n", encoding="utf-8")
                second = review_profile_identity("worker-code-local", runner=self.runner)
            self.assertEqual(first["profile"], "worker-code-local")
            self.assertEqual(first["provider"], "custom:lm-studio")
            self.assertNotEqual(first["fingerprint"], second["fingerprint"])

    def test_rejects_unparseable_profile_metadata(self) -> None:
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "Profile: broken\nModel: missing-provider\n", "")
        with self.assertRaisesRegex(RuntimeError, "model/provider"):
            show_profile("broken", runner=runner)


if __name__ == "__main__":
    unittest.main()
