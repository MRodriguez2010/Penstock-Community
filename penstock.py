#!/usr/bin/env python3
"""Launch the single-owner workspace with Python 3.11 or newer."""
import argparse
import contextlib
import hmac
import json
import mimetypes
import os
import secrets
import shutil
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "server"))
from connections import Connections
from workspace import Workspace, Conflict
from workflows import propose


def initialize(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / "skills").mkdir(exist_ok=True)
    for source, dest in (("connections.toml", "connections.toml"), ("skills/summarize.md", "skills/summarize.md")):
        try:
            with (root / dest).open("xb") as target, (ROOT / "examples" / source).open("rb") as example:
                shutil.copyfileobj(example, target)
        except FileExistsError:
            pass
    try:
        with (root / "owner.key").open("x", encoding="ascii") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
    except FileExistsError:
        pass


@contextlib.contextmanager
def single_writer(root):
    # OS releases the advisory lock on shutdown, including an unexpected exit.
    with (root / ".server.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            if not lock.tell():
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def make_handler(workspace, key):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Penstock"
        def log_message(self, *_):
            pass

        def send_bytes(self, status, content, mime):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(content)

        def send_json(self, status, value):
            self.send_bytes(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), "application/json")

        def authorized(self):
            header = self.headers.get("Authorization", "")
            if not header.isascii() or not hmac.compare_digest(header, "Bearer " + key):
                self.send_json(401, {"error":"Enter the workspace owner key."})
                return False
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/health":
                return self.send_json(200, {"status":"ok"})
            if path.startswith("/api/"):
                if not self.authorized():
                    return
                if path == "/api/workspace":
                    return self.send_json(200, workspace.catalog())
                return self.send_json(404, {"error":"Unknown endpoint."})
            if path == "/":
                file = ROOT / "src/index.html"
            elif path.startswith("/src/") and path[5:] in ASSETS:
                file = ROOT / path[1:]
            elif path == "/examples/review.json":
                file = ROOT / "examples/review.json"
            else:
                return self.send_json(404, {"error":"Unknown resource."})
            self.send_bytes(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] or "application/octet-stream")

        def do_POST(self):
            if not self.authorized():
                return
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                return self.send_json(403, {"error":"Use the same origin as your workspace."})
            try:
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 196608 or self.headers.get_content_type() != "application/json":
                    return self.send_json(400, {"error":"Send a JSON object of at most 192 KiB."})
                d = json.loads(self.rfile.read(size))
                if not isinstance(d, dict):
                    raise ValueError("Send a JSON object.")
                path = urlsplit(self.path).path
                if path == "/api/propose":
                    plan = propose(workspace.connections, d["provider"], d["brief"])
                    result = {"plan": plan}
                elif path == "/api/save":
                    result = workspace.save(d["plan"], d.get("workflow"), d.get("baseVersion"))
                elif path == "/api/version":
                    result = workspace.version(d["workflow"], d["version"])
                elif path == "/api/publish":
                    result = workspace.publish(d["workflow"], d["version"])
                elif path == "/api/start":
                    result = workspace.start(d["workflow"], d["version"], d["mode"], d["input"], d["request"])
                elif path == "/api/run":
                    result = workspace.run(d["id"])
                elif path == "/api/decide":
                    result = workspace.decide(d["id"], d["node"], d["approve"])
                else:
                    return self.send_json(404, {"error":"Unknown endpoint."})
                self.send_json(200, result)
            except Conflict as exc:
                self.send_json(409, {"error":str(exc)})
            except (KeyError, TypeError, ValueError, RecursionError) as exc:
                self.send_json(400, {"error":str(exc)})
    return Handler


ASSETS = {"index.html","app.js","style.css","canvasLayout.js","pipeRouting.js","workflowEvidence.js","penstock.svg"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init","check","serve","key"))
    parser.add_argument("--workspace", default=os.environ.get("PENSTOCK_WORKSPACE", ".workspace"))
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = Path(args.workspace).expanduser().resolve()
    os.umask(0o077)
    if args.command == "init":
        initialize(root)
        print("Workspace ready at", root)
        return
    if not (root / "owner.key").is_file():
        parser.exit(1, "Initialize this workspace first with penstock.py init.\n")
    key = (root / "owner.key").read_text("ascii").strip()
    if len(key) < 32:
        parser.exit(1, "The workspace owner.key must contain at least 32 characters.\n")
    if args.command == "key":
        print(key)
        return
    connections = Connections(root)
    if args.command == "check":
        print(json.dumps(connections.catalog(), indent=2))
        return
    with single_writer(root):
        workspace = Workspace(root, connections)
        if not workspace.catalog()["versions"]:
            workspace.save(json.loads((ROOT / "examples/review.json").read_text("utf-8")))
        with ThreadingHTTPServer((args.bind, args.port), make_handler(workspace, key)) as http:
            print(f"Penstock listening on {args.bind}:{http.server_port}", flush=True)
            try:
                http.serve_forever()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))
