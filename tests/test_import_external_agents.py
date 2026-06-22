import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CAO_REPO = Path("/Users/wh4lter/Workspace/lab/cli-agent-orchestrator")
SCRIPT = ROOT / "scripts" / "import_external_agents.py"


class ExternalAgentImportTests(unittest.TestCase):
    def run_importer(self, *arguments):
        result = subprocess.run(
            [
                "uv",
                "run",
                "--directory",
                str(CAO_REPO),
                "python",
                str(SCRIPT),
                *map(str, arguments),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        return result

    def test_imports_agent_and_skill_with_fail_closed_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "external"
            output = root / "profiles"
            (source / ".opencode/agents").mkdir(parents=True)
            (source / ".opencode/skills/review").mkdir(parents=True)
            (source / "LICENSE").write_text("MIT fixture", encoding="utf-8")
            (source / ".opencode/agents/coordinator.md").write_text(
                "---\ndescription: Coordinates review\nmode: primary\n"
                "permission:\n  edit: allow\n  bash: allow\n---\n\nCoordinate safely.\n",
                encoding="utf-8",
            )
            (source / ".opencode/skills/review/SKILL.md").write_text(
                "---\nname: focused-review\ndescription: Review one component\n---\n\n"
                "Review the assigned component.\n",
                encoding="utf-8",
            )
            (source / ".opencode/skills/review/reference.txt").write_text(
                "evidence", encoding="utf-8"
            )

            result = self.run_importer(
                source,
                output,
                "--format",
                "opencode",
                "--include-skills",
                "--prefix",
                "ext",
                "--cao-repo",
                CAO_REPO,
            )

            self.assertEqual(result.returncode, 0, result.stdout)
            manifest = json.loads((output / "import-manifest.json").read_text())
            self.assertEqual(len(manifest["profiles"]), 2)
            self.assertEqual(len(manifest["licenses"]), 1)
            coordinator = next(
                item for item in manifest["profiles"] if item["kind"] == "agent"
            )
            self.assertEqual(coordinator["role"], "reviewer")
            self.assertEqual(coordinator["allowed_tools"], ["fs_read", "fs_list"])
            self.assertIn("demoted", manifest["warnings"][0])
            profile = Path(coordinator["profile"]).read_text(encoding="utf-8")
            self.assertNotIn("execute_bash", profile)
            self.assertNotIn("fs_write", profile)
            validation = subprocess.run(
                [
                    "uv",
                    "run",
                    "--directory",
                    str(CAO_REPO),
                    "python",
                    "-c",
                    "from pathlib import Path; "
                    "from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text; "
                    f"p=Path({str(coordinator['profile'])!r}); "
                    "profile=parse_agent_profile_text(p.read_text(), p.stem); "
                    "print(profile.name)",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(validation.returncode, 0, validation.stdout)
            self.assertEqual(validation.stdout.strip(), "ext-coordinator")
            self.assertTrue(
                (output / "_skills/ext-focused-review/reference.txt").is_file()
            )

    def test_dry_run_discovers_codex_toml_without_writing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "external"
            output = root / "profiles"
            (source / ".codex/agents").mkdir(parents=True)
            (source / ".codex/agents/reviewer.toml").write_text(
                'name = "reviewer"\n'
                'description = "Reviews correctness"\n'
                'developer_instructions = "Review evidence only."\n',
                encoding="utf-8",
            )

            result = self.run_importer(
                source,
                output,
                "--format",
                "codex",
                "--dry-run",
                "--cao-repo",
                CAO_REPO,
            )

            self.assertEqual(result.returncode, 0, result.stdout)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["schema"], "hutch.agent-import-plan.v1")
            self.assertEqual(plan["items"][0]["name"], "reviewer")
            self.assertIn("no top-level license", plan["warnings"][0])
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
