import argparse
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import hutch_deploy as DEPLOY


class HutchDeployTests(unittest.TestCase):
    def setUp(self):
        self._env = {
            "HUTCH_HOME": os.environ.get("HUTCH_HOME"),
            "HUTCH_AGENTS_STORE": os.environ.get("HUTCH_AGENTS_STORE"),
            "HUTCH_FLOWS_STORE": os.environ.get("HUTCH_FLOWS_STORE"),
            "CAO_REPO": os.environ.get("CAO_REPO"),
        }

    def tearDown(self):
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def args(self, root: Path) -> argparse.Namespace:
        cao_repo = root / "cao"
        cao_repo.mkdir()
        (cao_repo / "pyproject.toml").write_text("[project]\nname = 'cao'\n")
        return argparse.Namespace(
            hutch_home=str(root / ".hutch"),
            agents_store=None,
            flows_store=None,
            cao_repo=str(cao_repo),
            cao_host="127.0.0.1",
            cao_port=9889,
            host="127.0.0.1",
            dashboard_port=9890,
        )

    def test_init_runtime_copies_missing_default_stores(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.args(root)
            DEPLOY.configure_environment(args)

            first = DEPLOY.init_runtime(args)
            second = DEPLOY.init_runtime(args)

            self.assertEqual(first["agents_store"]["status"], "copied")
            self.assertEqual(first["flows_store"]["status"], "copied")
            self.assertEqual(second["agents_store"]["status"], "exists")
            self.assertEqual(second["flows_store"]["status"], "exists")
            self.assertTrue((root / ".hutch/agents_store/recon-planner").is_dir())
            self.assertTrue((root / ".hutch/flows_store/one-run/flow.json").is_file())

    def test_init_runtime_accepts_explicit_store_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.args(root)
            args.agents_store = str(root / "custom-agents")
            args.flows_store = str(root / "custom-flows")
            DEPLOY.configure_environment(args)

            value = DEPLOY.init_runtime(args)

            self.assertEqual(value["agents_store"]["status"], "copied")
            self.assertEqual(value["flows_store"]["status"], "copied")
            self.assertTrue((root / "custom-agents/recon-planner").is_dir())
            self.assertTrue((root / "custom-flows/one-run/flow.json").is_file())

    def test_init_runtime_trusts_agent_store_role_directories_for_codex(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.args(root)
            DEPLOY.configure_environment(args)

            with mock.patch.object(DEPLOY.Path, "home", return_value=root):
                value = DEPLOY.init_runtime(args)
                second = DEPLOY.init_runtime(args)

            config = (root / ".codex/config.toml").read_text(encoding="utf-8")
            supervisor = (root / ".hutch/agents_store/flow-supervisor").resolve()
            recon = (root / ".hutch/agents_store/recon-planner").resolve()
            java = (root / ".hutch/agents_store/java-auditor").resolve()
            self.assertIn(f'[projects."{supervisor}"]', config)
            self.assertIn(f'[projects."{recon}"]', config)
            self.assertIn(f'[projects."{java}"]', config)
            self.assertIn('trust_level = "trusted"', config)
            self.assertIn(str(recon), value["codex_trust"]["added"])
            self.assertEqual(second["codex_trust"]["added"], [])


if __name__ == "__main__":
    unittest.main()
