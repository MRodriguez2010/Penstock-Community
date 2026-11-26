import json
import tempfile
import time
import uuid
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib import request
from urllib.error import HTTPError

from test_workspace import Connections, Workspace, initialize, make_handler, serving
from resources import DEFAULT_PROJECT
from triggers import occurrences

def key():
    return str(uuid.uuid4())


class Projects(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        initialize(self.root)
        self.ws=Workspace(self.root,Connections(self.root))
        self.project=DEFAULT_PROJECT

    def action(self, action, **data):
        data.setdefault("key",key())
        return self.ws.data.action(self.project,action,data)

    def plan(self, steps, project=None):
        all_steps=[{"id":"start","type":"start","next":"n0"}]
        for i,(kind,cfg) in enumerate(steps):
            next_step="n"+str(i+1) if i+1<len(steps) else "done"
            out={"success":next_step,"error":"err"} if kind in ("verify-value","guardrails") else next_step
            all_steps.append({"id":"n"+str(i),"type":kind,"config":cfg,"next":out})
        all_steps.append({"id":"done","type":"finish"})
        if any(s["type"] in ("verify-value","guardrails") for s in all_steps):
            all_steps.append({"id":"err","type":"finish"})
        return self.ws.save({"name":"Owner workflow","steps":all_steps},project=project)["workflow"]

    def run_workflow(self,w,mode="test",value=None):
        return self.ws.start(w,1,mode,value,key())

    def test_migrate_prior_workspace(self):
        w=self.plan([("response",{"value":"ok"})])
        old=self.run_workflow(w)
        with self.ws.db() as c:
            for table in ("run_details","workflow_projects","projects"):
                c.execute("DROP TABLE "+table)
        self.ws=Workspace(self.root,Connections(self.root))
        self.assertEqual(self.ws.version(w,1)["project"],DEFAULT_PROJECT)
        self.assertEqual(self.ws.run(old["id"])["input"],None)
        self.assertEqual(self.ws.publish(w,1)["version"],1)

    def test_tables_csv_concurrency_test_isolation_and_event_lineage(self):
        t=self.action("table",name="Inbox",columns=[{"name":"Title","unique":True},{"name":"Score","type":"number"}])["id"]
        w=self.plan([("table",{"tableId":t,"operation":"insert_row","data":{"ref":{"node":"$input"}}})])
        test=self.run_workflow(w,value={"Title":"Test","Score":2})
        self.assertEqual(test["state"],"finished",test["history"])
        self.assertEqual(self.action("table-read",id=t)["rows"],[])
        self.assertFalse(list((self.root/"previews").glob("*.sqlite3")))
        self.ws.publish(w,1)
        target=self.plan([("response",{"value":{"ref":{"node":"$input","path":["row","Title"]}}})])
        self.run_workflow(target,value={"row":{"Title":"preview"}})
        self.ws.publish(target,1)
        self.ws.triggers.create(self.project,target,1,"table-event",{"tableId":t,"event":"insert"},None)
        self.assertEqual(self.run_workflow(w,"live",{"Title":"Live","Score":3})["state"],"finished")
        self.ws.triggers.tick(self.project,time.time())
        deliveries=self.ws.triggers.list(self.project)["deliveries"]
        self.assertEqual(self.ws.run(deliveries[0]["run"])["context"]["result"],"Live")
        csv=self.action("table-export",id=t)["csv"]
        clone=self.action("table-import",name="Copy",csv=csv)["id"]
        self.assertEqual(self.action("table-read",id=clone)["rows"],[["Live","3"]])
        with self.assertRaises(ValueError):
            self.action("table-import",id=t,expectedRevision=2,csv="Title,Score\nOther,4\nLive,7")
        self.assertEqual(self.action("table-read",id=t)["total"],1)
        other=self.ws.create_project("Second")["id"]
        with self.assertRaises(ValueError):
            self.ws.data.action(other,"table-read",{"id":t})
        self.assertFalse(self.ws.catalog(other)["versions"])

    def test_pins_and_nested_containers(self):
        t=self.action("table",name="Counts",columns=["Name"])["id"]
        self.action("table-row",id=t,values=["original"])
        pinned=self.plan([("pinned-table",{"tableId":t,"revision":2})])
        self.action("table-row",id=t,values=["later"])
        self.assertEqual(self.run_workflow(pinned)["context"]["result"]["rows"],[["original"]])
        body={"name":"Select item","steps":[{"id":"s","type":"start","next":"value"},
              {"id":"value","type":"response","config":{"value":{"ref":{"node":"$input","path":["item"]}}},"next":"end"},
              {"id":"end","type":"finish"}]}
        outer=self.plan([("parallel-container",{"body":body,"items":["first","second"]})])
        test=self.run_workflow(outer)
        self.assertEqual(test["context"].get("result"),["first","second"],test["history"])
        self.ws.publish(outer,1)
        self.assertEqual(self.run_workflow(outer,"live")["context"]["result"],["first","second"])
        self.assertNotIn("Select item",[v["name"] for v in self.ws.catalog()["versions"]])

    def test_cron_dispatch_idempotence_pause_and_restart(self):
        import datetime as dt
        reference=dt.datetime(2026,3,7,12,tzinfo=dt.timezone.utc).timestamp()
        points=occurrences({"scheduleType":"daily","timezone":"America/New_York","dailyTime":"09:00"},reference,3)
        self.assertEqual([dt.datetime.fromtimestamp(p,dt.timezone.utc).hour for p in points],[14,13,13])
        w=self.plan([("response",{"value":"scheduled"})])
        self.run_workflow(w)
        self.ws.publish(w,1)
        t=self.ws.triggers.create(self.project,w,1,"schedule",{"seconds":60},{"type":"interval"})
        at=t["nextDue"]+180
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda _:self.ws.triggers.tick(self.project,at),range(2)))
        self.ws=Workspace(self.root,Connections(self.root))
        self.ws.triggers.tick(self.project,at)
        state=self.ws.triggers.list(self.project)
        self.assertEqual(len(state["deliveries"]),1)
        self.assertEqual(state["triggers"][0]["next_due"],t["nextDue"]+240)
        self.assertEqual(self.ws.run(state["deliveries"][0]["run"])["state"],"finished")
        self.ws.triggers.enable(self.project,t["id"],False)
        self.ws.triggers.tick(self.project,at+1000)
        self.assertEqual(len(self.ws.triggers.list(self.project)["deliveries"]),1)

    def test_knowledge_file_memory_variables_and_children(self):
        w=self.plan([("variables",{"values":{"title":"Spec"}}),("knowledge",{"operation":"create_document","name":{"ref":{"node":"$variables","path":["title"]}},"content":"Local text"}),
                     ("knowledge",{"operation":"list_documents"}),("file",{"operation":"save","name":"report.txt","text":"One"}),
                     ("memory",{"operation":"add","content":"Message"}),("callback",{}),("step-log",{"operation":"write"})])
        r=self.run_workflow(w)
        self.assertEqual(r["state"],"finished",r["history"])
        self.assertEqual(self.ws.data.describe(self.project)["files"],[])
        self.ws.publish(w,1)
        self.assertEqual(self.run_workflow(w,"live")["state"],"finished")
        self.assertEqual(self.ws.data.describe(self.project)["documents"][0]["title"],"Spec")
        child=self.plan([("human-input",{"question":"Enter a value"})])
        pending=self.run_workflow(child)
        self.ws.decide(pending["id"],"n0",True,5)
        self.ws.publish(child,1)
        parent=self.plan([("parallel-each",{"workflow":child,"version":1})])
        p=self.run_workflow(parent,value=["a","b"])
        self.assertEqual(p["state"],"scheduled",p["history"])
        for i,k in enumerate(p["context"]["childRuns"]):
            self.ws.decide(k,"n0",True,i)
        self.ws=Workspace(self.root,Connections(self.root))
        with patch("workspace.time.time",return_value=time.time()+5):
            self.ws.wake()
        self.assertEqual(self.ws.run(p["id"])["context"]["result"],[0,1])

    def test_wait_and_webhook(self):
        w=self.plan([("wait",{"seconds":1})])
        r=self.run_workflow(w)
        self.ws=Workspace(self.root,Connections(self.root))
        with patch("workspace.time.time",return_value=time.time()+5):
            self.ws.wake()
        self.ws.publish(w,1)
        hook=self.ws.triggers.create(self.project,w,1,"webhook",{},None)
        owner=(self.root/"owner.key").read_text().strip()
        with serving(make_handler(self.ws,owner)) as origin:
            def call(token,payload):
                req=request.Request(origin+hook["path"],data=json.dumps(payload).encode(),headers={
                    "Authorization":"Bearer "+token,"Content-Type":"application/json","Idempotency-Key":"event1"})
                with request.urlopen(req) as resp: return json.load(resp)
            with self.assertRaises(HTTPError) as e: call(owner,{"a":1})
            self.assertEqual(e.exception.code,403)
            e.exception.close()
            self.assertEqual(call(hook["token"],{"a":1}),call(hook["token"],{"a":1}))
            with self.assertRaises(HTTPError) as e: call(hook["token"],{"a":2})
            e.exception.close()
        self.ws.triggers.tick(self.project,time.time())
        self.assertEqual(len(self.ws.triggers.list(self.project)["deliveries"]),1)

    @unittest.skipUnless((Path(__file__).resolve().parents[1]/"node_modules").is_dir(),"install optional Node workers")
    def test_function_and_guardrails(self):
        w=self.plan([("function",{"code":"return JSON.stringify({number: params + 1})"}),("guardrails",{"validationType":"json"})])
        r=self.run_workflow(w,value=2)
        self.assertEqual((r["state"],r["context"].get("result")),("finished",'{"number":3}'),r["history"])
