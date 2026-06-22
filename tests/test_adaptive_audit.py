import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import adaptive_audit as ADAPTIVE
import generate_cao_native_flow as GENERATOR
import hutch_campaign as CAMPAIGN


class AdaptiveAuditTests(unittest.TestCase):
    def create_repository(self, root: Path) -> Path:
        repository = root / "repo"
        (repository / "core/src/main/java/example").mkdir(parents=True)
        (repository / "plugin/src/main/java/example").mkdir(parents=True)
        (repository / "pom.xml").write_text("<project/>", encoding="utf-8")
        (repository / "core/pom.xml").write_text("<project/>", encoding="utf-8")
        (repository / "plugin/build.gradle").write_text("plugins {}", encoding="utf-8")
        (repository / "core/src/main/java/example/Core.java").write_text(
            "class Core {}", encoding="utf-8"
        )
        (repository / "plugin/src/main/java/example/Plugin.java").write_text(
            "class Plugin {}", encoding="utf-8"
        )
        return repository

    def test_inventory_discovers_build_modules_and_accounts_for_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.create_repository(Path(temporary))
            repo_inventory, modules = ADAPTIVE.build_inventories(repository)
            self.assertEqual(repo_inventory["source_file_count"], 2)
            self.assertEqual(modules["module_count"], 3)
            self.assertEqual(
                {module["path"] for module in modules["modules"]},
                {".", "core", "plugin"},
            )
            self.assertEqual(
                sum(module["source_file_count"] for module in modules["modules"]), 2
            )

    def test_generic_recon_workflow_is_compilable_for_any_git_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.create_repository(root)
            (repository / ".git").mkdir()
            (root / "skills").mkdir()
            workflow = CAMPAIGN.recon_workflow(
                repository,
                "example-audit",
                cao_repo=root,
                skill_roots=[root / "skills"],
            )
            path = root / "recon.json"
            ADAPTIVE.atomic_json(path, workflow)

            validated = GENERATOR.load_and_validate(path)

            self.assertEqual(validated["target"], str(repository.resolve()))
            self.assertEqual(validated["campaign"]["phase"], "recon")
            self.assertEqual(len(validated["stages"]), 2)
            self.assertNotIn("DJL", json.dumps(validated))

    def test_plan_rejects_missing_module_and_normalizes_path_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, modules = ADAPTIVE.build_inventories(
                self.create_repository(Path(temporary))
            )
            ids = [module["id"] for module in modules["modules"]]
            missing_plan = {
                "schema": "hutch.audit-plan.v1",
                "strategy": "sharded",
                "max_concurrency": 2,
                "tasks": [{"id": "one", "module_ids": [ids[0]], "skills": []}],
            }
            with self.assertRaisesRegex(ADAPTIVE.AdaptiveAuditError, "missing"):
                ADAPTIVE.validate_audit_plan(modules, missing_plan)

            invalid_scope = {
                "schema": "hutch.audit-plan.v1",
                "strategy": "whole_repo",
                "max_concurrency": 1,
                "tasks": [
                    {
                        "id": "all",
                        "module_ids": ids,
                        "paths": ["."],
                        "skills": [],
                    }
                ],
            }
            validated = ADAPTIVE.validate_audit_plan(modules, invalid_scope)
            self.assertEqual(validated["tasks"][0]["paths"], [".", "core", "plugin"])
            self.assertEqual(
                validated["hutch_normalizations"][0]["field"], "tasks.all.paths"
            )

    def test_compiler_creates_bounded_parallel_shards_and_coverage_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.create_repository(root)
            _, modules = ADAPTIVE.build_inventories(repository)
            tasks = [
                {
                    "id": f"module-{index}",
                    "module_ids": [module["id"]],
                    "paths": [module["path"]],
                    "skills": [],
                }
                for index, module in enumerate(modules["modules"], start=1)
            ]
            plan = {
                "schema": "hutch.audit-plan.v1",
                "strategy": "sharded",
                "max_concurrency": 2,
                "tasks": tasks,
            }
            workflow = ADAPTIVE.compile_workflow(
                modules,
                plan,
                name="test-mining",
                target=repository,
                cao_repo=root,
                skill_roots=[],
                campaign_id="campaign-1",
            )
            workflow_path = root / "workflow.json"
            ADAPTIVE.atomic_json(workflow_path, workflow)
            validated = GENERATOR.load_and_validate(workflow_path)
            batches = GENERATOR.execution_batches(validated)
            self.assertEqual([len(batch) for batch in batches], [2, 1, 1, 1, 1])
            self.assertIn("coverage_gate", batches[2][0])

            run_dir = root / "run"
            (run_dir / "shared").mkdir(parents=True)
            (run_dir / "artifacts/shards").mkdir(parents=True)
            ADAPTIVE.atomic_json(run_dir / "shared/modules.json", modules)
            for stage in workflow["stages"][: len(tasks)]:
                module_by_id = {module["id"]: module for module in modules["modules"]}
                ADAPTIVE.atomic_json(
                    run_dir / stage["coverage_contract"]["artifact"],
                    {
                        "schema": "hutch.coverage.v1",
                        "task_id": stage["task_id"],
                        "stage": stage["id"],
                        "modules": [
                            {
                                "module_id": module_id,
                                "status": "audited",
                                "reviewed_file_count": max(
                                    1, module_by_id[module_id]["source_file_count"]
                                ),
                                "evidence": [
                                    {
                                        "path": (
                                            "pom.xml"
                                            if module_by_id[module_id]["path"] == "."
                                            else module_by_id[module_id]["build_descriptors"][0]
                                        ),
                                        "observation": "reviewed module entry point",
                                    }
                                ],
                            }
                            for module_id in stage["coverage_contract"]["module_ids"]
                        ],
                    },
                )
            gate = next(stage for stage in workflow["stages"] if stage.get("coverage_gate"))
            summary = ADAPTIVE.build_coverage_summary(workflow, run_dir, gate)
            self.assertEqual(summary["gap_count"], 0)
            self.assertEqual(summary["audited_count"], modules["module_count"])

    def test_compiler_integrates_audit_skill_as_lead_stage_and_validator_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.create_repository(root)
            skill = root / "skills/audit-skills"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: audit-skills\ndescription: test fixture\n---\n",
                encoding="utf-8",
            )
            _, modules = ADAPTIVE.build_inventories(repository)
            plan = {
                "schema": "hutch.audit-plan.v1",
                "strategy": "whole_repo",
                "max_concurrency": 1,
                "tasks": [
                    {
                        "id": "whole-repository",
                        "module_ids": [module["id"] for module in modules["modules"]],
                        "paths": [module["path"] for module in modules["modules"]],
                        "skills": [],
                    }
                ],
            }
            workflow = ADAPTIVE.compile_workflow(
                modules,
                plan,
                name="test-audit-skill",
                target=repository,
                cao_repo=root,
                skill_roots=[root / "skills"],
                campaign_id="campaign-1",
            )
            workflow_path = root / "workflow.json"
            ADAPTIVE.atomic_json(workflow_path, workflow)

            validated = GENERATOR.load_and_validate(workflow_path)
            component = validated["stages"][0]
            miner = next(
                stage for stage in validated["stages"] if "coverage_contract" in stage
            )
            validator = next(
                stage
                for stage in validated["stages"]
                if stage["id"] == "finding-validation"
            )
            agent_by_id = {agent["id"]: agent for agent in validated["agents"]}

            self.assertEqual(component["id"], "component-risk-intelligence")
            self.assertEqual(miner["depends_on"], [component["id"]])
            self.assertIn("audit-skills", agent_by_id[miner["agent"]]["skills"])
            self.assertIn("audit-skills", agent_by_id[validator["agent"]]["skills"])
            self.assertEqual(validated["methodology"]["integrations"], ["audit-skills"])
            self.assertNotIn(
                "outbox/C-0001.result.json",
                next(
                    stage
                    for stage in validated["stages"]
                    if stage["id"] == "final-report"
                )["report_consistency"]["audit_results"],
            )
            profile = GENERATOR.render_worker_profile(
                validated, agent_by_id[miner["agent"]]
            )
            self.assertIn("never confirmed vulnerabilities", profile)
            self.assertIn("tmp/audit-skills/<task-id>/", profile)
            self.assertIn(
                ".opencode/skills/miner-001-audit-skills/scripts/run_component_vulnerability_scan.py",
                profile,
            )

    def test_generic_framework_descriptor_has_all_campaign_phases(self):
        descriptor = json.loads(
            (ROOT / "workflows/generic-vulnerability-mining-framework.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(descriptor["schema"], "hutch.campaign-template.v1")
        self.assertEqual(
            [phase["id"] for phase in descriptor["phases"]],
            ["recon", "planning", "mining"],
        )
        self.assertTrue(descriptor["controls"]["deterministic_coverage_gate"])
        self.assertEqual(descriptor["integrations"][0]["name"], "audit-skills")

    def test_coverage_rejects_uncontracted_and_missing_modules(self):
        stage = {
            "id": "audit-001",
            "task_id": "A-0001",
            "coverage_contract": {
                "artifact": "artifacts/coverage.json",
                "module_ids": ["one", "two"],
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "artifacts").mkdir()
            (run_dir / "artifacts/coverage.json").write_text(
                json.dumps(
                    {
                        "schema": "hutch.coverage.v1",
                        "task_id": "A-0001",
                        "stage": "audit-001",
                        "modules": [
                            {
                                "module_id": "one",
                                "status": "audited",
                                "reviewed_file_count": 1,
                                "evidence": [
                                    {"path": "One.java", "observation": "reviewed"}
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            valid, reason = ADAPTIVE.validate_coverage_document(stage, run_dir)
            self.assertFalse(valid)
            self.assertIn("two", reason)

    def test_campaign_handoff_builds_planning_then_mining_workflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.create_repository(root)
            skills = root / "skills"
            skills.mkdir()
            recon = root / "recon-run"
            (recon / "artifacts/intelligence").mkdir(parents=True)
            ADAPTIVE.atomic_json(
                recon / "workflow.json",
                {
                    "target": str(repository),
                    "snapshot": {"max_file_bytes": 1024},
                    "campaign": {
                        "schema": "hutch.campaign.v1",
                        "id": "campaign-one",
                        "phase": "recon",
                    },
                },
            )
            ADAPTIVE.atomic_json(
                recon / "state.json",
                {
                    "run_id": "recon-run",
                    "status": "completed",
                    "campaign": {"id": "campaign-one", "phase": "recon"},
                },
            )
            ADAPTIVE.atomic_json(
                recon / "artifacts/intelligence/threat-model.json",
                {"schema": "hutch.threat-model.v1", "threats": []},
            )
            planning = CAMPAIGN.planning_workflow(
                recon, cao_repo=root, skill_roots=[skills]
            )
            self.assertEqual(planning["campaign"]["parent_run_id"], "recon-run")
            self.assertTrue(planning["seed_artifacts"])

            planning_run = root / "planning-run"
            (planning_run / "shared/intelligence").mkdir(parents=True)
            (planning_run / "artifacts").mkdir()
            _, modules = ADAPTIVE.build_inventories(repository)
            all_modules = modules["modules"]
            plan = {
                "schema": "hutch.audit-plan.v1",
                "strategy": "whole_repo",
                "max_concurrency": 1,
                "tasks": [
                    {
                        "id": "whole-repository",
                        "module_ids": [module["id"] for module in all_modules],
                        "paths": [module["path"] for module in all_modules],
                        "skills": [],
                    }
                ],
            }
            ADAPTIVE.atomic_json(planning_run / "workflow.json", planning)
            ADAPTIVE.atomic_json(
                planning_run / "state.json",
                {
                    "run_id": "planning-run",
                    "status": "completed",
                    "campaign": planning["campaign"],
                },
            )
            ADAPTIVE.atomic_json(planning_run / "shared/modules.json", modules)
            ADAPTIVE.atomic_json(planning_run / "artifacts/audit-plan.json", plan)
            (planning_run / "artifacts/audit-plan.md").write_text(
                "## Module Coverage Matrix\ncomplete\n", encoding="utf-8"
            )
            ADAPTIVE.atomic_json(
                planning_run / "shared/intelligence/threat-model.json",
                {"schema": "hutch.threat-model.v1", "threats": []},
            )
            mining = CAMPAIGN.mining_workflow(
                planning_run, cao_repo=root, skill_roots=[skills]
            )
            self.assertEqual(mining["campaign"]["planning_run_id"], "planning-run")
            self.assertEqual(mining["campaign"]["intelligence_run_id"], "recon-run")
            self.assertEqual(sum("coverage_contract" in stage for stage in mining["stages"]), 1)


if __name__ == "__main__":
    unittest.main()
