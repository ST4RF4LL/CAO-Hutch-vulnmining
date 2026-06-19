import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


GENERATOR = load_script("generate_cao_native_flow")
STATE = load_script("hutch_flow_state")


class CaoNativeFlowTests(unittest.TestCase):
    def test_repository_workflow_compiles_to_native_cao_bundle(self):
        workflow_path = ROOT / "workflows/djl-vulnerability-mining.yaml"
        workflow = GENERATOR.load_and_validate(workflow_path)
        self.assertEqual(
            [stage["id"] for stage in workflow["stages"]],
            [
                "architecture",
                "threat-analysis",
                "java-audit",
                "native-audit",
                "supplychain-audit",
                "finding-validation",
                "final-report",
            ],
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "bundle"
            manifest = GENERATOR.write_output(
                workflow_path,
                workflow,
                output,
                Path(workflow["cao_repo"]),
            )
            flow = Path(manifest["flow"]).read_text(encoding="utf-8")
            self.assertIn("name: djl-vulnerability-mining", flow)
            self.assertIn("script: ./prepare-run.sh", flow)
            self.assertIn("CAO MCP `assign`", flow)
            self.assertEqual(len(manifest["profiles"]), 8)
            for profile in manifest["profiles"]:
                text = Path(profile).read_text(encoding="utf-8")
                self.assertTrue(text.startswith("---\nname:"))
                self.assertIn("\n---\n", text)

    def test_stage_validation_advances_only_valid_contract(self):
        stage = {
            "id": "audit",
            "task_id": "T-1",
            "depends_on": [],
            "artifact": "artifacts/audit.md",
            "required_sections": ["Findings"],
        }
        finding = {
            "id": "DJL-CAND-1",
            "title": "candidate",
            "severity": "high",
            "confidence": "medium",
            "status": "candidate",
            "weakness": "CWE-20",
            "evidence": [{"path": "A.java", "line": 1, "symbol": "a", "observation": "x"}],
            "impact": "impact",
            "assumptions": ["assumption"],
        }
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "artifacts").mkdir()
            (run_dir / "outbox").mkdir()
            (run_dir / "workflow.json").write_text(
                json.dumps({"stages": [stage]}), encoding="utf-8"
            )
            (run_dir / "state.json").write_text(
                json.dumps({"status": "prepared", "stages": {"audit": {"status": "pending"}}}),
                encoding="utf-8",
            )
            (run_dir / "events.jsonl").write_text("", encoding="utf-8")
            (run_dir / "artifacts/audit.md").write_text("## Findings\nproof\n", encoding="utf-8")
            (run_dir / "outbox/T-1.result.json").write_text(
                json.dumps(
                    {
                        "schema": "hutch.result.v1",
                        "task_id": "T-1",
                        "stage": "audit",
                        "status": "done",
                        "artifacts": ["artifacts/audit.md"],
                        "findings": [finding],
                    }
                ),
                encoding="utf-8",
            )
            result = STATE.validate_stage(run_dir, "audit")
            self.assertTrue(result["ok"])
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["stages"]["audit"]["status"], "done")


if __name__ == "__main__":
    unittest.main()
