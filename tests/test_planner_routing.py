import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from local_first_orchestrator.decomposition_planner import LocalDecompositionPlanner
from local_first_orchestrator.operator_config import ModelRegistration, OperatorConfig, load_operator_config, save_operator_config


class PlannerRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        subprocess.run(("git", "init", "-q"), cwd=self.repo, check=True)
        self.ledger = root / "ledger.db"
        self.ledger.touch()
        self.config_path = root / "operator-config.json"
        self.base = dict(
            ledger_path=self.ledger,
            canonical_repository=self.repo,
            repository_allowlist=(self.repo,),
            implementation=ModelRegistration("impl-profile", "impl-provider", "implementation-model"),
            review=ModelRegistration("review-profile", "review-provider", "review-model"),
            worktree_root=root / "worktrees",
            artifact_root=root / "artifacts",
            implementation_timeout_seconds=1800,
            review_timeout_seconds=900,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, routes):
        return OperatorConfig(**self.base, decomposition=tuple(sorted(routes.items())))

    def test_decomposition_route_is_separate_from_implementation_and_review(self):
        cfg = self.config({"local": ModelRegistration("planner-profile", "planner-provider", "planner-model")})
        save_operator_config(cfg, self.config_path)
        loaded = load_operator_config(self.config_path)
        self.assertEqual(loaded.decomposition_route("local").model, "planner-model")
        self.assertEqual(loaded.implementation.model, "implementation-model")
        self.assertEqual(loaded.review.model, "review-model")

    def test_local_cost_class_selects_within_decomposition_role(self):
        cfg = self.config({"local": ModelRegistration("planner-profile", "planner-provider", "planner-model")})
        route = cfg.decomposition_route("local")
        planner = LocalDecompositionPlanner(cost_class="local", provider=route.provider, model=route.model, profile=route.profile, role="decomposition", routing_source="operator-config.decomposition")
        self.assertEqual((planner.role, planner.provider, planner.model, planner.profile), ("decomposition", "planner-provider", "planner-model", "planner-profile"))

    def test_missing_planner_route_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "not configured"):
            self.config({}).decomposition_route("local")

    def test_unknown_cost_class_route_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "not configured"):
            self.config({"standard": ModelRegistration("p", "q", "m")}).decomposition_route("local")

    def test_invalid_route_shape_fails_closed(self):
        raw = dict(self.base)
        raw["decomposition"] = {"local": {"profile": "p", "provider": "q"}}
        self.config_path.write_text(json.dumps({"ledger_path": str(self.ledger), "canonical_repository": str(self.repo), "repository_allowlist": [str(self.repo)], "implementation": {"profile": "i", "provider": "ip", "model": "im"}, "review": {"profile": "r", "provider": "rp", "model": "rm"}, "worktree_root": str(raw["worktree_root"]), "artifact_root": str(raw["artifact_root"]), "implementation_timeout_seconds": 1800, "review_timeout_seconds": 900, "decomposition": raw["decomposition"]}))
        with self.assertRaises(ValueError):
            load_operator_config(self.config_path)

    def test_provenance_contains_role_and_routing_source(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        planner = LocalDecompositionPlanner(runner, cost_class="local", provider="planner-provider", model="planner-model", profile="planner-profile", role="decomposition", routing_source="fixture-route")
        self.assertEqual(planner.role, "decomposition")
        self.assertEqual(planner.routing_source, "fixture-route")

    def test_old_configs_remain_readable_without_implicit_planner_route(self):
        cfg = self.config({})
        save_operator_config(cfg, self.config_path)
        loaded = load_operator_config(self.config_path)
        self.assertEqual(loaded.decomposition, ())
        with self.assertRaises(ValueError):
            loaded.decomposition_route("local")

    def test_multiple_cost_class_routes_are_distinguishable(self):
        cfg = self.config({"local": ModelRegistration("lp", "lprov", "local-planner"), "standard": ModelRegistration("sp", "sprov", "standard-planner")})
        self.assertEqual(cfg.decomposition_route("local").model, "local-planner")
        self.assertEqual(cfg.decomposition_route("standard").model, "standard-planner")


if __name__ == "__main__":
    unittest.main()
