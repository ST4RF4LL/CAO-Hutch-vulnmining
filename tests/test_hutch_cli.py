import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import hutch_cli as CLI
from hutch_mcp_control import HutchMcpControl


class FakeClient:
    def __init__(self):
        self.base_url = "http://127.0.0.1:9890"
        self.calls = []

    def get(self, path):
        self.calls.append(("GET", path, None))
        if path == "/api/health":
            return {"status": "ok"}
        if path == "/api/campaigns":
            return [{"instance_id": "camp-1", "status": "completed"}]
        if path == "/api/campaigns/camp-1":
            return {
                "instance_id": "camp-1",
                "deliverables": [
                    {"path": "artifacts/report.md", "content": "report body"}
                ],
            }
        if path == "/api/runs/run-1":
            return {
                "run_id": "run-1",
                "deliverables": [
                    {"path": "artifacts/report.md", "content": "report body"}
                ],
            }
        if path == "/api/runs":
            return [
                {
                    "run_id": "run-1",
                    "status": "running",
                    "project": {"id": "app"},
                },
                {
                    "run_id": "run-2",
                    "status": "completed",
                    "project": {"id": "other"},
                },
            ]
        if path == "/api/cao/catalog":
            return {
                "flows": [{"name": "audit"}],
                "profiles": [{"name": "auditor", "provider": "opencode_cli"}],
            }
        return {"id": "value"}

    def post(self, path, body=None):
        self.calls.append(("POST", path, body))
        return {"ok": True, "path": path, "body": body}


