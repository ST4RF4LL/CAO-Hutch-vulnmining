import importlib.util
import json
import os
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
CELLS = load_script("agent_cells")
ASSIGN = load_script("cao_assign_cell")


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
            self.assertIn("scripts/cao_assign_cell.py", flow)
            self.assertIn("Do not substitute CAO MCP `assign`", flow)
            self.assertIn(
                "workspace `[[run_dir]]/agents/java-auditor/workspace`", flow
            )
            self.assertNotIn('working_directory="[[run_dir]]"', flow)
            self.assertEqual(len(manifest["profiles"]), 8)
            for profile in manifest["profiles"]:
                text = Path(profile).read_text(encoding="utf-8")
                self.assertTrue(text.startswith("---\nname:"))
                self.assertIn("\n---\n", text)
            java_profile = output / "profiles/djl-vulnerability-mining-java-auditor.md"
            self.assertIn(
                "`java-auditor-java-injection-review`",
                java_profile.read_text(encoding="utf-8"),
            )
            self.assertIn("\nmcpServers:\n", java_profile.read_text(encoding="utf-8"))
            self.assertNotIn("\n+mcpServers:\n", java_profile.read_text(encoding="utf-8"))

    def test_agent_cells_copy_only_declared_skills_and_link_run_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill_root = root / "source-skills"
            for name in ("common-review", "java-review"):
                folder = skill_root / "collections" / name
                folder.mkdir(parents=True)
                (folder / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: test skill\n---\n\n# {name}\n",
                    encoding="utf-8",
                )
                (folder / "reference.txt").write_text("support", encoding="utf-8")
            run_dir = root / "run"
            for name in CELLS.CELL_LINKS:
                (run_dir / name).mkdir(parents=True)
            workflow = {"skill_roots": [str(skill_root)]}
            profile_source = root / "worker.md"
            profile_source.write_text(
                "---\nname: worker\ndescription: test worker\nallowedTools:\n"
                "  - fs_read\n  - fs_list\n---\n\n# Worker\n",
                encoding="utf-8",
            )
            cells = CELLS.prepare_agent_cells(
                workflow,
                run_dir,
                [
                    {
                        "id": "architect",
                        "profile": "audit-architect",
                        "skills": ["common-review"],
                        "profile_source": profile_source,
                    },
                    {
                        "id": "java-auditor",
                        "profile": "audit-java",
                        "skills": ["common-review", "java-review"],
                        "profile_source": profile_source,
                    },
                ],
            )

            architect = Path(cells["architect"]["workspace"])
            java = Path(cells["java-auditor"]["workspace"])
            architect_skill = architect / ".opencode/skills/architect-common-review"
            java_skill = java / ".opencode/skills/java-auditor-java-review"
            self.assertTrue((architect_skill / "SKILL.md").is_file())
            self.assertIn(
                "name: architect-common-review",
                (architect_skill / "SKILL.md").read_text(encoding="utf-8"),
            )
            self.assertFalse((architect / ".opencode/skills/architect-java-review").exists())
            self.assertTrue((java_skill / "reference.txt").is_file())
            self.assertEqual((architect / "shared").resolve(), (run_dir / "shared").resolve())
            self.assertTrue((architect / "shared").is_symlink())
            self.assertFalse(os.path.isabs(os.readlink(architect / "shared")))

            config = json.loads((architect / ".opencode/opencode.json").read_text())
            permissions = config["agent"]["audit-architect"]["permission"]
            rules = permissions["skill"]
            self.assertEqual(rules, {"*": "deny", "architect-common-review": "allow"})
            self.assertEqual(
                permissions["external_directory"],
                {"*": "deny", f"{run_dir.resolve()}/*": "allow"},
            )
            self.assertEqual(permissions["agent-skill-loader_*"], "deny")
            self.assertEqual(
                cells["architect"]["runtime_skills"],
                {"common-review": "architect-common-review"},
            )
            local_agent = architect / ".opencode/agents/audit-architect.md"
            self.assertIn(
                '"skill": {"*": "deny", "architect-common-review": "allow"}',
                local_agent.read_text(encoding="utf-8"),
            )
            self.assertIn(
                '"agent-skill-loader_*": "deny"',
                local_agent.read_text(encoding="utf-8"),
            )
            self.assertIn(
                f'"{CELLS.HUTCH_RUNS_GLOB}": "allow"',
                local_agent.read_text(encoding="utf-8"),
            )
            CELLS.prepare_agent_cells(
                workflow,
                run_dir,
                [
                    {
                        "id": "architect",
                        "profile": "audit-architect",
                        "skills": ["common-review"],
                        "profile_source": profile_source,
                    }
                ],
            )
            self.assertTrue((architect_skill / "SKILL.md").is_file())

    def test_agent_cell_rejects_unknown_skill(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill_root = root / "skills"
            skill_root.mkdir()
            with self.assertRaisesRegex(CELLS.AgentCellError, "skills not found"):
                CELLS.validate_cell_specs(
                    {"skill_roots": [str(skill_root)]},
                    [{"id": "worker", "profile": "worker", "skills": ["missing"]}],
                )

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
