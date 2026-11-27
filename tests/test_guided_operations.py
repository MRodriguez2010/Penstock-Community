import contextlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import penstock
import operations
import office_oauth
from ops_events import prepare, record_event
from resources import DEFAULT_PROJECT


class GuidedOperations(unittest.TestCase):
    def setUp(self):
        self.scratch=tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root=Path(self.scratch.name)
        penstock.initialize(self.root)
        self.ws=penstock.Workspace(self.root,penstock.Connections(self.root))
        self.other=self.ws.create_project("Other")["id"]
        self.project=DEFAULT_PROJECT
        environment=patch.dict(os.environ,{"TEST_UNUSED":"https://hooks.slack.com/services/synthetic/binding/test",
                                           "TEST_GOOGLE_SECRET":"synthetic-client-secret"})
        environment.start()
        self.addCleanup(environment.stop)
        (self.root/"operations.toml").write_text(f'[projects."{self.project}".slack]\nwebhook_env="TEST_UNUSED"\nstudio_url="https://studio.example.org"\n')

    def request(self, endpoint, **opts):
        return operations.dispatch(self.ws,endpoint,{"project":self.project,**opts},admin=True,owner=True,actor="owner")

    def test_notification_is_bound_to_project_and_single_delivery(self):
        self.request("ops-configure",kind="slack",confirmDelivery=True)
        with self.ws.data.db(self.project) as c:
            record_event(c,self.project,"run:before",1,"finished","Old item",deliver=True,occurred=0)
            for ref,live in (("starter:sample",False),("run:live",True),("run:live",True)):
                record_event(c,self.project,ref,1,"waiting","Test result",deliver=live)
        inbox=self.request("ops-notifications")
        self.assertEqual(len(inbox["items"]),3)
        self.assertEqual(len(inbox["deliveries"]),1)
        self.assertFalse(operations.dispatch(self.ws,"ops-notifications",{"project":self.other})["items"])
        item=inbox["items"][0]
        with self.assertRaises(ValueError):
            operations.dispatch(self.ws,"ops-notification-ack",{"project":self.other,"id":item["id"]})
        with patch("operations.notify") as delivery:
            operations.deliver_next(self.ws,self.project)
            operations.deliver_next(self.ws,self.project)
            delivery.assert_called_once()
        self.assertEqual(self.request("ops-notifications")["deliveries"][0]["status"],"delivered")

    def test_consent_and_credentials_for_connections(self):
        with patch("operations.read_items",return_value=["One"]) as read:
            with self.assertRaises(ValueError):
                self.request("ops-read",kind="project-files")
            with self.assertRaises(ValueError):
                self.request("ops-read",kind="google-drive",consent=True)
            read.assert_not_called()
            self.assertEqual(self.request("ops-read",kind="project-files",consent=True)["items"],["One"])
        with self.assertRaises(PermissionError):
            operations.dispatch(self.ws,"ops-profile-add",{"project":self.project,"name":"new"},admin=True)
        self.request("ops-skill-add",name="triage",instructions="Identify the due date.")
        self.request("ops-profile-add",name="local-runner",url="http://127.0.0.1:11434/api/chat",model="local",protocol="ollama")
        reopened=penstock.Connections(self.root)
        self.assertIn("triage",reopened.skills)
        self.assertIn("local-runner",reopened.providers)
        with self.assertRaises(ValueError):
            self.request("ops-profile-add",name="bad",url="http://example.org/chat",model="local",protocol="ollama")

    def test_repair_uses_exact_failed_version_and_never_publishes(self):
        plan={"name":"Numeric intake","steps":[{"id":"start","type":"start","next":"end","config":{
              "runMetadata":False,"inputFormat":[{"id":"count","name":"count","type":"number","description":"Units"}]}},
              {"id":"end","type":"finish"}]}
        v=self.ws.save(plan)
        for text in ["pending","unknown"]:
            run=self.ws.start(v["workflow"],v["version"],"test",{"count":text},str(uuid.uuid4()))
            self.assertEqual(run["state"],"failed")
        item=self.request("ops-improvements")["items"][0]
        result=self.request("ops-test-change",workflow=item["workflow"],version=item["version"],field=item["field"])
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["results"]),2)
        self.assertEqual(self.ws.version(v["workflow"],v["version"])["published"],0)
        with self.assertRaises(ValueError):
            operations.dispatch(self.ws,"ops-test-change",{"project":self.other,**{k:item[k] for k in ("workflow","version","field")}})

    def test_notification_reauthorization_and_ambiguous_delivery_recovery(self):
        enable={"project":self.project,"kind":"slack","confirmDelivery":True}
        with self.assertRaises(PermissionError):
            operations.dispatch(self.ws,"ops-configure",enable,admin=True,owner=False)
        self.request("ops-configure",kind="slack",confirmDelivery=True)
        with self.ws.data.db(self.project) as c:
            record_event(c,self.project,"run:old-destination",1,"finished","Original route",deliver=True)
        with patch.dict(os.environ,{"TEST_UNUSED":"https://hooks.slack.com/services/synthetic/new-channel/test"}), \
                patch("operations.notify") as send:
            slack=next(item for item in self.request("ops-catalog")["items"] if item["id"]=="slack")
            self.assertTrue(slack["reauthorize"])
            self.assertFalse(slack["enabled"])
            operations.deliver_next(self.ws,self.project)
            send.assert_not_called()
            self.assertEqual(self.request("ops-notifications")["deliveries"][0]["status"],"destination changed")
            self.request("ops-configure",kind="slack",confirmDelivery=True)
            with self.ws.data.db(self.project) as c:
                record_event(c,self.project,"run:ambiguous",1,"finished","Interrupted attempt",deliver=True)
                c.execute("UPDATE ops_outbox SET status='sending' WHERE project=? AND status='queued'",(self.project,))
            operations.recover_outbox(penstock.Workspace(self.root,penstock.Connections(self.root)),self.project)
            operations.deliver_next(self.ws,self.project)
            send.assert_not_called()
            self.assertIn("delivery unconfirmed",[row["status"] for row in self.request("ops-notifications")["deliveries"]])
            with self.ws.data.db(self.project) as c:
                record_event(c,self.project,"run:new-destination",1,"finished","New route",deliver=True)
            operations.deliver_next(self.ws,self.project)
            operations.deliver_next(self.ws,self.project)
            send.assert_called_once()

    def test_journal_preserves_older_run_transitions_and_replays_idempotently(self):
        plan=json.loads((penstock.ROOT/"examples/review.json").read_text())
        version=self.ws.save(plan)
        workflow, edition=version["workflow"],version["version"]
        self.ws.start(workflow,edition,"offline","normal",str(uuid.uuid4()))
        self.ws.publish(workflow,edition)
        waiting=self.ws.start(workflow,edition,"live","urgent",str(uuid.uuid4()))
        self.assertEqual(waiting["state"],"waiting")
        self.request("ops-configure",kind="slack",confirmDelivery=True)
        for _ in range(101):
            self.ws.start(workflow,edition,"offline","normal",str(uuid.uuid4()))
        self.assertEqual(self.ws.decide(waiting["id"],"review",True)["state"],"finished")
        reopened=penstock.Workspace(self.root,penstock.Connections(self.root))
        successful_db=reopened.db
        opens=0

        @contextlib.contextmanager
        def fail_ack_once():
            nonlocal opens
            opens+=1
            if opens==2:
                raise OSError("Simulated loss after inbox commit")
            with successful_db() as c:
                yield c

        with patch.object(reopened,"db",fail_ack_once), self.assertRaisesRegex(OSError,"inbox commit"):
            operations.collect(reopened,self.project)
        self.assertEqual(operations.collect(reopened,self.project),100)
        self.assertEqual(operations.collect(reopened,self.project),4)
        self.assertEqual(operations.collect(reopened,self.project),0)
        with reopened.data.db(self.project) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM ops_events WHERE project=?",(self.project,)).fetchone()[0],104)
            notifications=c.execute("""SELECT e.reference FROM ops_outbox o JOIN ops_events e
                                       ON e.id=o.event AND e.project=o.project WHERE o.project=?""",
                                    (self.project,)).fetchall()
            self.assertEqual([row[0] for row in notifications],["run:"+waiting["id"]])

    def test_legacy_notifications_and_interrupted_runs_are_recovered_without_resending(self):
        plan=json.loads((penstock.ROOT/"examples/review.json").read_text())
        v=self.ws.save(plan)
        receipt=self.ws.start(v["workflow"],v["version"],"offline","normal",str(uuid.uuid4()))
        with self.ws.db() as c:
            c.execute("DROP TABLE run_event_journal")
            c.execute("UPDATE runs SET state='running' WHERE id=?",(receipt["id"],))
        with self.ws.data.db(self.project) as c:
            c.execute("""CREATE TABLE ops_connections(project TEXT,kind TEXT,checked INTEGER,enabled INTEGER,
                         PRIMARY KEY(project,kind))""")
            c.execute("""CREATE TABLE ops_outbox(project TEXT,event TEXT,kind TEXT,status TEXT,
                         PRIMARY KEY(project,event,kind))""")
            c.execute("INSERT INTO ops_connections VALUES(?,?,NULL,1)",(self.project,"slack"))
            c.execute("INSERT INTO ops_outbox VALUES(?,?,?,'queued')",(self.project,"legacy-event","slack"))
        reopened=penstock.Workspace(self.root,penstock.Connections(self.root))
        operations.recover_outbox(reopened,self.project)
        self.assertEqual(reopened.run(receipt["id"])["state"],"interrupted")
        self.assertEqual(operations.collect(reopened,self.project),1)
        data=operations.dispatch(reopened,"ops-notifications",{"project":self.project},admin=True)
        self.assertEqual([row["state"] for row in data["items"]],["interrupted"])
        self.assertEqual([row["status"] for row in data["deliveries"]],["destination changed"])

    def test_source_run_requires_explicit_project_read_even_for_legacy_owner(self):
        plan=self.request("ops-source-draft",kind="project-files")["plan"]
        v=self.ws.save(plan)
        workflow, edition=v["workflow"],v["version"]
        with patch("operations.execute_source",return_value={"items":["fixture"]}) as read:
            offline=self.ws.start(workflow,edition,"offline",{},str(uuid.uuid4()))
            self.assertEqual(self.ws.decide(offline["id"],"read",True)["state"],"finished")
            self.ws.publish(workflow,edition)
            for permissions in (None,{"read":True},{"projectRead":False}):
                run=self.ws.start(workflow,edition,"live",{},str(uuid.uuid4()),permissions=permissions)
                self.assertEqual(run["state"],"failed")
                self.assertIn("live source read permission",run["history"][-1]["error"])
            read.assert_not_called()
            run=self.ws.start(workflow,edition,"live",{},str(uuid.uuid4()),permissions={"projectRead":True})
            self.assertEqual(run["state"],"waiting")
            read.assert_not_called()
            reopened=penstock.Workspace(self.root,penstock.Connections(self.root))
            self.assertEqual(reopened.run(run["id"])["permissions"],{"projectRead":True})
            self.assertEqual(reopened.decide(run["id"],"read",True)["context"]["result"],{"items":["fixture"]})
            read.assert_called_once()

    @unittest.skipUnless(importlib.util.find_spec("cryptography"),"Install the optional office OAuth dependencies.")
    def test_oauth_reader_requires_a_real_project_grant_before_enabling(self):
        origin="https://studio.example.org"
        kind="google-drive"
        with (self.root/"operations.toml").open("a") as c:
            c.write(f'''\n[projects."{self.project}".{kind}]
oauth_origin="{origin}"
oauth_client_id="test.apps.googleusercontent.com"
oauth_client_secret_env="TEST_GOOGLE_SECRET"
''')
        def card():
            return next(i for i in self.request("ops-catalog")["items"] if i["id"]==kind)
        self.assertTrue(card()["signIn"])
        self.assertFalse(card()["enabled"])
        for endpoint in ("ops-configure","ops-source-draft"):
            with self.assertRaisesRegex(ValueError,"Sign in"):
                self.request(endpoint,kind=kind)
        with self.assertRaises(PermissionError):
            operations.dispatch(self.ws,"ops-oauth-start",{"project":self.project,"kind":kind},admin=False)
        session=self.request("ops-oauth-start",kind=kind)
        auth=parse_qs(urlsplit(session["url"]).query)["state"]
        ticket=office_oauth.callback(self.ws,kind,{"state":auth,"code":["test-code"]},session["_browser"],"studio.example.org")
        token={"token_type":"Bearer","access_token":"test-access","refresh_token":"test-refresh","expires_in":3600,
               "scope":office_oauth.SCOPES[kind]}
        with patch("office_oauth.token_request",return_value=token) as exchange, \
                patch("operations.read_items",return_value=["Fixture file"]) as read:
            self.assertTrue(self.request("ops-oauth-finish",kind=kind,state=ticket,_browser=session["_browser"])["connected"])
            exchange.assert_called_once()
            self.assertTrue(card()["enabled"] and card()["signedIn"])
            self.assertIn("plan",self.request("ops-source-draft",kind=kind))
            with self.assertRaisesRegex(ValueError,"Confirm"):
                self.request("ops-read",kind=kind)
            read.assert_not_called()
            self.assertEqual(self.request("ops-read",kind=kind,consent=True)["items"],["Fixture file"])
            self.assertEqual(read.call_args.kwargs["token"],"test-access")
            self.request("ops-revoke",kind=kind)
            self.assertFalse(card()["enabled"] or card()["signedIn"])
            self.assertFalse(office_oauth.status(self.ws,self.other,kind))
