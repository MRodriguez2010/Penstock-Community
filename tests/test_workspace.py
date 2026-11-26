import contextlib
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from penstock import Connections, Workspace, initialize, make_handler
from workspace import Conflict
from workflows import plan_graph, propose


@contextlib.contextmanager
def serving(handler):
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as http:
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{http.server_port}"
        finally:
            http.shutdown()
            thread.join()


class Studio(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        initialize(self.root)
        self.plan = json.loads((ROOT / "examples/review.json").read_text())
        self.routes = Connections(self.root)
        self.space = Workspace(self.root, self.routes)
        self.v = self.space.save(self.plan)
        self.wid = self.v["workflow"]

    def test_offline_review_version_and_restart(self):
        with self.assertRaises(Conflict):
            self.space.publish(self.wid, 1)
        held = self.space.start(self.wid, 1, "test", "urgent", "first")
        self.assertEqual(held["state"], "waiting")
        self.space = Workspace(self.root, self.routes)
        done = self.space.decide(held["id"], "review", True)
        self.assertEqual(done["state"], "finished")
        self.assertEqual([s["node"] for s in done["history"]], ["intake","route","review","ready"])
        self.assertTrue(self.space.publish(self.wid,1)["published"])
        self.assertEqual(self.space.start(self.wid,1,"test","urgent","first")["id"], held["id"])
        with self.assertRaises(Conflict):
            self.space.start(self.wid,1,"test","different","first")
        with self.assertRaises(Conflict):
            self.space.decide(held["id"], "review", True)
        self.assertEqual(self.space.start(self.wid,1,"live","routine","two")["context"]["result"],"routine")
        self.assertEqual(self.space.save(self.plan,self.wid,1)["version"],2)
        with self.assertRaises(Conflict):
            self.space.save(self.plan,self.wid,1)
        with self.assertRaises(Conflict):
            self.space.publish(self.wid,2)

    def test_fanout_join_and_reject_cycle(self):
        plan = {"name":"Join branches","steps":[
            {"id":"start","type":"start","next":{"success":["left","right"]}},
            *[{"id":name,"type":"condition","config":{"contains":"x"},
               "next":{"true":"finish","false":"finish"}} for name in ("left","right")],
            {"id":"finish","type":"finish"}]}
        v = self.space.save(plan)
        r = self.space.start(v["workflow"],1,"test","x","fanout")
        self.assertEqual([s["node"] for s in r["history"]], ["start","left","right","finish"])
        plan["steps"][2]["next"]["true"]="right"
        with self.assertRaises(ValueError):
            plan_graph(plan,self.routes)

    def test_http_and_workspace_privacy(self):
        key = (self.root/"owner.key").read_text().strip()
        initialize(self.root)
        self.assertEqual((self.root/"owner.key").read_text().strip(), key)
        with serving(make_handler(self.space,key)) as url:
            for path,status in [("/owner.key",404),("/.workspace/owner.key",404),("/src/../server/workspace.py",404),("/api/workspace",401)]:
                with self.assertRaises(urllib.error.HTTPError) as exc:
                    urllib.request.urlopen(url+path)
                self.assertEqual(exc.exception.code,status)
                exc.exception.close()
            self.assertIn(b"Workflow studio",urllib.request.urlopen(url+"/").read())
            req = urllib.request.Request(url+"/api/workspace",headers={"Authorization":"Bearer "+key})
            self.assertIn(self.wid,urllib.request.urlopen(req).read().decode())
            req = urllib.request.Request(url+"/api/save",data=b"{}",headers={"Origin":"https://another.example",
                        "Content-Type":"application/json","Authorization":"Bearer "+key})
            with self.assertRaises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req)
            self.assertEqual(exc.exception.code,403)
            exc.exception.close()

    def test_model_protocols_skill_validation_and_delivery_review(self):
        received = []
        plan = {"name":"Summarize and send","steps":[
            {"id":"start","type":"start","next":"summarize"},
            {"id":"summarize","type":"agent","config":{"provider":"chat","instructions":"Summarize",
              "skill":"summarize","schema":{"type":"object","properties":{"text":{"type":"string"}},
              "required":["text"],"additionalProperties":False}},"next":"send"},
            {"id":"send","type":"service","config":{"service":"inbox"},"next":"finish"},
            {"id":"finish","type":"finish"}]}
        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append((self.path,data,dict(self.headers)))
                planning = "Return only a JSON object with name" in str(data)
                answer = json.dumps(plan) if planning else '{"text":"Summary"}'
                payload = {"/chat":{"choices":[{"message":{"content":answer}}]},
                           "/ollama":{"message":{"content":answer}},
                           "/messages":{"content":[{"type":"text","text":answer}]},
                           "/inbox":{"accepted":True}}[self.path]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
        with serving(Endpoint) as url, patch.dict(os.environ, {"TEST_ENDPOINT_KEY":"synthetic-test-only"}):
            config=(self.root/"connections.toml")
            with config.open("a") as out:
                for name,protocol in [("chat","chat-completions"),("ollama","ollama"),("messages","anthropic-messages")]:
                    out.write(f'\n[providers.{name}]\nprotocol="{protocol}"\nurl="{url}/{name}"\nmodel="fixture"\nkey_env="TEST_ENDPOINT_KEY"\n')
                out.write(f'\n[services.inbox]\nurl="{url}/inbox"\n')
            routes=Connections(self.root)
            for name in routes.providers:
                self.assertEqual(propose(routes,name,"Summarize the request"),plan)
            self.assertEqual(received[0][2]["Authorization"], "Bearer synthetic-test-only")
            self.assertEqual(received[2][2]["X-Api-Key"], "synthetic-test-only")
            self.space=Workspace(self.root,routes)
            wid=self.space.save(plan)["workflow"]
            r=self.space.start(wid,1,"test","A request","a")
            self.assertIn("Keep the original facts",received[-1][1]["messages"][0]["content"])
            self.assertEqual(r["context"]["pending"]["value"],{"text":"Summary"})
            test=self.space.decide(r["id"],"send",True)
            self.assertEqual(test["state"],"finished")
            self.assertNotIn("/inbox",[p for p,*_ in received])
            self.space.publish(wid,1)
            r=self.space.start(wid,1,"live","A request","b")
            self.assertEqual(r["state"],"waiting")
            def decide(_):
                try: return self.space.decide(r["id"],"send",True)["state"]
                except Conflict: return "conflict"
            with ThreadPoolExecutor(2) as pool:
                self.assertCountEqual(pool.map(decide,range(2)),["finished","conflict"])
            self.assertEqual([body for p,body,_ in received if p=="/inbox"],[{"input":{"text":"Summary"}}])

if __name__=="__main__":
    unittest.main()
