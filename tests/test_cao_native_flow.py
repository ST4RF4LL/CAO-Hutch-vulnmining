import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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
                "workspace from task JSON `agent_cell.workspace`", flow
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
                {
                    "*": "deny",
                    f"{run_dir.resolve()}/*": "allow",
                    f"{architect.resolve()}/*": "allow",
                },
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

    def test_agent_cell_uses_explicit_role_local_skill_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "role/skills/local-review"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text(
                "---\nname: local-review\ndescription: fixture\nlicense: MIT\n---\n",
                encoding="utf-8",
            )
            run_dir = root / "run"
            for name in CELLS.CELL_LINKS:
                (run_dir / name).mkdir(parents=True)

            cells = CELLS.prepare_agent_cells(
                {"provider": "codex", "skill_roots": []},
                run_dir,
                [
                    {
                        "id": "reviewer",
                        "profile": "audit-reviewer",
                        "skills": ["local-review"],
                        "skill_sources": {"local-review": str(source)},
                    }
                ],
            )

            copied = (
                Path(cells["reviewer"]["workspace"])
                / ".agents/skills/reviewer-local-review/SKILL.md"
            )
            self.assertTrue(copied.is_file())
            self.assertEqual(
                cells["reviewer"]["skill_sources"]["local-review"],
                str(source.resolve()),
            )

    def test_codex_agent_cell_uses_project_local_skill_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill_root = root / "skills"
            source = skill_root / "common-review"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text(
                "---\nname: common-review\ndescription: fixture\n---\n",
                encoding="utf-8",
            )
            run_dir = root / "run"
            for name in CELLS.CELL_LINKS:
                (run_dir / name).mkdir(parents=True)

            cells = CELLS.prepare_agent_cells(
                {"provider": "codex", "skill_roots": [str(skill_root)]},
                run_dir,
                [
                    {
                        "id": "reviewer",
                        "profile": "audit-reviewer",
                        "skills": ["common-review"],
                    }
                ],
            )

            workspace = Path(cells["reviewer"]["workspace"])
            copied = workspace / ".agents/skills/reviewer-common-review/SKILL.md"
            self.assertTrue(copied.is_file())
            self.assertEqual(cells["reviewer"]["provider"], "codex")
            self.assertEqual(cells["reviewer"]["skills_dir"], str(copied.parent.parent))
            self.assertIsNone(cells["reviewer"]["opencode_config"])
            self.assertFalse((workspace / ".opencode/opencode.json").exists())

    def test_agent_store_workspace_relinks_between_runs_and_assigns(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = root / "agents_store"
            workspace = store / "reviewer"
            workspace.mkdir(parents=True)
            profile = root / "profile.md"
            profile.write_text(
                "---\nname: reviewer\ndescription: Reviewer\nallowedTools:\n"
                "  - fs_read\n---\n\n# Reviewer\n",
                encoding="utf-8",
            )
            runs = [root / "run-1", root / "run-2"]
            with mock.patch.object(CELLS, "hutch_agents_store", return_value=store.resolve()):
                for run_dir in runs:
                    for name in CELLS.CELL_LINKS:
                        (run_dir / name).mkdir(parents=True)
                    cells = CELLS.prepare_agent_cells(
                        {"provider": "codex", "skill_roots": []},
                        run_dir,
                        [
                            {
                                "id": "reviewer",
                                "profile": "reviewer-profile",
                                "skills": [],
                                "profile_source": profile,
                                "agent_store": str(workspace),
                            }
                        ],
                    )
            self.assertEqual((workspace / "inbox").resolve(), (runs[-1] / "inbox").resolve())
            task = {
                "schema": "hutch.task.v1",
                "run_directory": str(runs[-1]),
                "agent_profile": "reviewer-profile",
                "agent_cell": cells["reviewer"],
            }
            task_path = runs[-1] / "inbox/T-1.task.json"
            task_path.write_text(json.dumps(task), encoding="utf-8")

            _, _, loaded_workspace, profile_name = ASSIGN._load_contract(task_path)

            self.assertEqual(loaded_workspace, workspace.resolve())
            self.assertEqual(profile_name, "reviewer-profile")

    def test_native_prepare_uses_target_project_without_snapshot(self):
        prepare_native = load_script("prepare_native_flow_run")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            (target / "README.md").write_text("target project\n", encoding="utf-8")
            agents_store_root = root / "agents_store"
            profile_dir = root / "profiles"
            profile_dir.mkdir()
            profile = profile_dir / "demo-reviewer.md"
            profile.write_text(
                "---\n"
                "name: demo-reviewer\n"
                "description: reviewer\n"
                "allowedTools:\n"
                "  - fs_read\n"
                "  - fs_list\n"
                "---\n\n"
                "# Reviewer\n",
                encoding="utf-8",
            )
            workflow = {
                "schema": "hutch.cao-workflow.v1",
                "name": "demo",
                "provider": "opencode_cli",
                "target": str(target),
                "agents": [
                    {
                        "id": "reviewer",
                        "description": "Reviewer",
                        "mission": "Review target.",
                        "skills": [],
                        "agent_store": str(agents_store_root / "reviewer"),
                    }
                ],
                "stages": [
                    {
                        "id": "review",
                        "task_id": "T-1",
                        "agent": "reviewer",
                        "depends_on": [],
                        "artifact": "artifacts/review.md",
                        "objective": "Review target.",
                    }
                ],
            }
            workflow_path = root / "workflow.json"
            workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
            agent_store = agents_store_root / "reviewer"
            agent_store.mkdir(parents=True)

            with mock.patch.object(prepare_native, "hutch_runs_dir", return_value=root / "runs"):
                with mock.patch("agent_cells.hutch_agents_store", return_value=agents_store_root.resolve()):
                    with mock.patch.object(
                        CELLS, "hutch_agents_store", return_value=agents_store_root.resolve()
                    ):
                        result = prepare_native.prepare(workflow_path, profile_dir)

            output = result["output"]
            run_dir = Path(output["run_dir"])
            self.assertNotIn("target_snapshot", output)
            self.assertEqual(output["target"], str(target.resolve()))
            self.assertFalse((run_dir / "shared/target-snapshot").exists())
            self.assertFalse((run_dir / "shared/snapshot-manifest.json").exists())
            self.assertTrue((run_dir / "shared/target-project.json").is_file())
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertNotIn("target_snapshot", manifest)
            self.assertEqual(manifest["target"], str(target.resolve()))
            self.assertEqual(
                manifest["agent_cells"]["reviewer"]["workspace"],
                str(agent_store.resolve()),
            )
            self.assertEqual(
                manifest["agent_cells"]["reviewer"]["runtime_workspace"],
                str((run_dir / "agents/reviewer/workspace").resolve()),
            )
            task = json.loads((run_dir / "inbox/T-1.task.json").read_text(encoding="utf-8"))
            self.assertEqual(task["target"]["type"], "target_project")
            self.assertEqual(task["target"]["path"], str(target.resolve()))
            self.assertEqual(task["constraints"]["write_root"], str(run_dir))

    def test_codex_assignment_waits_for_mcp_startup_to_settle(self):
        states = iter(
            [
                {"status": "idle"},
                {"output": "Starting MCP servers (3/4)"},
                {"status": "idle"},
                {"output": ""},
                {"status": "idle"},
                {"output": ""},
                {"status": "idle"},
                {"output": ""},
            ]
        )

        with mock.patch.object(ASSIGN, "_request", side_effect=lambda *args, **kwargs: next(states)):
            with mock.patch.object(ASSIGN.time, "sleep"):
                status = ASSIGN._wait_until_input_ready("deadbeef", "codex", 10)

        self.assertEqual(status, "idle")

    def test_codex_assignment_accepts_workspace_trust_prompt(self):
        with mock.patch.object(ASSIGN, "_request", return_value={"success": True}) as request:
            accepted = ASSIGN._accept_codex_trust_prompt(
                "deadbeef",
                "codex",
                "Yes, allow Codex to work in this folder without asking for approval",
            )

        self.assertTrue(accepted)
        request.assert_called_once_with(
            "POST",
            "/terminals/deadbeef/key",
            params={"key": "Enter"},
        )

    def test_codex_assignment_accepts_truncated_workspace_trust_prompt(self):
        with mock.patch.object(ASSIGN, "_request", return_value={"success": True}) as request:
            accepted = ASSIGN._accept_codex_trust_prompt(
                "deadbeef",
                "codex",
                "2. No, quit\n\n  Press enter to continue",
            )

        self.assertTrue(accepted)
        request.assert_called_once_with(
            "POST",
            "/terminals/deadbeef/key",
            params={"key": "Enter"},
        )

    def test_codex_trust_guard_records_terminal_before_accepting_prompt(self):
        discovered_ids = set()
        requests = []

        def request(method, path, **kwargs):
            requests.append((method, path, kwargs))
            if path == "/sessions/test-session/terminals":
                return [
                    {
                        "id": "new-worker",
                        "agent_profile": "reporter-profile",
                    }
                ]
            if path == "/terminals/new-worker/output":
                return {"output": "Do you trust the files in this folder?"}
            if path == "/terminals/new-worker/key":
                return {"success": True}
            raise AssertionError((method, path, kwargs))

        with mock.patch.object(ASSIGN, "_request", side_effect=request):
            ASSIGN._guard_codex_trust_prompt(
                "test-session",
                set(),
                "reporter-profile",
                discovered_ids,
                ASSIGN.threading.Event(),
                1,
            )

        self.assertEqual(discovered_ids, {"new-worker"})
        self.assertIn(
            (
                "POST",
                "/terminals/new-worker/key",
                {"params": {"key": "Enter"}},
            ),
            requests,
        )

    def test_non_codex_assignment_does_not_accept_workspace_trust_prompt(self):
        with mock.patch.object(ASSIGN, "_request") as request:
            accepted = ASSIGN._accept_codex_trust_prompt(
                "deadbeef",
                "opencode_cli",
                "allow Codex to work in this folder",
            )

        self.assertFalse(accepted)
        request.assert_not_called()

    def test_codex_assignment_cleans_terminal_discovered_before_create_failure(self):
        class ImmediateThread:
            def __init__(self, *, target, args, daemon):
                self.target = target
                self.args = args

            def start(self):
                self.target(*self.args)

            def join(self, timeout=None):
                return None

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            workspace = run_dir / "agents/reporter/workspace"
            inbox = run_dir / "inbox"
            for name in ("artifacts", "outbox", "shared", "tmp"):
                (run_dir / name).mkdir(parents=True)
            workspace.mkdir(parents=True)
            inbox.mkdir()
            for name in ASSIGN.CELL_LINKS:
                (workspace / name).symlink_to(
                    os.path.relpath(run_dir / name, workspace),
                    target_is_directory=True,
                )
            cell_dir = run_dir / "agents/reporter"
            (cell_dir / "cell.json").write_text(
                json.dumps(
                    {
                        "schema": "hutch.agent-cell.v1",
                        "id": "reporter",
                        "profile": "reporter-profile",
                        "cell_dir": str(cell_dir.resolve()),
                        "workspace": str(workspace.resolve()),
                    }
                ),
                encoding="utf-8",
            )
            task_path = inbox / "T-1.task.json"
            task_path.write_text(
                json.dumps(
                    {
                        "schema": "hutch.task.v1",
                        "run_directory": str(run_dir),
                        "agent_profile": "reporter-profile",
                        "agent_cell": {
                            "id": "reporter",
                            "profile": "reporter-profile",
                            "provider": "codex",
                            "workspace": str(workspace),
                        },
                    }
                ),
                encoding="utf-8",
            )
            requests = []

            def request(method, path, **kwargs):
                requests.append((method, path))
                if path == "/terminals/supervisor":
                    return {"session_name": "test-session"}
                if path == "/sessions/test-session/terminals" and method == "GET":
                    return []
                if path == "/sessions/test-session/terminals" and method == "POST":
                    raise ASSIGN.AssignError("create request failed before returning id")
                if path == "/terminals/orphan" and method == "DELETE":
                    return {"success": True}
                raise AssertionError((method, path, kwargs))

            def discover(session, existing_ids, profile, discovered_ids, stop, timeout):
                discovered_ids.add("orphan")

            with mock.patch.dict(os.environ, {"CAO_TERMINAL_ID": "supervisor"}):
                with mock.patch.object(ASSIGN, "_request", side_effect=request):
                    with mock.patch.object(
                        ASSIGN, "_guard_codex_trust_prompt", side_effect=discover
                    ):
                        with mock.patch.object(
                            ASSIGN.threading, "Thread", ImmediateThread
                        ):
                            with self.assertRaisesRegex(
                                ASSIGN.AssignError, "create request failed"
                            ):
                                ASSIGN.assign(task_path, 10)

            self.assertIn(("DELETE", "/terminals/orphan"), requests)

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

    def test_domain_plan_skip_writes_durable_stage_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            for name in ("artifacts", "outbox"):
                (run_dir / name).mkdir()
            workflow = {
                "stages": [
                    {
                        "id": "planning",
                        "task_id": "D-0002",
                        "agent": "planner",
                        "depends_on": [],
                        "artifact": "artifacts/plan.md",
                    },
                    {
                        "id": "java-audit",
                        "task_id": "D-0101",
                        "agent": "java",
                        "depends_on": ["planning"],
                        "artifact": "artifacts/java.md",
                        "required_sections": [
                            "Scope and Method",
                            "Evidence and Limitations",
                        ],
                        "domain_condition": {
                            "domain": "java",
                            "plan_artifact": "artifacts/domain-plan.json",
                        },
                    },
                ]
            }
            state = {
                "run_id": "direct-run",
                "status": "running",
                "stages": {
                    "planning": {"status": "done"},
                    "java-audit": {"status": "pending"},
                },
            }
            (run_dir / "workflow.json").write_text(
                json.dumps(workflow), encoding="utf-8"
            )
            (run_dir / "state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            (run_dir / "events.jsonl").write_text("", encoding="utf-8")
            (run_dir / "artifacts/domain-plan.json").write_text(
                json.dumps(
                    {
                        "schema": "hutch.domain-audit-plan.v1",
                        "decisions": [
                            {
                                "domain": "java",
                                "action": "skip",
                                "reason": "No Java or JVM artifacts were discovered.",
                                "evidence": ["shared/repository-inventory.json"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = STATE.skip_stage(run_dir, "java-audit")

            self.assertEqual(result["action"], "skip")
            updated = json.loads(
                (run_dir / "state.json").read_text(encoding="utf-8")
            )
            self.assertTrue(updated["stages"]["java-audit"]["skipped"])
            evidence = json.loads(
                (run_dir / "outbox/D-0101.result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(evidence["findings"], [])
            self.assertIn(
                "## Scope and Method",
                (run_dir / "artifacts/java.md").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
