import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_cao_native_flow as GENERATOR
import hutch_template as TEMPLATE
from adaptive_audit import atomic_json


class WorkflowTemplateTests(unittest.TestCase):
    def create_repository(self, root: Path) -> Path:
        repository = root / "target"
        (repository / "src/main/java/example").mkdir(parents=True)
        (repository / ".git").mkdir()
        (repository / "pom.xml").write_text("<project/>", encoding="utf-8")
        (repository / "src/main/java/example/App.java").write_text(
            "class App {}", encoding="utf-8"
        )
        return repository

    def test_builtin_templates_are_listed(self):
        templates = TEMPLATE.list_templates()

        self.assertEqual(
            [item["id"] for item in templates],
            [
                "information-collection",
                "one-run",
                "security-knowledge-one-run",
                "security-knowledge-recon",
                "security-knowledge-threat-model",
                "security-knowledge-vulnerability-mining",
                "threat-modeling",
                "vulnerability-mining",
            ],
        )
        self.assertTrue(all(item["stages"] >= 1 for item in templates))

    def test_one_run_template_renders_compileable_workflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.create_repository(root)

            workflow, removed, _ = TEMPLATE.instantiate_template(
                "one-run",
                repository,
                name="target-one-run",
                cao_repo=root,
                skill_roots=[],
            )
            workflow_path = root / "target-one-run.json"
            atomic_json(workflow_path, workflow)

            validated = GENERATOR.load_and_validate(workflow_path)
            batches = GENERATOR.execution_batches(validated)

            self.assertEqual(validated["schema"], "hutch.cao-workflow.v1")
            self.assertEqual(validated["template"]["id"], "one-run")
            self.assertEqual(
                [stage["id"] for stage in validated["stages"]],
                [
                    "repository-intelligence",
                    "threat-intelligence",
                    "attack-surface-mining",
                    "implementation-mining",
                    "component-supplychain-mining",
                    "finding-validation",
                    "final-report",
                ],
            )
            self.assertEqual([len(batch) for batch in batches], [1, 1, 3, 1, 1])
            self.assertIn("repository-analyst", removed)
            self.assertNotIn("/" + "Users/", json.dumps(validated))

    def test_one_run_template_can_target_codex(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.create_repository(root)

            workflow, _, _ = TEMPLATE.instantiate_template(
                "one-run",
                repository,
                name="target-codex-one-run",
                cao_repo=root,
                skill_roots=[],
                provider="codex",
            )
            workflow_path = root / "target-codex-one-run.json"
            atomic_json(workflow_path, workflow)

            validated = GENERATOR.load_and_validate(workflow_path)
            output = root / "bundle"
            manifest = GENERATOR.write_output(
                workflow_path,
                validated,
                output,
                root,
            )

            self.assertEqual(validated["provider"], "codex")
            self.assertIn(
                "provider: codex",
                Path(manifest["flow"]).read_text(encoding="utf-8"),
            )
            for profile in manifest["profiles"]:
                self.assertIn(
                    "provider: codex",
                    Path(profile).read_text(encoding="utf-8"),
                )
            supervisor = (
                output / "profiles/target-codex-one-run-supervisor.md"
            ).read_text(encoding="utf-8")
            self.assertIn("command: uv", supervisor)
            self.assertIn(f"      - {root}", supervisor)
            self.assertNotIn("command: sh", supervisor)

    def test_strict_skills_rejects_missing_template_skills(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.create_repository(root)

            with self.assertRaisesRegex(TEMPLATE.TemplateError, "skills not found"):
                TEMPLATE.instantiate_template(
                    "one-run",
                    repository,
                    cao_repo=root,
                    skill_roots=[],
                    strict_skills=True,
                )

    def test_security_knowledge_template_uses_supplied_skill_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.create_repository(root)
            skill_root = root / "skills"
            skill_root.mkdir()
            _, template = TEMPLATE.load_template("security-knowledge-one-run")
            for skill in sorted(TEMPLATE.requested_skills(template["workflow"])):
                directory = skill_root / skill
                directory.mkdir()
                (directory / "SKILL.md").write_text(
                    f"---\nname: {skill}\ndescription: fixture\n---\n\nfixture\n",
                    encoding="utf-8",
                )

            workflow, removed, _ = TEMPLATE.instantiate_template(
                "security-knowledge-one-run",
                repository,
                name="target-security-knowledge",
                cao_repo=root,
                skill_roots=[skill_root],
                strict_skills=True,
            )
            workflow_path = root / "security-knowledge.json"
            atomic_json(workflow_path, workflow)

            validated = GENERATOR.load_and_validate(workflow_path)
            router = next(
                agent for agent in validated["agents"] if agent["id"] == "security-router"
            )

            self.assertEqual(removed, {})
            self.assertIn("secknowledge-skill", router["skills"])
            self.assertIn("hack", router["skills"])
            self.assertEqual(validated["template"]["id"], "security-knowledge-one-run")
            self.assertEqual(
                [len(batch) for batch in GENERATOR.execution_batches(validated)],
                [1, 1, 5, 1, 1],
            )

    def test_security_knowledge_bundle_records_license_gate(self):
        bundle = json.loads(
            (ROOT / "template/skill-bundles/security-knowledge.json").read_text(
                encoding="utf-8"
            )
        )
        profiles = json.loads(
            (ROOT / "template/agent-profiles/security-knowledge.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(bundle["schema"], "hutch.skill-bundle.v1")
        self.assertIn("do not vendor", bundle["sources"][0]["license_status"])
        self.assertEqual(len(profiles["profiles"]), 8)
        self.assertEqual(
            {profile["id"] for profile in profiles["profiles"]},
            {
                "security-router",
                "web-api-auth-auditor",
                "injection-auditor",
                "file-ssrf-auditor",
                "ai-agent-auditor",
                "infra-supply-auditor",
                "security-validator",
                "security-report-writer",
            },
        )


if __name__ == "__main__":
    unittest.main()
