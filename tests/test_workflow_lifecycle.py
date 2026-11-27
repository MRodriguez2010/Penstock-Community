from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from penstock import Connections, ThreadingHTTPServer, Workspace, initialize, make_handler
from graph_table_events import enqueue
from managed_access import ManagedAccess, OWNER, configure
from managed_http import session_cookie
from resources import DEFAULT_PROJECT
from workflow_lifecycle import LifecycleConflict, dispatch


class WorkflowLifecycle(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        initialize(self.root)
        self.workspace = Workspace(self.root, Connections(self.root))
        self.first = self.publish("First")
        self.workflow = self.first["workflow"]
        self.second = self.publish("Second", self.workflow, 1)

    def plan(self, label):
        return {"name": label, "steps": [
            {"id": "start", "type": "start", "next": "data"},
            {"id": "data", "type": "response", "config": {"value": label}, "next": "finish"},
            {"id": "finish", "type": "finish"},
        ]}

    def publish(self, label, workflow=None, base=None, *, plan=None):
        version = self.workspace.save(plan or self.plan(label), workflow, base)
        self.workspace.start(version["workflow"], version["version"], "offline", "sample", str(uuid4()))
        return self.workspace.publish(version["workflow"], version["version"])

    def request(self, action, **fields):
        return dispatch(self.workspace, "workflow-lifecycle-" + action, {
            "project": DEFAULT_PROJECT, "workflow": self.workflow, **fields
        }, actor="administrator")

    def state(self):
        return self.request("state")

    def change(self, action, **fields):
        state = self.state()
        return self.request(action, expectedGeneration=state["generation"], request=str(uuid4()), **fields)

    def schedule(self, version=2):
        trigger = self.workspace.triggers.create(DEFAULT_PROJECT, self.workflow, version, "schedule",
                                                    {"seconds": 3600}, "scheduled input")
        return trigger["id"]

    def test_recorded_active_rollback_controls_live_launch_without_retargeting_schedule(self):
        schedule = self.schedule()
        receipt = self.change("rollback", version=1, schedules=[])
        self.assertEqual(receipt["fromVersion"], 2)
        self.assertEqual(receipt["activeVersion"], 1)
        self.assertEqual(self.state()["schedules"][0]["version"], 2)
        self.assertEqual(self.workspace.version(self.workflow, 2)["activeVersion"], 1)
        with self.assertRaisesRegex(LifecycleConflict, "active release changed"):
            self.workspace.start(self.workflow, 2, "live", "sample", "stale", active_release=True)
        live = self.workspace.start(self.workflow, 1, "live", "sample", "current", active_release=True)
        self.assertEqual(live["context"]["result"], "First")
        legacy = self.workspace.start(self.workflow, 2, "live", "sample", "explicit-legacy")
        self.assertEqual(legacy["version"], 2)
        self.workspace = Workspace(self.root, Connections(self.root))
        self.assertEqual(self.state()["activeVersion"], 1)
        self.assertEqual(self.state()["schedules"][0]["id"], schedule)
        self.assertEqual(self.state()["schedules"][0]["version"], 2)

    def test_schedule_rebind_is_explicit_atomic_and_deliveries_keep_event_versions(self):
        schedule, other = sorted((self.schedule(), self.schedule()))
        due = self.state()["schedules"][0]["nextDue"]
        self.workspace.triggers.enable(DEFAULT_PROJECT, other, False)
        with self.workspace.data.db(DEFAULT_PROJECT) as c:
            stored = dict(c.execute("SELECT * FROM triggers WHERE id=?", (schedule,)).fetchone())
            enqueue(c, stored, {"old": True}, [], "already-queued")
        published = self.state()
        with self.assertRaisesRegex(LifecycleConflict, "reviewed schedule changed"):
            self.change("rollback", version=1, schedules=[
                {"id": schedule, "version": 2, "enabled": True},
                {"id": other, "version": 2, "enabled": True},
            ])
        self.assertEqual(self.state()["activeVersion"], published["activeVersion"])
        self.assertTrue(all(t["version"] == 2 for t in self.state()["schedules"]))
        actual = self.change("rollback", version=1, schedules=[
            {"id": schedule, "version": 2, "enabled": True},
            {"id": other, "version": 2, "enabled": False},
        ])
        self.assertEqual(len(actual["schedules"]), 2)
        records = {t["id"]: t for t in self.state()["schedules"]}
        self.assertEqual(records[schedule]["version"], 1)
        self.assertFalse(records[other]["enabled"])
        self.assertGreaterEqual(records[schedule]["nextDue"], due - 1)
        self.workspace.triggers.tick(DEFAULT_PROJECT, time.time())
        with self.workspace.data.db(DEFAULT_PROJECT) as c:
            queued = dict(c.execute("SELECT * FROM deliveries WHERE id='already-queued'").fetchone())
            self.assertEqual((queued["version"], queued["state"]), (2, "dispatched"))
        self.assertEqual(self.workspace.run(queued["run"])["context"]["result"], "Second")

    def test_archive_preserves_receipts_and_recovery_keeps_trigger_paused(self):
        schedule = self.schedule()
        previous = self.workspace.start(self.workflow, 2, "live", {}, "before", active_release=True)
        with self.workspace.data.db(DEFAULT_PROJECT) as c:
            trigger = dict(c.execute("SELECT * FROM triggers WHERE id=?", (schedule,)).fetchone())
            enqueue(c, trigger, {}, [], "not-yet-admitted")
        state = self.state()
        payload = {"expectedGeneration": state["generation"], "request": "archive-one"}
        receipt = self.request("archive", **payload)
        self.assertEqual(self.request("archive", **payload), receipt)
        self.assertEqual((receipt["pausedTriggers"], receipt["pausedDeliveries"]), (1, 1))
        self.assertTrue(self.workspace.version(self.workflow, 2)["archived"])
        self.assertEqual(self.workspace.run(previous["id"])["history"], previous["history"])
        for action in (
            lambda: self.workspace.start(self.workflow, 2, "offline", {}, "archived"),
            lambda: self.workspace.start(self.workflow, 2, "live", {}, "archived-live"),
            lambda: self.workspace.save(self.plan("Changed"), self.workflow, 2),
            lambda: self.workspace.publish(self.workflow, 2),
            lambda: self.workspace.triggers.enable(DEFAULT_PROJECT, schedule, True),
            self.schedule,
        ):
            with self.assertRaisesRegex(LifecycleConflict, "archived"):
                action()
        self.change("recover")
        state = self.state()
        self.assertFalse(state["archived"])
        self.assertIsNone(state["activeVersion"])
        self.assertFalse(state["schedules"][0]["enabled"])
        with self.assertRaisesRegex(LifecycleConflict, "Activate a published release"):
            self.workspace.start(self.workflow, 2, "live", {}, "paused-legacy")
        self.change("rollback", version=1, schedules=[])
        self.assertFalse(self.state()["schedules"][0]["enabled"])
        with self.workspace.data.db(DEFAULT_PROJECT) as c:
            self.assertEqual(c.execute("SELECT state FROM deliveries WHERE id='not-yet-admitted'").fetchone()[0], "paused")
        with self.assertRaisesRegex(LifecycleConflict, "no longer ready"):
            self.workspace.start(self.workflow, 2, "live", {}, "not-yet-admitted", trigger_delivery=True)

    def test_unpublished_wrong_project_and_concurrent_confirmations_are_rejected(self):
        elsewhere = self.workspace.create_project("Other")["id"]
        with self.assertRaisesRegex(ValueError, "workflow in this project"):
            self.request("state", project=elsewhere)
        draft = self.workspace.save(self.plan("Still draft"), self.workflow, 2)
        with self.assertRaisesRegex(ValueError, "previously published"):
            self.change("rollback", version=draft["version"], schedules=[])
        height = self.state()["generation"]
        self.workspace.save(self.plan("Another draft edit"), self.workflow, 3)
        with self.assertRaisesRegex(LifecycleConflict, "changed"):
            self.request("archive", expectedGeneration=height, request="old")
        current = self.state()["generation"]

        def archive(index):
            try:
                return self.request("archive", expectedGeneration=current, request=f"parallel-{index}")["action"]
            except LifecycleConflict:
                return "stale"

        with ThreadPoolExecutor(max_workers=2) as workers:
            self.assertCountEqual(workers.map(archive, range(2)), ["stale", "archive"])

    def test_archive_blocks_review_and_wake_without_contacting_provider(self):
        plan = {"name": "Wait and review", "steps": [
            {"id": "start", "type": "start", "next": "wait"},
            {"id": "wait", "type": "wait", "config": {"seconds": 1}, "next": "review"},
            {"id": "review", "type": "review", "config": {"question": "Proceed?"},
             "next": {"approved": "finish", "declined": "finish"}},
            {"id": "finish", "type": "finish"},
        ]}
        version = self.workspace.save(plan, self.workflow, 2)
        run = self.workspace.start(self.workflow, version["version"], "offline", "does not leave the workspace", "waiting")
        self.assertEqual(run["state"], "scheduled")
        self.change("archive")
        with patch("workspace.time.time", return_value=time.time() + 100):
            self.workspace.wake()
            self.assertEqual(self.workspace.run(run["id"])["state"], "scheduled")
            self.change("recover")
            self.workspace.wake()
            self.assertEqual(self.workspace.run(run["id"])["state"], "waiting")
        self.change("archive")
        with self.assertRaisesRegex(LifecycleConflict, "archived"):
            self.workspace.decide(run["id"], "review", True)
        self.assertEqual(self.workspace.run(run["id"])["state"], "waiting")

    def test_http_roles_csrf_exact_active_and_authenticated_rebind(self):
        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.workspace, "unused")))
        origin = f"http://127.0.0.1:{server.server_port}"
        configure(self.root, origin)
        managed = ManagedAccess(self.root)
        server.RequestHandlerClass = make_handler(self.workspace, "unused", managed)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)

        def session(role):
            invite = managed.invite(DEFAULT_PROJECT, f"{role}@example.test", role, "local", OWNER)
            code = parse_qs(urlsplit(invite["url"]).fragment)["invite"][0]
            token = managed.accept_local(code, "A temporary password long enough to test")
            identity = managed.session(token)
            return {"Cookie": f"{session_cookie(managed)}={token}", "X-CSRF-Token": identity["csrf"]}

        def post(endpoint, data, headers, expected):
            with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as client:
                client.request("POST", "/api/" + endpoint, json.dumps(data), {
                    "Content-Type": "application/json", "Origin": origin, **headers
                })
                response = client.getresponse()
                result = json.loads(response.read())
                self.assertEqual(response.status, expected, result)
                return result

        operator = session("operator")
        admin = session("admin")
        schedule = self.schedule()
        reference = {"project": DEFAULT_PROJECT, "workflow": self.workflow}
        state = post("workflow-lifecycle-state", reference, operator, 200)
        rollback = {**reference, "version": 1, "expectedGeneration": state["generation"], "request": "release",
                    "schedules": [{"id": schedule, "version": 2, "enabled": True}]}
        post("workflow-lifecycle-rollback", rollback, operator, 403)
        post("workflow-lifecycle-rollback", rollback, {"Cookie": admin["Cookie"]}, 403)
        receipt = post("workflow-lifecycle-rollback", rollback, admin, 200)
        self.assertEqual(receipt["activeVersion"], 1)
        self.assertEqual(receipt["schedules"][0]["to"], 1)
        attempted = {"workflow": self.workflow, "version": 2, "mode": "live",
                     "input": "sample", "request": "wrong-active", "activeRelease": False}
        post("start", attempted, operator, 409)
        result = post("start", {**attempted, "version": 1, "request": "right-active"}, operator, 200)
        self.assertEqual(result["context"]["result"], "First")
