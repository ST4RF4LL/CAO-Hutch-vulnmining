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


if __name__ == "__main__":
    unittest.main()
