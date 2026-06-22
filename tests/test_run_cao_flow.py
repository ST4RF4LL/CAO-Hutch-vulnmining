import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("run_cao_flow", ROOT / "scripts/run_cao_flow.py")
FLOW = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(FLOW)


class FlowContractTests(unittest.TestCase):
    def test_repository_workflow_is_valid_and_ordered(self):
        workflow = FLOW.load_workflow(ROOT / "workflows/djl-security-review.yaml")
        self.assertEqual(
            [stage["id"] for stage in workflow["stages"]],
            ["architecture", "threat-analysis", "code-audit"],
        )
        self.assertIn("java-injection-review", workflow["stages"][2]["skills"])

    def test_result_requires_declared_artifact_and_headings(self):
        stage = {
            "id": "architecture",
            "task_id": "T-0001",
            "artifact": "artifacts/architecture.md",
            "required_sections": ["Repository and Module Map", "Evidence and Limitations"],
        }
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "artifacts").mkdir()
            (run_dir / "outbox").mkdir()
            (run_dir / stage["artifact"]).write_text(
                "## Repository and Module Map\nmap\n\n## Evidence and Limitations\nlimits\n",
                encoding="utf-8",
            )
            result = {
                "schema": "hutch.result.v1",
                "task_id": "T-0001",
                "stage": "architecture",
                "status": "done",
                "artifacts": ["artifacts/architecture.md"],
            }
            (run_dir / "outbox" / "T-0001.result.json").write_text(
                json.dumps(result), encoding="utf-8"
            )
            self.assertEqual(FLOW.validate_result(stage, run_dir), (True, "validated"))

            (run_dir / stage["artifact"]).write_text(
                "## Repository and Module Map\nmap\n", encoding="utf-8"
            )
            valid, reason = FLOW.validate_result(stage, run_dir)
            self.assertFalse(valid)
            self.assertIn("Evidence and Limitations", reason)

    def test_stage_launches_from_agent_cell_workspace(self):
        stage = {
            "id": "audit",
            "task_id": "T-1",
            "profile": "audit-profile",
            "artifact": "artifacts/audit.md",
            "required_sections": ["Findings"],
            "depends_on": [],
        }

        class Runtime:
            def __init__(self, run_dir):
                self.run_dir = run_dir
                self.working_directory = None

            def launch(self, profile, provider, session, working_directory, message):
                self.working_directory = working_directory
                (self.run_dir / "artifacts/audit.md").write_text(
                    "## Findings\nnone\n", encoding="utf-8"
                )
                (self.run_dir / "outbox/T-1.result.json").write_text(
                    json.dumps(
                        {
                            "schema": "hutch.result.v1",
                            "task_id": "T-1",
                            "stage": "audit",
                            "status": "done",
                            "artifacts": ["artifacts/audit.md"],
                        }
                    ),
                    encoding="utf-8",
                )
                return "cao-test"

            def status(self, session):
                return "{}"

            def shutdown(self, session):
                return None

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            for name in ("artifacts", "inbox", "outbox", "tmp"):
                (run_dir / name).mkdir()
            task = run_dir / "inbox/T-1.task.json"
            task.write_text("{}", encoding="utf-8")
            workspace = run_dir / "agents/audit/workspace"
            workspace.mkdir(parents=True)
            state = {
                "status": "prepared",
                "stages": {"audit": {"status": "pending", "workspace": str(workspace)}},
            }
            runtime = Runtime(run_dir)
            FLOW.run_stage(runtime, stage, "opencode_cli", run_dir, state, 1, 0.01)
            self.assertEqual(runtime.working_directory, workspace)


if __name__ == "__main__":
    unittest.main()
