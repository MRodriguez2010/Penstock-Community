"""Run the guided browser journeys against disposable loopback services."""
import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import penstock
from managed_access import configure, ManagedAccess
from resources import DEFAULT_PROJECT


class FixtureModel(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/lookup":
            self.server.lookup_calls += 1
            self.send_error(409)
            return
        message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path != "/chat" or message["messages"][-1]["content"] != self.server.job:
            self.send_error(400)
            return
        self.server.calls += 1
        plan = {"name": "Prepare a board update", "steps": [
            {"id": "start", "type": "start", "next": "review"},
            {"id": "review", "type": "review", "config": {"question": "Approve this board update?"},
             "next": {"approved": "finish", "declined": "finish"}},
            {"id": "finish", "type": "finish"},
        ]}
        result = json.dumps({"message": {"content": json.dumps(plan)}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)

    def log_message(self, *args):
        pass


def start(stack, server):
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stack.callback(thread.join)
    stack.callback(server.shutdown)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screens", type=Path, help="Save synthetic captures in this new directory")
    parser.add_argument("--clips", action="store_true", help="Record short browser videos in the capture directory (requires ffmpeg)")
    args = parser.parse_args()
    if args.clips and not args.screens:
        parser.error("--clips requires --screens")
    if args.screens:
        args.screens.mkdir(parents=True)
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="penstock-browser-")))
        model = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), FixtureModel))
        model.job = "Prepare a board update from a reviewed request."
        model.calls = 0
        model.lookup_calls = 0
        start(stack, model)
        penstock.initialize(root)
        (root / "connections.toml").write_text(
            f'[providers.sample]\nprotocol="ollama"\nurl="http://127.0.0.1:{model.server_port}/chat"\nmodel="fixture"\n'
        )
        with (root / "connections.toml").open("a") as installed:
            installed.write(f'\n[lookups.sample-repositories]\nkind="github"\nurl="http://127.0.0.1:{model.server_port}/lookup"\nprojects=["{DEFAULT_PROJECT}"]\n')
        workspace = penstock.Workspace(root, penstock.Connections(root))
        workspace.save(json.loads((SOURCE / "examples/review.json").read_text()))
        import typed_tables
        with workspace.data.db(DEFAULT_PROJECT) as c:
            table=typed_tables.new_table(c, DEFAULT_PROJECT, DEFAULT_PROJECT, {
                "name":"Operations updates","columns":["Date","Title","Status"]})
            typed_tables.append(c,DEFAULT_PROJECT,DEFAULT_PROJECT,{
                "id":table["id"],"values":["2026-11-16","Confirm renewal date","Completed"]})
        key = (root / "owner.key").read_text().strip()
        http = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), None))
        origin = f"http://127.0.0.1:{http.server_port}"
        configure(root, origin)
        auth = ManagedAccess(root)
        invite = auth.invite(DEFAULT_PROJECT, "reviewer@example.org", "reviewer", "local", "owner")["url"]
        executive = auth.invite(DEFAULT_PROJECT, "executive@example.org", "executive", "local", "owner")["url"]
        http.RequestHandlerClass = penstock.make_handler(workspace, key, auth)
        start(stack, http)
        payload = {"disposable": True, "clips": args.clips, "origin": origin, "project": DEFAULT_PROJECT, "ownerKey": key,
                   "invite": invite, "executive": executive, "job": model.job, "sandbox": (SOURCE / "design-sandbox/index.html").as_uri(),
                   "screens": str(args.screens.resolve()) if args.screens else None}
        result = subprocess.run(["node", str(SOURCE / "tests/guided-browser.cjs")],
                                input=json.dumps(payload), text=True, capture_output=True, timeout=120)
        if result.returncode:
            raise SystemExit(result.stderr)
        report = json.loads(result.stdout)
        if model.lookup_calls:
            raise AssertionError("Editing a lookup contacted its configured adapter.")
        if model.calls != 1:
            raise AssertionError("The fixture model must receive exactly one approved drafting request.")
        report["approvedFixtureModelRequests"] = model.calls
        if args.screens:
            (args.screens / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))


if __name__ == "__main__":
    main()
