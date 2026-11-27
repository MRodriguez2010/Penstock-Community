"""Exercise the starter journeys at their application boundary and durable project stores."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from urllib import request
from urllib.error import HTTPError

from test_workspace import Connections, Workspace, initialize, make_handler, serving
from resources import DEFAULT_PROJECT
from starter_api import StarterConflict, dispatch
from starter_recipes import BRIEF, INTAKE


class GuidedStudio(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        initialize(self.root)
        self.workspace = Workspace(self.root, Connections(self.root))

    def call(self, endpoint, **body):
        project = body.pop("project", DEFAULT_PROJECT)
        return dispatch(self.workspace, "starter-" + endpoint, {"project": project, **body})

    def start(self, kind, inputs=None, **fields):
        data = {"kind": kind, "version": 1, "source": "sample", "request": str(uuid.uuid4()),
                "input": {} if inputs is None else inputs, **fields}
        return self.call("run", **data)

    def sample_dates(self):
        return {"from": "2026-11-16", "through": "2026-11-22"}

    def test_brief_period_writes_approved_local_file_and_records_sources(self):
        original = self.start(BRIEF, self.sample_dates())
        next_week = self.start(BRIEF, {"from": "2026-11-23", "through": "2026-11-29"})
        self.assertEqual(original["status"], "awaiting-review")
        self.assertEqual(original["output"]["counts"], {"blocked": 1, "complete": 2, "in-progress": 1})
        self.assertEqual(original["output"]["decisions"][0]["source"], "Rollout review · Nov 17")
        self.assertEqual(original["output"]["overdue"][0]["title"], "Waiting for regional cost estimates")
        self.assertEqual(next_week["output"]["total"], 3)
        self.assertEqual(next_week["output"]["decisions"][0]["title"], "Extend coverage to the Northeast")
        self.assertEqual(next_week["output"]["overdue"][0]["title"], "Verifying the refreshed forecast")
        self.assertEqual(self.workspace.data.describe(DEFAULT_PROJECT)["files"], [])

        approved = self.call("decide", id=original["id"], approve=True)
        saved = self.workspace.data.action(DEFAULT_PROJECT, "file-read", {"id": approved["effect"]["id"]})
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(saved["text"], original["output"]["markdown"])
        for phrase in ("## Decisions", "## Overdue items", "Rollout review · Nov 17", "2026-11-16 to 2026-11-22"):
            self.assertIn(phrase, saved["text"])
        self.assertEqual(self.call("decide", id=original["id"], approve=True)["effect"], approved["effect"])
        self.assertEqual(len(self.workspace.data.describe(DEFAULT_PROJECT)["files"]), 1)
        with self.assertRaises(StarterConflict):
            self.call("decide", id=original["id"], approve=False)

    def test_version_settings_and_receipts_survive_restart(self):
        first = self.start(BRIEF, self.sample_dates())
        base = next(recipe for recipe in self.call("catalog")["starters"] if recipe["kind"] == BRIEF)
        settings = {**base["settings"], "title": "Team update", "includeOwners": False}
        changed = self.call("save", kind=BRIEF, baseVersion=1, settings=settings)
        self.assertEqual(changed["version"], 2)
        with self.assertRaises(StarterConflict):
            self.call("save", kind=BRIEF, baseVersion=1, settings=settings)
        second = self.start(BRIEF, self.sample_dates(), version=2)
        self.assertIn("# Team update", second["output"]["markdown"])
        self.assertNotIn("(Operations)", second["output"]["markdown"])
        self.workspace = Workspace(self.root, Connections(self.root))
        reread = self.call("read", id=first["id"])
        self.assertEqual(reread["settings"]["title"], "Weekly executive brief")
        self.assertEqual(reread["version"], 1)
        self.assertEqual(reread["history"][-1]["state"], "waiting")

    def test_intake_review_never_writes_on_decline_and_approve_is_atomic(self):
        denied = self.start(INTAKE)
        self.assertEqual(self.call("decide", id=denied["id"], approve=False)["status"], "declined")
        self.assertEqual(self.workspace.data.describe(DEFAULT_PROJECT)["tables"], [])

        pending = self.start(INTAKE, {"title": "Replace the intake template", "requester": "Ava Chen",
                                      "priority": "urgent", "notes": "Review by Friday"}, source="custom")
        with ThreadPoolExecutor(max_workers=2) as pool:
            decisions = list(pool.map(lambda _: self.call("decide", id=pending["id"], approve=True), range(2)))
        self.assertEqual(decisions[0]["effect"], decisions[1]["effect"])
        table_id = decisions[0]["effect"]["id"]
        table = self.workspace.data.action(DEFAULT_PROJECT, "table-read", {"id": table_id})
        self.assertEqual(table["rows"], [["Replace the intake template", "Ava Chen", "Urgent",
                                          "Review by Friday", "User input", "Approved"]])
        with self.workspace.data.db(DEFAULT_PROJECT) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM ops_outbox").fetchone()[0], 0)
            receipts = connection.execute("SELECT reference,state FROM ops_events WHERE project=?",
                                          (DEFAULT_PROJECT,)).fetchall()
            for run_id, expected in ((denied["id"], "declined"), (pending["id"], "finished")):
                states = {row["state"] for row in receipts if row["reference"] == "starter:" + run_id}
                self.assertEqual(states, {"waiting", expected})

        version = self.workspace.save({"name": "Follow intake changes", "steps": [
            {"id": "start", "type": "start", "next": "finish"}, {"id": "finish", "type": "finish"},
        ]})
        self.workspace.start(version["workflow"], version["version"], "test", {}, str(uuid.uuid4()))
        self.workspace.publish(version["workflow"], version["version"])
        self.workspace.triggers.create(DEFAULT_PROJECT, version["workflow"], version["version"], "table-event",
                                       {"tableId": table_id, "event": "insert"}, None)
        second = self.start(INTAKE)
        self.assertEqual(self.call("decide", id=second["id"], approve=True)["effect"]["id"], table_id)
        self.assertEqual(self.workspace.triggers.list(DEFAULT_PROJECT)["deliveries"], [])

    def test_changes_record_reason_and_actor_and_link_an_immutable_new_preview(self):
        original = self.start(BRIEF, self.sample_dates())
        command = {"project": DEFAULT_PROJECT, "id": original["id"], "reason": "Include the next period."}
        with self.assertRaises(PermissionError):
            dispatch(self.workspace, "starter-request-changes", command, can_review=False)
        sent = dispatch(self.workspace, "starter-request-changes", command,
                        actor={"id": "supervisor", "displayName": "Reviewer"})
        self.assertEqual(sent["status"], "changes-requested")
        self.assertEqual(sent["changeRequest"]["reason"], "Include the next period.")
        self.assertEqual(sent["changeRequest"]["actor"]["id"], "supervisor")
        self.assertEqual(sent["output"], original["output"])
        self.assertEqual(self.workspace.data.describe(DEFAULT_PROJECT)["files"], [])
        with self.assertRaises(StarterConflict):
            self.call("decide", id=original["id"], approve=True)
        self.workspace = Workspace(self.root, Connections(self.root))
        new_dates = {"from": "2026-11-23", "through": "2026-11-29"}
        request_id = str(uuid.uuid4())
        revised = self.start(BRIEF, new_dates, revises=original["id"], request=request_id)
        self.assertEqual(revised["revises"], original["id"])
        self.assertEqual(revised["status"], "awaiting-review")
        self.assertNotEqual(revised["output"], original["output"])
        self.assertEqual(self.start(BRIEF, new_dates, revises=original["id"], request=request_id)["id"], revised["id"])
        self.assertEqual(self.call("read", id=original["id"])["revisedBy"], revised["id"])
        with self.assertRaises(StarterConflict):
            self.start(BRIEF, new_dates, revises=original["id"])
        with self.workspace.data.db(DEFAULT_PROJECT) as connection:
            event = connection.execute("SELECT state FROM ops_events WHERE reference=? AND state=?",
                                       ("starter:" + original["id"], "changes-requested")).fetchone()
            self.assertIsNotNone(event)
            self.assertEqual(connection.execute("SELECT count(*) FROM ops_outbox").fetchone()[0], 0)

    def test_receipt_and_request_ids_are_scoped_even_in_shared_database(self):
        other = self.workspace.create_project("Other project")["id"]
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row

        class DB:
            @contextmanager
            def db(self, project):
                with db:
                    yield db

        class SharedWorkspace:
            data = DB()

            @staticmethod
            def require_project(project):
                if project not in (DEFAULT_PROJECT, other):
                    raise ValueError("Unknown project.")

        same = {"kind": INTAKE, "version": 1, "source": "sample", "request": str(uuid.uuid4()), "input": {}}
        first = dispatch(SharedWorkspace(), "starter-run", {"project": DEFAULT_PROJECT, **same})
        second = dispatch(SharedWorkspace(), "starter-run", {"project": other, **same})
        self.assertNotEqual(first["id"], second["id"])
        with self.assertRaisesRegex(ValueError, "unavailable"):
            dispatch(SharedWorkspace(), "starter-read", {"project": other, "id": first["id"]})

    def test_authorized_http_handlers_and_bad_period_recovery(self):
        key = "tester-" + str(uuid.uuid4())
        with serving(make_handler(self.workspace, key)) as address:
            url = address + "/api/"

            def post(path, data, authorized=True):
                headers = {"Content-Type": "application/json"}
                if authorized:
                    headers["Authorization"] = "Bearer " + key
                req = request.Request(url + path, data=json.dumps(data).encode(), headers=headers)
                with request.urlopen(req, timeout=4) as response:
                    return json.load(response)

            with self.assertRaises(HTTPError) as denied:
                post("starter-catalog", {"project": DEFAULT_PROJECT}, authorized=False)
            self.assertEqual(denied.exception.code, 401)
            denied.exception.close()
            self.assertEqual([v["kind"] for v in post("starter-catalog", {"project": DEFAULT_PROJECT})["starters"]],
                             [BRIEF, INTAKE])
            payload = {"project": DEFAULT_PROJECT, "kind": BRIEF, "version": 1, "source": "sample",
                       "request": str(uuid.uuid4()), "input": {"from": "2026-11-23", "through": "2026-11-01"}}
            with self.assertRaises(HTTPError) as bad_request:
                post("starter-run", payload)
            self.assertIn("period start", json.load(bad_request.exception)["error"])
            bad_request.exception.close()
            payload["input"] = self.sample_dates()
            run = post("starter-run", payload)
            self.assertEqual(post("starter-run", payload)["id"], run["id"])
            self.assertEqual(post("starter-read", {"project": DEFAULT_PROJECT, "id": run["id"]})["status"], "awaiting-review")

        with self.assertRaises(PermissionError):
            dispatch(self.workspace, "starter-decide", {"project": DEFAULT_PROJECT, "id": run["id"], "approve": True},
                     can_review=False)
        with self.assertRaises(PermissionError):
            dispatch(self.workspace, "starter-run", payload, can_edit=False)
        self.assertEqual(self.call("read", id=run["id"])["status"], "awaiting-review")


if __name__ == "__main__":
    unittest.main()
