import json
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import tempfile
import time

from test_workspace import Connections, Workspace, initialize, serving
from resources import DEFAULT_PROJECT
from workers import sdk
from uuid import uuid4


class Endpoint(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def reply(self,value):
        raw=json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path=="/items":
            return self.reply({"items":self.items})
        self.reply({"name":"Fixture","version":"1","description":"Local echo",
                    "supportedInterfaces":[{"url":self.origin+"/rpc","protocolBinding":"JSONRPC","protocolVersion":"1.0"}],
                    "capabilities":{"streaming":False,"pushNotifications":False,"extendedAgentCard":False},
                    "defaultInputModes":["text/plain"],"defaultOutputModes":["text/plain"],
                    "skills":[{"id":"echo","name":"Echo","description":"Echo","tags":["test"]}]})

    def do_POST(self):
        msg=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method=msg.get("method")
        if method=="notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        if method=="initialize":
            result={"protocolVersion":msg["params"]["protocolVersion"],"capabilities":{"tools":{}},
                    "serverInfo":{"name":"Fixture","version":"1"}}
        elif method=="tools/list":
            result={"tools":[{"name":"echo","description":"Echo","inputSchema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}}]}
        elif method=="tools/call":
            result={"content":[{"type":"text","text":msg["params"]["arguments"]["text"]}]}
        else:
            raise AssertionError(method)
        self.reply({"jsonrpc":"2.0","id":msg["id"],"result":result})


class Services(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        initialize(self.root)
        class Fixture(Endpoint):
            items=[]
        url=self.enterContext(serving(Fixture))
        Fixture.origin=url
        self.items=Fixture.items
        with (self.root/"connections.toml").open("a") as f:
            f.write(f'\n[services.tools]\nprotocol="mcp"\nurl="{url}/mcp"\ntools=["echo"]\n')
            f.write(f'\n[services.agent]\nprotocol="a2a"\nurl="{url}"\nendpoint_urls=["{url}/rpc"]\n')
            f.write(f'\n[services.items]\nprotocol="http"\nmethod="GET"\nurl="{url}/items"\n')
        self.routes=Connections(self.root)

    @unittest.skipUnless((Path(__file__).resolve().parents[1]/"node_modules").is_dir(),"install optional protocol packages")
    def test_real_sdk_transports(self):
        result=sdk(self.routes,"mcp",{"service":"tools","operation":"call","tool":"echo"},{"text":"hello"},"step")
        self.assertEqual(result["content"][0]["text"],"hello")
        with self.assertRaises(ValueError):
            sdk(self.routes,"mcp",{"service":"tools","operation":"call","tool":"unregistered"},{},"step")
        card=sdk(self.routes,"a2a",{"service":"agent","operation":"a2a_get_agent_card"},None,"step")
        self.assertEqual(card["name"],"Fixture")

    def test_polling_baseline_new_item_restart(self):
        ws=Workspace(self.root,self.routes)
        w=ws.save({"name":"Receive an item","steps":[{"id":"s","type":"start","next":"f"},{"id":"f","type":"finish"}]})["workflow"]
        ws.start(w,1,"test",{},str(uuid4()))
        ws.publish(w,1)
        cfg={"service":"items","seconds":30,"itemsPath":["items"],"idField":"id"}
        t=ws.triggers.create(DEFAULT_PROJECT,w,1,"app-trigger",cfg,None)
        self.items.append({"id":1})
        ws.triggers.tick(DEFAULT_PROJECT,t["nextDue"]+1)
        self.assertEqual(ws.triggers.list(DEFAULT_PROJECT)["deliveries"],[])
        self.items.append({"id":2})
        ws.triggers.tick(DEFAULT_PROJECT,t["nextDue"]+32)
        ws=Workspace(self.root,self.routes)
        ws.triggers.tick(DEFAULT_PROJECT,t["nextDue"]+64)
        deliveries=ws.triggers.list(DEFAULT_PROJECT)["deliveries"]
        self.assertEqual(len(deliveries),1)
        self.assertEqual(ws.run(deliveries[0]["run"])["context"]["result"],{"id":2})
