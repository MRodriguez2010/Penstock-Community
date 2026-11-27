"""Project permission and deterministic offline receipt comparison regressions."""
from contextlib import closing, ExitStack
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from penstock import Connections, ThreadingHTTPServer, Workspace, initialize, make_handler
from managed_access import ManagedAccess, OWNER, configure
from managed_http import session_cookie
from receipt_replay import replay_receipt
from resources import DEFAULT_PROJECT


class ReceiptReplay(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        initialize(self.root)
        self.workspace = Workspace(self.root, Connections(self.root))

    def save(self, steps, workflow=None, base=None, project=DEFAULT_PROJECT):
        return self.workspace.save({"name": "Test receipt", "steps": steps}, workflow, base, project)

    def start(self, version, value):
        return self.workspace.start(version["workflow"], version["version"], "offline", value, str(uuid4()))

    def selection(self, version, run):
        return {**{field: version[field] for field in ("workflow", "version", "digest", "project")}, "run": run["id"]}

    def simple(self):
        return [{"id": "start", "type": "start", "next": "end"}, {"id": "end", "type": "finish"}]

    def structured(self):
        return {"inputFormat": [{"id": "count", "name": "count", "type": "number", "description": "A count"}],
                "runMetadata": True}

    def test_exact_old_version_keeps_decisions_and_fanout_without_workspace_writes(self):
        original = self.save([
            {"id": "start", "type": "start", "config": self.structured(), "next": "check"},
            {"id": "check", "type": "condition", "config": {"operator": "gte", "right": 2,
             "left": {"ref": {"node": "start", "path": ["count"]}}}, "next": {"true": "review", "false": "end"}},
            {"id": "review", "type": "review", "config": {"question": "Proceed?"},
             "next": {"approved": ["answer", "other"], "declined": "end"}},
            {"id": "answer", "type": "human-input", "config": {"question": "Which receipt name?"},
             "next": "combine"},
            {"id": "other", "type": "variables", "config": {"values": {"source": "test"}}, "next": "combine"},
            {"id": "combine", "type": "response", "config": {"value": {"ref": {"node": "answer"}}}, "next": "end"},
            {"id": "end", "type": "finish"},
        ])
        run = self.start(original, {"count": "3"})
        self.assertEqual(run["state"], "waiting")
        run = self.workspace.decide(run["id"], "review", True)
        run = self.workspace.decide(run["id"], "answer", True, {"name": "Recorded answer"})
        self.assertEqual(run["context"]["result"], {"name": "Recorded answer"})
        self.save(self.simple(), original["workflow"], 1)
        before_db = self.workspace.file.read_bytes()
        with ExitStack() as stack:
            for target, attribute in [(self.workspace, "start"), (self.workspace, "drive"),
                                      (self.workspace.data, "block"), (self.workspace.connections, "complete"),
                                      (self.workspace.connections, "deliver")]:
                stack.enter_context(patch.object(target, attribute, side_effect=AssertionError("Live path called")))
            result = replay_receipt(self.workspace, self.selection(original, run))
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["binding"], original["binding"])
        self.assertEqual(result["digest"], original["digest"])
        self.assertEqual(result["observedState"], "finished")
        self.assertEqual([row["node"] for row in result["steps"]], [s["node"] for s in run["history"]])
        answer = next(row for row in result["steps"] if row["node"] == "answer")
        self.assertEqual(answer["observed"]["output"]["value"], {"name": "Recorded answer"})
        self.assertEqual(answer["basis"], "Recorded decision and answer")
        self.assertFalse(result["outcome"]["changed"])
        self.assertEqual(self.workspace.file.read_bytes(), before_db)
        self.assertEqual(self.workspace.run(run["id"]), run)

    def test_deterministic_start_error_and_readable_drift(self):
        version = self.save([{"id": "start", "type": "start", "config": self.structured(), "next": "end"},
                             {"id": "end", "type": "finish"}])
        run = self.start(version, {"count": "Invalid"})
        result = replay_receipt(self.workspace, self.selection(version, run))
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["observedState"], "failed")
        self.assertIn("Expected a finite number", result["steps"][0]["observed"]["error"]["value"])
        # Simulate an old engine with different error wording in its captured receipt.
        history = run["history"]
        history[0]["error"] = "Old input conversion message."
        with self.workspace.db() as db:
            db.execute("UPDATE runs SET history=? WHERE id=?", (json.dumps(history), run["id"]))
        result = replay_receipt(self.workspace, self.selection(version, run))
        self.assertEqual(result["status"], "different")
        step = result["steps"][0]
        self.assertEqual(step["changes"], ["error"])
        self.assertEqual(step["expected"]["error"]["value"], "Old input conversion message.")
        self.assertIn("Expected a finite number", step["observed"]["error"]["value"])

    def test_external_preview_is_reported_and_never_uses_connector_or_live_writes(self):
        self.workspace.connections.services["example"] = {"url": "https://example.invalid", "protocol": "http"}
        version = self.save([
            {"id": "start", "type": "start", "next": "post"},
            {"id": "post", "type": "service", "config": {"service": "example", "preview": {"queued": True}},
             "next": "end"},
            {"id": "end", "type": "finish"},
        ])
        run = self.start(version, "request")
        before = replay_receipt(self.workspace, self.selection(version, run))
        self.assertEqual(before["status"], "blocked")
        self.assertIn("terminal", before["reason"])
        run = self.workspace.decide(run["id"], "post", True)
        # A later endpoint change must not rewrite the historical binding.
        self.workspace.connections.services["example"]["url"] = "https://later.invalid"
        with patch.object(self.workspace.connections, "deliver", side_effect=AssertionError("Network called")) as call:
            before_db = self.workspace.file.read_bytes()
            result = replay_receipt(self.workspace, self.selection(version, run))
            call.assert_not_called()
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["observedState"])
        self.assertEqual(result["binding"], version["binding"])
        self.assertEqual([step["status"] for step in result["steps"]], ["matched", "blocked", "not-reached"])
        self.assertEqual(result["steps"][1]["code"], "external")
        self.assertIsNone(result["steps"][1]["observed"])
        self.assertEqual(self.workspace.file.read_bytes(), before_db)

    def test_refuses_worker_step_even_with_a_recorded_output_and_checks_graph_digest(self):
        version = self.save([
            {"id": "start", "type": "start", "next": "worker"},
            {"id": "worker", "type": "function", "config": {"code": "return Date.now()"}, "next": "end"},
            {"id": "end", "type": "finish"},
        ])
        with patch("blocks.workers.helper", return_value={"result": 42}):
            run = self.start(version, None)
        with patch("blocks.workers.helper", side_effect=AssertionError("Worker called")):
            result = replay_receipt(self.workspace, self.selection(version, run))
        self.assertEqual(result["steps"][1]["code"], "runtime")
        version["graph"]["nodes"][1]["label"] = "Edited graph"
        with self.workspace.db() as db:
            db.execute("UPDATE versions SET graph=? WHERE workflow=? AND version=?",
                       (json.dumps(version["graph"]), version["workflow"], version["version"]))
        result = replay_receipt(self.workspace, self.selection(version, run))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("could not be verified", result["reason"])
        self.assertEqual(result["steps"], [])

    def test_replay_reads_pinned_table_snapshot_and_reports_missing_snapshot(self):
        def data(action, **fields):
            return self.workspace.data.action(DEFAULT_PROJECT, action, {"key": str(uuid4()), **fields})

        table = data("table", name="Captured", columns=["Name"])["id"]
        data("table-row", id=table, values=["old"])
        version = self.save([
            {"id": "start", "type": "start", "next": "pin"},
            {"id": "pin", "type": "pinned-table", "config": {"tableId": table, "revision": 2}, "next": "end"},
            {"id": "end", "type": "finish"},
        ])
        run = self.start(version, {})
        data("table-row", id=table, values=["new"])
        with patch.object(self.workspace.data, "block", side_effect=AssertionError("Mutable resource used")):
            result = replay_receipt(self.workspace, self.selection(version, run))
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["outcome"]["observed"]["output"]["value"]["rows"], [["old"]])
        with self.workspace.db() as db:
            db.execute("DELETE FROM table_pins WHERE workflow=? AND version=?", (version["workflow"], version["version"]))
        result = replay_receipt(self.workspace, self.selection(version, run))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["steps"][1]["code"], "missing-snapshot")

    def test_http_project_editor_authorization_and_exact_provenance(self):
        other = self.workspace.create_project("Other")["id"]
        first = self.save(self.simple())
        second = self.save(self.simple(), project=other)
        run = self.start(first, "first")
        private = self.start(second, "other")
        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.workspace, "unused")))
        origin = f"http://127.0.0.1:{server.server_port}"
        configure(self.root, origin)
        managed = ManagedAccess(self.root)
        server.RequestHandlerClass = make_handler(self.workspace, "unused", managed)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

        def session(role):
            invite = managed.invite(DEFAULT_PROJECT, f"{role}@example.test", role, "local", OWNER)
            code = parse_qs(urlsplit(invite["url"]).fragment)["invite"][0]
            token = managed.accept_local(code, "A temporary password long enough to test")
            return {"Cookie": f"{session_cookie(managed)}={token}",
                    "X-CSRF-Token": managed.session(token)["csrf"]}

        def request(data, headers):
            with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as client:
                client.request("POST", "/api/receipt-replay", json.dumps(data),
                               {"Content-Type": "application/json", "Origin": origin, **headers})
                response = client.getresponse()
                return response.status, json.loads(response.read())

        editor = session("editor")
        reviewer = session("reviewer")
        known = self.selection(first, run)
        status, result = request(known, editor)
        self.assertEqual((status, result["status"]), (200, "matched"))
        self.assertEqual(request(known, reviewer)[0], 403)
        self.assertEqual(request(known, {"Cookie": editor["Cookie"]})[0], 403)
        self.assertEqual(request(self.selection(second, private), editor)[0], 403)
        self.assertEqual(request({**known, "run": private["id"]}, editor)[0], 403)
        self.assertEqual(request({**known, "version": 2}, editor)[0], 403)
        self.assertEqual(request({**known, "digest": "a" * 64}, editor)[0], 400)
        self.assertEqual(request({**known, "mode": "live"}, editor)[0], 400)
        with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as client:
            client.request("GET", "/src/receiptReplay.js")
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b"Replay receipt offline", response.read())
