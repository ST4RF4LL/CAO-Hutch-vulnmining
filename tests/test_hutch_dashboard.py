import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from hutch_dashboard.model import RunRepository
from hutch_dashboard.server import handler_factory


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
                "target_fingerprint": {"target": "/src", "head": "abc123"},
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

    def test_reconstructs_completed_flow_agents_sessions_and_text_outputs(self):
        summaries = self.repository.list_runs(status="completed")
        self.assertEqual([item["run_id"] for item in summaries], ["run-001"])
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

    def test_http_api_lists_only_completed_runs(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(self.repository))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(base + "/api/runs") as response:
                runs = json.load(response)
            with urllib.request.urlopen(base + "/api/runs/run-001") as response:
                detail = json.load(response)
            self.assertEqual(runs[0]["workflow"], "audit-flow")
            self.assertEqual(detail["agents"][1]["profile"], "audit-flow-auditor")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
