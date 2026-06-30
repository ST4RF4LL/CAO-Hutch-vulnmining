import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from hutch_dashboard.model import RunDeleteConflict, RunRepository
from hutch_dashboard.server import CaoGateway, CaoGatewayError, handler_factory


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class HutchDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.runs = root / "runs"
        self.logs = root / "logs"
        self.db = root / "missing.db"
        self.target = root / "targets" / "repo-a"
        (self.target / ".git").mkdir(parents=True)
        run = self.runs / "run-001"
        write_json(
            run / "state.json",
            {
                "run_id": "run-001",
                "workflow": "audit-flow",
                "status": "completed",
                "created_at": "2026-06-19T10:00:00+08:00",
                "finished_at": "2026-06-19T10:05:00+08:00",
                "cao_session": "cao-flow-audit-flow",
                "target_fingerprint": {"target": str(self.target), "head": "abc123"},
                "integrity": {"ok": True},
                "stages": {"audit": {"status": "done", "task_id": "T-1"}},
            },
        )
        write_json(
            run / "workflow.json",
            {
                "name": "audit-flow",
                "version": "1",
                "provider": "codex",
                "stages": [
                    {
                        "id": "audit",
                        "task_id": "T-1",
                        "agent": "auditor",
                        "artifact": "artifacts/audit.md",
                        "depends_on": [],
                    }
                ],
            },
        )
        write_json(
            run / "inbox/T-1.task.json",
            {"agent_profile": "audit-flow-auditor", "inputs": ["source"]},
        )
        write_json(
            run / "outbox/T-1.result.json",
            {
                "status": "done",
                "summary": "one finding",
                "findings": [{"status": "confirmed"}],
                "artifacts": ["artifacts/audit.md"],
            },
        )
        (run / "artifacts").mkdir()
        (run / "artifacts/audit.md").write_text("# Audit\nEvidence", encoding="utf-8")
        (run / "events.jsonl").write_text(
            json.dumps(
                {
                    "ts": "2026-06-19T10:01:00+08:00",
                    "event": "stage_assigned_by_cao",
                    "stage": "audit",
                    "terminal_id": "term-1",
                    "attempt": 1,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        write_json(
            self.logs / "term-1.snapshot.json",
            {
                "terminal_id": "term-1",
                "session_name": "cao-flow-audit-flow",
                "window_name": "auditor-abcd",
                "agent_profile": "audit-flow-auditor",
                "provider": "codex",
                "caller_id": "super-1",
            },
        )
        (self.logs / "term-1.scrollback").write_text("done", encoding="utf-8")
        self.repository = RunRepository(self.runs, self.logs, self.db)

    def tearDown(self):
        self.temporary.cleanup()

    def create_campaign_runs(self):
        state_path = self.runs / "run-001/state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["campaign"] = {
            "schema": "hutch.campaign.v1",
            "id": "adaptive-audit",
            "phase": "recon",
        }
        write_json(state_path, state)
        for run_id, workflow, phase, campaign in (
            (
                "run-002",
                "audit-planning",
                "planning",
                {
                    "schema": "hutch.campaign.v1",
                    "id": "adaptive-audit",
                    "phase": "planning",
                    "parent_run_id": "run-001",
                    "intelligence_run_id": "run-001",
                },
            ),
            (
                "run-003",
                "audit-mining",
                "mining",
                {
                    "schema": "hutch.campaign.v1",
                    "id": "adaptive-audit",
                    "phase": "mining",
                    "intelligence_run_id": "run-001",
                    "planning_run_id": "run-002",
                },
            ),
        ):
            run = self.runs / run_id
            write_json(
                run / "state.json",
                {
                    "run_id": run_id,
                    "workflow": workflow,
                    "status": "completed",
                    "created_at": f"2026-06-19T10:0{1 if phase == 'planning' else 2}:00+08:00",
                    "finished_at": f"2026-06-19T10:0{2 if phase == 'planning' else 3}:00+08:00",
                    "target_fingerprint": {"target": str(self.target), "head": "abc123"},
                    "integrity": {"ok": True},
                    "campaign": campaign,
                    "stages": {},
                },
            )
            write_json(
                run / "workflow.json",
                {"name": workflow, "campaign": campaign, "stages": []},
            )
        return self.repository.list_campaigns()[0]["instance_id"]

    def test_reconstructs_completed_flow_agents_sessions_and_text_outputs(self):
        summaries = self.repository.list_runs(status="completed")
        self.assertEqual([item["run_id"] for item in summaries], ["run-001"])
        self.assertEqual(summaries[0]["project"]["repo_path"], str(self.target.resolve()))
        detail = self.repository.get_run("run-001")
        self.assertEqual(detail["duration_seconds"], 300)
        self.assertEqual(len(detail["agents"]), 2)  # supervisor + worker
        self.assertEqual(
            detail["graph"],
            {
                "nodes": [
                    {
                        "id": "flow-supervisor",
                        "label": "audit-flow-supervisor",
                        "status": "completed",
                        "type": "supervisor",
                    },
                    {
                        "id": "audit",
                        "label": "audit-flow-auditor",
                        "status": "done",
                        "type": "agent",
                    },
                ],
                "edges": [
                    {
                        "id": "flow-supervisor--audit",
                        "source": "flow-supervisor",
                        "target": "audit",
                        "type": "dispatch",
                        "transfers": [],
                    }
                ],
            },
        )
        worker = next(item for item in detail["agents"] if item["stage"] == "audit")
        self.assertEqual(worker["assignments"][0]["terminal_id"], "term-1")
        self.assertEqual(worker["assignments"][0]["session"], "cao-flow-audit-flow")
        self.assertTrue(worker["assignments"][0]["scrollback_available"])
        artifact = next(item for item in detail["deliverables"] if item["path"] == "artifacts/audit.md")
        self.assertEqual(artifact["content"], "# Audit\nEvidence")

    def test_campaign_aggregates_child_flows_without_hiding_them(self):
        instance_id = self.create_campaign_runs()

        campaigns = self.repository.list_campaigns()
        detail = self.repository.get_campaign(instance_id)

        self.assertEqual(len(campaigns), 1)
        self.assertEqual(campaigns[0]["status"], "completed")
        self.assertEqual(campaigns[0]["phases"], ["recon", "planning", "mining"])
        self.assertEqual(
            [flow["run_id"] for flow in campaigns[0]["flows"]],
            ["run-001", "run-002", "run-003"],
        )
        self.assertEqual(len(self.repository.list_runs()), 3)
        self.assertEqual(len(detail["graph"]["nodes"]), 3)
        self.assertEqual(
            {(edge["source"], edge["target"]) for edge in detail["graph"]["edges"]},
            {("run-001", "run-002"), ("run-002", "run-003")},
        )
        self.assertEqual(detail["deliverables"][0]["run_id"], "run-001")

    def test_markdown_renderer_does_not_use_html_injection(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "hutch_dashboard"
            / "static"
            / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn("function renderMarkdown(markdown)", script)
        self.assertIn('artifactMode: "rendered"', script)
        self.assertIn("function renderCampaignGraph(campaign)", script)
        self.assertIn('fetchJSON("/api/campaigns")', script)
        self.assertNotIn(".innerHTML", script)
        self.assertNotIn("insertAdjacentHTML", script)

    def test_groups_flows_by_enclosing_git_repository_directory(self):
        nested_target = self.target / "modules" / "api"
        nested_target.mkdir(parents=True)
        run = self.runs / "run-002"
        write_json(
            run / "state.json",
            {
                "run_id": "run-002",
                "workflow": "follow-up-flow",
                "status": "completed",
                "created_at": "2026-06-18T10:00:00+08:00",
                "finished_at": "2026-06-18T10:01:00+08:00",
                "target_fingerprint": {"target": str(nested_target), "head": "def456"},
                "stages": {},
            },
        )
        write_json(run / "workflow.json", {"name": "follow-up-flow", "stages": []})

        projects = self.repository.list_projects(status="completed")

        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["name"], "repo-a")
        self.assertEqual(projects[0]["repo_path"], str(self.target.resolve()))
        self.assertEqual(projects[0]["flow_count"], 2)
        self.assertEqual(
            {flow["run_id"] for flow in projects[0]["flows"]},
            {"run-001", "run-002"},
        )

    def test_configured_application_builds_adaptive_tree_with_service_leaves(self):
        root = Path(self.temporary.name)
        application = root / "application"
        payments = application / "payments"
        service_a = payments / "services" / "checkout"
        service_b = payments / "services" / "ledger"
        service_c = application / "internal" / "platform" / "identity"
        (service_a / ".git").mkdir(parents=True)
        (service_b / ".git").mkdir(parents=True)
        (service_c / ".git").mkdir(parents=True)
        projects_file = root / "projects.json"
        write_json(
            projects_file,
            {
                "projects": [
                    {"id": "commerce", "name": "Commerce", "root": str(application)}
                ]
            },
        )
        run = self.runs / "run-002"
        write_json(
            run / "state.json",
            {
                "run_id": "run-002",
                "workflow": "checkout-audit",
                "status": "completed",
                "created_at": "2026-06-20T10:00:00+08:00",
                "finished_at": "2026-06-20T10:02:00+08:00",
                "target_fingerprint": {"target": str(service_a), "head": "def456"},
                "stages": {"report": {"status": "done", "task_id": "T-2"}},
            },
        )
        write_json(
            run / "workflow.json",
            {
                "name": "checkout-audit",
                "stages": [
                    {
                        "id": "report",
                        "task_id": "T-2",
                        "artifact": "artifacts/final-report.md",
                    }
                ],
            },
        )
        (run / "artifacts").mkdir()
        (run / "artifacts/final-report.md").write_text("# Report", encoding="utf-8")

        repository = RunRepository(self.runs, self.logs, self.db, projects_file)
        project = repository.get_project("commerce")

        self.assertEqual(project["root_path"], str(application.resolve()))
        self.assertEqual(project["directory_count"], 4)
        self.assertEqual(project["service_count"], 3)
        self.assertEqual(project["flow_count"], 1)
        self.assertEqual(project["report_count"], 1)
        payments_node = next(
            child for child in project["tree"]["children"] if child["name"] == "payments"
        )
        internal_node = next(
            child for child in project["tree"]["children"] if child["name"] == "internal"
        )
        services_node = payments_node["children"][0]
        self.assertEqual(payments_node["name"], "payments")
        self.assertEqual(services_node["name"], "services")
        checkout = next(service for service in services_node["children"] if service["name"] == "checkout")
        ledger = next(service for service in services_node["children"] if service["name"] == "ledger")
        self.assertEqual(checkout["type"], "service")
        self.assertNotIn("children", checkout)
        self.assertEqual(checkout["flows"][0]["run_id"], "run-002")
        self.assertEqual(checkout["reports"][0]["path"], "artifacts/final-report.md")
        self.assertEqual(ledger["flow_count"], 0)
        self.assertEqual(internal_node["children"][0]["name"], "platform")
        self.assertEqual(
            internal_node["children"][0]["children"][0]["name"], "identity"
        )

        detail = repository.get_run("run-002")
        self.assertEqual(detail["project"]["id"], "commerce")
        self.assertEqual(detail["domain"]["name"], "payments / services")
        self.assertEqual(detail["service"]["name"], "checkout")
        self.assertEqual(detail["service"]["tree_path"], ["payments", "services"])

    def test_open_project_persists_registry_and_refreshes_tree(self):
        root = Path(self.temporary.name)
        application = root / "new-application"
        service = application / "domain" / "service"
        (service / ".git").mkdir(parents=True)
        projects_file = root / "projects.json"
        repository = RunRepository(self.runs, self.logs, self.db, projects_file)

        project = repository.open_project(
            str(application), name="New Application", project_id="new-app"
        )

        self.assertEqual(project["id"], "new-app")
        self.assertEqual(project["service_count"], 1)
        persisted = json.loads(projects_file.read_text(encoding="utf-8"))
        self.assertEqual(persisted["schema"], "hutch.projects.v1")
        self.assertEqual(persisted["projects"][0]["root"], str(application.resolve()))

    def test_stop_run_preserves_interrupted_stage_evidence(self):
        state_path = self.runs / "run-001/state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["status"] = "running"
        state["stages"]["audit"]["status"] = "running"
        write_json(state_path, state)

        result = self.repository.stop_run("run-001")

        stopped = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(stopped["stages"]["audit"]["status"], "interrupted")
        self.assertIn("run_stopped_by_operator", (self.runs / "run-001/events.jsonl").read_text())

    def test_http_api_lists_runs_and_projects(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(self.repository))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(base + "/api/runs") as response:
                runs = json.load(response)
            with urllib.request.urlopen(base + "/api/runs/run-001") as response:
                detail = json.load(response)
            with urllib.request.urlopen(base + "/api/projects") as response:
                projects = json.load(response)
            self.assertEqual(runs[0]["workflow"], "audit-flow")
            self.assertEqual(detail["agents"][1]["profile"], "audit-flow-auditor")
            self.assertEqual(projects[0]["flows"][0]["run_id"], "run-001")
        finally:
            server.shutdown()
            server.server_close()

    def test_http_api_lists_and_gets_campaigns(self):
        instance_id = self.create_campaign_runs()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(self.repository))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(base + "/api/campaigns") as response:
                campaigns = json.load(response)
            with urllib.request.urlopen(
                base + f"/api/campaigns/{instance_id}"
            ) as response:
                detail = json.load(response)
            self.assertEqual(campaigns[0]["campaign_id"], "adaptive-audit")
            self.assertEqual(detail["flow_count"], 3)
            self.assertEqual(len(detail["graph"]["edges"]), 2)
        finally:
            server.shutdown()
            server.server_close()

    def test_delete_finished_run_moves_record_to_recoverable_trash(self):
        result = self.repository.delete_run("run-001")

        self.assertTrue(result["deleted"])
        self.assertEqual(self.repository.list_runs(), [])
        destination = Path(result["recoverable_path"])
        self.assertTrue((destination / "state.json").is_file())
        self.assertEqual(destination.parent, (self.runs / ".trash").resolve())

    def test_delete_running_flow_is_rejected(self):
        state_path = self.runs / "run-001/state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["status"] = "running"
        write_json(state_path, state)

        with self.assertRaises(RunDeleteConflict):
            self.repository.delete_run("run-001")
        self.assertTrue(state_path.is_file())

    def test_running_flow_without_cao_session_is_orphaned_and_deletable(self):
        state_path = self.runs / "run-001/state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["status"] = "running"
        state["cao_session"] = "cao-flow-missing"
        write_json(state_path, state)

        run = self.repository.list_runs(active_sessions=set())[0]
        self.assertEqual(run["status"], "orphaned")
        self.assertEqual(run["raw_status"], "running")
        result = self.repository.delete_run("run-001", active_sessions=set())
        self.assertEqual(result["status"], "orphaned")

    def test_running_flow_with_live_cao_session_remains_protected(self):
        state_path = self.runs / "run-001/state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["status"] = "running"
        state["cao_session"] = "cao-flow-audit-flow"
        write_json(state_path, state)

        run = self.repository.list_runs(
            active_sessions={"cao-flow-audit-flow"}
        )[0]
        self.assertEqual(run["status"], "running")
        with self.assertRaises(RunDeleteConflict):
            self.repository.delete_run(
                "run-001", active_sessions={"cao-flow-audit-flow"}
            )

    def test_delete_prepared_flow_is_allowed(self):
        state_path = self.runs / "run-001/state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["status"] = "prepared"
        write_json(state_path, state)

        result = self.repository.delete_run("run-001")

        self.assertTrue(result["deleted"])
        self.assertFalse(state_path.exists())

    def test_http_api_deletes_finished_run_record(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(self.repository))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            request = urllib.request.Request(
                base + "/api/runs/run-001", method="DELETE"
            )
            with urllib.request.urlopen(request) as response:
                result = json.load(response)
            self.assertTrue(result["deleted"])
            with urllib.request.urlopen(base + "/api/runs") as response:
                runs = json.load(response)
            self.assertEqual(runs, [])
        finally:
            server.shutdown()
            server.server_close()

    def test_terminal_snapshot_is_available_after_tmux_window_is_gone(self):
        terminal = self.repository.get_terminal_snapshot("term-1")
        self.assertIsNotNone(terminal)
        self.assertFalse(terminal["live"])
        self.assertEqual(terminal["window"], "auditor-abcd")
        self.assertEqual(terminal["output"], "done")

    def test_http_api_proxies_terminal_and_allowlisted_cao_commands(self):
        class FakeCao:
            def __init__(self):
                self.calls = []

            def terminal(self, terminal_id):
                return {"terminal_id": terminal_id, "live": True, "output": "screen"}

            def send_input(self, terminal_id, message):
                self.calls.append(("input", terminal_id, message))
                return {"success": True}

            def send_key(self, terminal_id, key):
                self.calls.append(("key", terminal_id, key))
                return {"success": True}

            def catalog(self):
                return {"flows": [{"name": "audit-flow"}], "profiles": [], "providers": []}

            def execute(self, command):
                self.calls.append(("execute", command))
                return {"ok": True, "command": command}

        fake = FakeCao()
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), handler_factory(self.repository, fake)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(base + "/api/terminals/term-1") as response:
                terminal = json.load(response)
            request = urllib.request.Request(
                base + "/api/terminals/term-1/input",
                data=json.dumps({"message": "continue"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                sent = json.load(response)
            command = urllib.request.Request(
                base + "/api/cao/execute",
                data=json.dumps({"command": "cao flow run audit-flow"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(command) as response:
                executed = json.load(response)
            self.assertEqual(terminal["output"], "screen")
            self.assertTrue(sent["success"])
            self.assertTrue(executed["ok"])
            self.assertIn(("input", "term-1", "continue"), fake.calls)
            self.assertIn(("execute", "cao flow run audit-flow"), fake.calls)
        finally:
            server.shutdown()
            server.server_close()

    def test_http_api_lists_hutch_stores_without_cao_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agents_store = root / "agents_store"
            flows_store = root / "flows_store"
            agent_dir = agents_store / "recon-planner"
            agent_dir.mkdir(parents=True)
            write_json(
                agent_dir / "manifest.json",
                {
                    "schema": "hutch.agent-store.v1",
                    "id": "recon-planner",
                    "description": "Plans audit domains.",
                    "instructions": "AGENTS.md",
                    "mcp": "mcp.json",
                    "skills": ["security-recon", "audit-artifact-management"],
                },
            )
            write_json(
                agent_dir / "mcp.json",
                {
                    "schema": "hutch.agent-mcp.v1",
                    "servers": {
                        "atlas": {"type": "stdio", "command": "atlas", "args": ["mcp"]}
                    },
                },
            )
            flow_dir = flows_store / "one-run"
            flow_dir.mkdir(parents=True)
            write_json(
                flow_dir / "flow.json",
                {
                    "schema": "hutch.cao-workflow-template.v1",
                    "id": "one-run",
                    "version": "1.0.0",
                    "description": "One run.",
                    "workflow": {
                        "provider": "opencode_cli",
                        "schedule": "0 0 1 1 *",
                        "execution": {"max_concurrency": 1, "max_attempts": 2},
                        "agents": [{"id": "recon-planner"}],
                        "stages": [{"id": "recon"}],
                    },
                },
            )

            class FakeCao:
                def catalog(self):
                    raise AssertionError("store pages must not read CAO catalog")

            env = {
                "HUTCH_AGENTS_STORE": str(agents_store),
                "HUTCH_FLOWS_STORE": str(flows_store),
            }
            with mock.patch.dict(os.environ, env):
                server = ThreadingHTTPServer(
                    ("127.0.0.1", 0), handler_factory(self.repository, FakeCao())
                )
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base = f"http://127.0.0.1:{server.server_port}"
                    with urllib.request.urlopen(base + "/api/stores/agents") as response:
                        agents = json.load(response)
                    with urllib.request.urlopen(base + "/api/stores/flows") as response:
                        flows = json.load(response)
                finally:
                    server.shutdown()
                    server.server_close()

            self.assertEqual(agents["count"], 1)
            self.assertEqual(agents["agents"][0]["id"], "recon-planner")
            self.assertEqual(
                agents["agents"][0]["skills"],
                ["security-recon", "audit-artifact-management"],
            )
            self.assertEqual(agents["agents"][0]["mcp_servers"][0]["name"], "atlas")
            self.assertEqual(flows["count"], 1)
            self.assertEqual(flows["flows"][0]["id"], "one-run")
            self.assertEqual(flows["flows"][0]["execution"]["max_concurrency"], 1)

    def test_http_api_controls_flow_and_stops_run_session(self):
        state_path = self.runs / "run-001/state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["status"] = "running"
        state["stages"]["audit"]["status"] = "running"
        write_json(state_path, state)

        class FakeCao:
            def __init__(self):
                self.calls = []

            def active_sessions(self):
                return {"cao-flow-audit-flow"}

            def flow_action(self, name, action):
                self.calls.append(("flow", name, action))
                return {"ok": True, "flow": name, "action": action}

            def stop_session(self, session):
                self.calls.append(("stop", session))
                return {"ok": True, "session": session}

        fake = FakeCao()
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), handler_factory(self.repository, fake)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            for path in ("/api/flows/audit-flow/start", "/api/runs/run-001/stop"):
                request = urllib.request.Request(
                    base + path,
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    result = json.load(response)
                self.assertTrue(result["ok"])
            self.assertIn(("flow", "audit-flow", "start"), fake.calls)
            self.assertIn(("stop", "cao-flow-audit-flow"), fake.calls)
            stopped = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(stopped["status"], "stopped")
        finally:
            server.shutdown()
            server.server_close()

    def test_cao_command_parser_rejects_arbitrary_shell(self):
        gateway = CaoGateway("http://127.0.0.1:1")
        with self.assertRaises(CaoGatewayError):
            gateway.execute("rm -rf /tmp/example")

    def test_cao_command_parser_maps_flow_and_launch_to_api(self):
        class RecordingGateway(CaoGateway):
            def __init__(self):
                super().__init__("http://127.0.0.1:1")
                self.requests = []

            def _session_terminal_ids(self, session_name):
                return set()

            def _guard_codex_flow_trust_prompt(
                self, session_name, existing_ids, stop, timeout=90.0
            ):
                return

            def _request(self, method, path, params=None, timeout=30.0):
                self.requests.append((method, path, params))
                return {"id": "created"}

        gateway = RecordingGateway()
        flow = gateway.execute("cao flow run audit-flow")
        launch = gateway.execute(
            f"cao launch audit-flow-auditor --provider opencode_cli "
            f"--session web-audit --working-directory {self.target}"
        )
        self.assertEqual(flow["kind"], "flow")
        self.assertEqual(launch["kind"], "agent")
        self.assertEqual(gateway.requests[0][:2], ("GET", "/flows/audit-flow"))
        self.assertEqual(gateway.requests[1][:2], ("POST", "/flows/audit-flow/run"))
        self.assertEqual(gateway.requests[2][0:2], ("POST", "/sessions"))
        self.assertEqual(
            gateway.requests[2][2]["working_directory"], str(self.target.resolve())
        )

    def test_cao_flow_start_uses_hutch_native_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "agents_store/flow-supervisor"
            workspace.mkdir(parents=True)
            flow_file = root / "flow.md"
            script = root / "prepare.sh"
            script.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '{\"execute\": true, \"output\": {\"run_dir\": \"/tmp/run\"}}'\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            flow_file.write_text(
                "---\n"
                "name: audit-flow\n"
                "schedule: \"0 0 1 1 *\"\n"
                "agent_profile: audit-supervisor\n"
                "provider: codex\n"
                f"working_directory: \"{workspace}\"\n"
                f"script: {script}\n"
                "---\n"
                "Run [[run_dir]].\n",
                encoding="utf-8",
            )

            class RecordingGateway(CaoGateway):
                def __init__(self):
                    super().__init__("http://127.0.0.1:1")
                    self.requests = []

                def _session_terminal_ids(self, session_name):
                    return set()

                def _guard_codex_flow_trust_prompt(
                    self, session_name, existing_ids, stop, timeout=90.0
                ):
                    return

                def _request(self, method, path, params=None, timeout=30.0):
                    self.requests.append((method, path, params))
                    if path == "/flows/audit-flow":
                        return {
                            "name": "audit-flow",
                            "file_path": str(flow_file),
                            "agent_profile": "audit-supervisor",
                            "provider": "codex",
                            "script": str(script),
                        }
                    if path == "/sessions":
                        return {"id": "term-1"}
                    if path == "/terminals/term-1/input":
                        return {"success": True}
                    raise AssertionError((method, path, params))

            gateway = RecordingGateway()

            result = gateway.flow_action("audit-flow", "start")

            self.assertTrue(result["result"]["executed"])
            self.assertEqual(gateway.requests[1][0:2], ("POST", "/sessions"))
            self.assertEqual(
                gateway.requests[1][2]["working_directory"], str(workspace.resolve())
            )
            self.assertEqual(
                gateway.requests[2],
                ("POST", "/terminals/term-1/input", {"message": "Run /tmp/run.\n"}),
            )
            self.assertNotIn(("POST", "/flows/audit-flow/run", None), gateway.requests)

    def test_cao_flow_start_accepts_new_codex_conductor_trust_prompt(self):
        class RecordingGateway(CaoGateway):
            def __init__(self):
                super().__init__("http://127.0.0.1:1")
                self.requests = []

            def _request(self, method, path, params=None, timeout=30.0):
                self.requests.append((method, path, params))
                if path == "/terminals/term-1":
                    return {"id": "term-1", "provider": "codex"}
                if path == "/terminals/term-1/output":
                    return {
                        "output": "Do you trust the files in this folder?\n2. No, quit"
                    }
                return {"success": True}

        gateway = RecordingGateway()

        accepted = gateway._accept_codex_trust_prompt("term-1")

        self.assertTrue(accepted)
        self.assertEqual(
            gateway.requests[-1],
            ("POST", "/terminals/term-1/key", {"key": "Enter"}),
        )

    def test_hutch_normalizes_codex_minutes_spinner_false_completed(self):
        output = (
            "• Confirmed candidates with full source-to-sink traceability: "
            "(2m 29s • esc to interrupt)"
        )

        status = CaoGateway._effective_terminal_status(
            "codex", "completed", output
        )

        self.assertEqual(status, "processing")

    def test_hutch_normalizes_codex_background_terminal_false_completed(self):
        status = CaoGateway._effective_terminal_status(
            "codex",
            "completed",
            "Confirmed · 1 background terminal running · /ps to view",
        )

        self.assertEqual(status, "processing")

    def test_hutch_preserves_real_codex_completed_status(self):
        status = CaoGateway._effective_terminal_status(
            "codex", "completed", "• Final answer\n\n›"
        )

        self.assertEqual(status, "completed")

    def test_cao_active_sessions_normalizes_session_records(self):
        class RecordingGateway(CaoGateway):
            def _request(self, method, path, params=None, timeout=30.0):
                self.assert_request = (method, path)
                return [{"name": "session-a"}, {"session_name": "session-b"}]

        gateway = RecordingGateway("http://127.0.0.1:1")
        self.assertEqual(gateway.active_sessions(), {"session-a", "session-b"})
        self.assertEqual(gateway.assert_request, ("GET", "/sessions"))

    def test_cao_structured_flow_and_stop_actions(self):
        class RecordingGateway(CaoGateway):
            def __init__(self):
                super().__init__("http://127.0.0.1:1")
                self.requests = []

            def _request(self, method, path, params=None, timeout=30.0):
                self.requests.append((method, path, params))
                return {"success": True}

        gateway = RecordingGateway()
        gateway.flow_action("audit-flow", "disable")
        gateway.stop_session("cao-flow-audit-flow")

        self.assertEqual(gateway.requests[0][:2], ("POST", "/flows/audit-flow/disable"))
        self.assertEqual(gateway.requests[1][:2], ("DELETE", "/sessions/cao-flow-audit-flow"))

    def test_project_tree_ui_exposes_persistent_collapse_and_project_only_mode(self):
        static = Path(__file__).resolve().parents[1] / "hutch_dashboard" / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "app.js").read_text(encoding="utf-8")
        styles = (static / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="project-only"', html)
        self.assertIn('id="flow-only"', html)
        self.assertIn('id="sidebar-toggle"', html)
        self.assertIn('id="agents-store"', html)
        self.assertIn('id="flows-store"', html)
        self.assertIn("hutch.collapsed-project-nodes.v1", script)
        self.assertIn("hutch.project-only.v1", script)
        self.assertIn("hutch.flow-only.v1", script)
        self.assertIn("hutch.sidebar-collapsed.v1", script)
        self.assertIn("function updateSidebarCollapseControl", script)
        self.assertIn("function serviceHasFlow", script)
        self.assertIn("function sidebarProjects", script)
        self.assertIn("function updateFlowOnlyControl", script)
        self.assertIn("/api/stores/agents", script)
        self.assertIn("/api/stores/flows", script)
        self.assertIn("function renderAgentsStoreDetail", script)
        self.assertIn("function renderFlowsStoreDetail", script)
        self.assertIn("function bindGraphPan", script)
        self.assertIn('addEventListener("pointerdown"', script)
        self.assertIn("bindGraphPan(svg, layout, applyGraphView)", script)
        self.assertIn("function bindCollapsible", script)
        self.assertIn("if (!state.projectOnly)", script)
        self.assertIn(".store-card-grid", styles)
        self.assertIn(".shell.sidebar-collapsed", styles)
        self.assertIn(".flow-graph.panning", styles)
        self.assertIn(".project-tree-children[hidden]", styles)


if __name__ == "__main__":
    unittest.main()