class HutchCliTests(unittest.TestCase):
    def test_flow_list_filters_project_and_status(self):
        client = FakeClient()
        args = argparse.Namespace(project="app", status="running")

        runs = CLI.flow_list(args, client)

        self.assertEqual([run["run_id"] for run in runs], ["run-1"])

    def test_flow_actions_use_structured_hutch_endpoints(self):
        client = FakeClient()
        CLI.flow_action(
            argparse.Namespace(flow_name="audit", flow_action="start"), client
        )
        CLI.flow_stop(argparse.Namespace(run_id="run-1"), client)

        self.assertEqual(client.calls[0][:2], ("POST", "/api/flows/audit/start"))
        self.assertEqual(client.calls[1][:2], ("POST", "/api/runs/run-1/stop"))

    def test_top_level_list_reads_only_local_stores(self):
        parser = CLI.build_parser()
        agent_args = parser.parse_args(["list", "agent"])
        flow_args = parser.parse_args(["list", "flow"])
        client = FakeClient()

        agents = CLI.list_store(agent_args, client)
        flows = CLI.list_store(flow_args, client)

        self.assertIs(agent_args.handler, CLI.list_store)
        self.assertIs(flow_args.handler, CLI.list_store)
        self.assertIn("recon-planner", {agent["id"] for agent in agents})
        self.assertEqual(
            {flow["id"] for flow in flows},
            {"one-run-no-supervisor", "one-run"},
        )
        self.assertEqual(client.calls, [])

    def test_top_level_listi_reads_runtime_instances(self):
        parser = CLI.build_parser()
        agent_args = parser.parse_args(["listi", "agent"])
        flow_args = parser.parse_args(
            ["listi", "flow", "--project", "app", "--status", "running"]
        )
        client = FakeClient()

        agents = CLI.list_instance(agent_args, client)
        flows = CLI.list_instance(flow_args, client)

        self.assertIs(agent_args.handler, CLI.list_instance)
        self.assertIs(flow_args.handler, CLI.list_instance)
        self.assertEqual(agents, [{"name": "auditor", "provider": "opencode_cli"}])
        self.assertEqual([flow["run_id"] for flow in flows], ["run-1"])
        self.assertEqual(client.calls[0][:2], ("GET", "/api/cao/catalog"))
        self.assertEqual(client.calls[1][:2], ("GET", "/api/runs"))

    def test_flow_one_run_compiles_installs_and_optionally_starts(self):
        client = FakeClient()
        args = argparse.Namespace(
            project_directory="/tmp/target",
            name="target-security-review",
            output=None,
            compile_output=None,
            cao_repo="/tmp/cao",
            skill_root=[],
            strict_skills=False,
            provider="codex",
            no_replace=False,
            start=True,
            no_supervisor=False,
        )
        with mock.patch.object(
            CLI,
            "flow_from_template",
            return_value={"ok": True, "name": "target-security-review"},
        ) as render:
            value = CLI.flow_one_run(args, client)

        forwarded = render.call_args.args[0]
        self.assertEqual(forwarded.template, "one-run")
        self.assertTrue(forwarded.compile)
        self.assertTrue(forwarded.install)
        self.assertTrue(forwarded.replace)
        self.assertEqual(forwarded.provider, "codex")
        self.assertTrue(value["installed"])
        self.assertTrue(value["started"])
        self.assertEqual(
            client.calls[-1][:2],
            ("POST", "/api/flows/target-security-review/start"),
        )

    def test_flow_one_run_command_parses_project_directory(self):
        args = CLI.build_parser().parse_args(
            [
                "flow",
                "one_run",
                "/tmp/target",
                "--provider",
                "codex",
                "--no-supervisor",
            ]
        )

        self.assertIs(args.handler, CLI.flow_one_run)
        self.assertEqual(args.project_directory, "/tmp/target")
        self.assertEqual(args.provider, "codex")
        self.assertTrue(args.no_supervisor)

    def test_flow_one_run_no_supervisor_selects_direct_template(self):
        client = FakeClient()
        args = argparse.Namespace(
            project_directory="/tmp/target",
            name=None,
            output=None,
            compile_output=None,
            cao_repo="/tmp/cao",
            skill_root=[],
            strict_skills=False,
            provider="codex",
            no_replace=False,
            start=False,
            no_supervisor=True,
        )
        with mock.patch.object(
            CLI,
            "flow_from_template",
            return_value={"ok": True, "name": "target-one-run-direct"},
        ) as render:
            value = CLI.flow_one_run(args, client)

        self.assertEqual(render.call_args.args[0].template, "one-run-no-supervisor")
        self.assertTrue(value["no_supervisor"])

    def test_project_open_resolves_path_and_preserves_operator_metadata(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(
                path=temporary,
                name="Example",
                project_id="example",
                browser=False,
            )
            CLI.project_open(args, client)

        body = client.calls[0][2]
        self.assertEqual(body["name"], "Example")
        self.assertEqual(body["id"], "example")
        self.assertTrue(Path(body["path"]).is_absolute())

    def test_machine_output_is_valid_json(self):
        value = {"ok": True, "run_id": "run-1"}
        self.assertEqual(json.loads(json.dumps(value)), value)

    def test_mcp_control_bounds_artifact_context_and_reads_exact_artifact(self):
        control = HutchMcpControl("http://127.0.0.1:9890")
        control.client = FakeClient()

        campaign = control.get_campaign("camp-1")
        run = control.get_flow_run("run-1")
        artifact = control.get_flow_artifact("run-1", "artifacts/report.md")

        self.assertTrue(campaign["success"])
        self.assertNotIn("content", campaign["campaign"]["deliverables"][0])
        self.assertNotIn("content", run["run"]["deliverables"][0])
        self.assertEqual(artifact["artifact"]["content"], "report body")

    def test_mcp_control_uses_only_structured_mutation_endpoints(self):
        control = HutchMcpControl("http://127.0.0.1:9890")
        client = FakeClient()
        control.client = client

        control.start_flow("audit flow")
        control.set_flow_schedule("audit flow", False)
        control.stop_flow_run("run/1")

        self.assertEqual(client.calls[0][:2], ("POST", "/api/flows/audit%20flow/start"))
        self.assertEqual(client.calls[1][:2], ("POST", "/api/flows/audit%20flow/disable"))
        self.assertEqual(client.calls[2][:2], ("POST", "/api/runs/run%2F1/stop"))


if __name__ == "__main__":
    unittest.main()
