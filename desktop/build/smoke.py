"""Qualify the source, staged, or installed desktop runtime on loopback."""
import argparse
from contextlib import closing, contextmanager
from hashlib import sha256
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from queue import Empty, Queue
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from threading import Thread
from urllib import error, request
from uuid import uuid4


DESKTOP = Path(__file__).resolve().parents[1]
STAGE = DESKTOP / "src-tauri/stage"
SUFFIX = ".exe" if os.name == "nt" else ""


def read_ready(child):
    lines = Queue()
    reader = Thread(target=lambda: lines.put(child.stdout.readline()), daemon=True)
    reader.start()
    try:
        line = lines.get(timeout=25)
    except Empty as exc:
        raise AssertionError("The backend did not report its loopback port.") from exc
    assert line.startswith("Penstock listening on 127.0.0.1:"), line
    port = int(line.rsplit(":", 1)[1])
    assert 0 < port < 65536
    return f"http://127.0.0.1:{port}"


def expect_status(opener, url, status):
    try:
        with opener.open(url, timeout=5) as response:
            received = response.status
    except error.HTTPError as exc:
        try:
            received = exc.code
        finally:
            exc.close()
    assert received == status, (url.rsplit("/", 1)[0], received, status)


def process_env(source, node):
    env = {key: value for key, value in os.environ.items() if not key.startswith("PENSTOCK_")}
    env["PATH"] = os.pathsep.join((str(node.parent), env.get("PATH", "")))
    env.pop("PYTHONPATH", None)
    if source:
        env["PYTHONPATH"] = os.pathsep.join((str(DESKTOP.parent), str(DESKTOP)))
    return env


@contextmanager
def contact_probe():
    """Detect surprise requests to the registered provider, service, or standard proxy."""
    touched = []

    class Probe(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reject(self):
            touched.append(self.command)
            self.send_error(418)

        do_GET = do_POST = do_CONNECT = reject

    with ThreadingHTTPServer(("127.0.0.1", 0), Probe) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", touched
        finally:
            server.shutdown()
            thread.join()


class Studio:
    def __init__(self, origin, bootstrap):
        self.url = origin
        anonymous = request.build_opener(request.ProxyHandler({}))
        expect_status(anonymous, origin + "/api/workspace", 401)
        cookies = CookieJar()
        self.opener = request.build_opener(request.ProxyHandler({}), request.HTTPCookieProcessor(cookies))
        with self.opener.open(origin + "/desktop/bootstrap/" + bootstrap, timeout=5) as response:
            assert response.geturl() == origin + "/"
            assert "Penstock" in response.read().decode()
        assert any(not cookie.domain_specified and cookie.has_nonstandard_attr("HttpOnly") and
                   cookie.get_nonstandard_attr("SameSite") == "Strict" for cookie in cookies)
        expect_status(anonymous, origin + "/desktop/bootstrap/" + bootstrap, 404)
        session = self.get("auth/status")
        assert session["user"]["method"] == "desktop"
        self.csrf = session["csrf"]
        self.project = self.get("workspace")["project"]

    def get(self, route):
        with self.opener.open(self.url + "/api/" + route, timeout=6) as response:
            return json.load(response)

    def post(self, route, **body):
        payload = json.dumps({"project": self.project, **body}).encode()
        req = request.Request(self.url + "/api/" + route, data=payload, headers={
            "Content-Type": "application/json", "Origin": self.url, "X-CSRF-Token": self.csrf,
        })
        with self.opener.open(req, timeout=12) as response:
            return json.load(response)

    def asset(self, filename):
        with self.opener.open(self.url + "/src/" + filename, timeout=5) as response:
            assert response.status == 200 and response.read(), filename


@contextmanager
def launch(backend, workspace, env):
    bootstrap = secrets.token_hex(32)
    with subprocess.Popen(
        [*backend, "serve", "--workspace", workspace, "--bind", "127.0.0.1", "--port", "0"],
        cwd=workspace.parent, env={**env, "PENSTOCK_DESKTOP_BOOTSTRAP": bootstrap},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    ) as child:
        try:
            yield Studio(read_ready(child), bootstrap)
        finally:
            child.stdin.close()
            try:
                code = child.wait(timeout=14)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
                raise AssertionError("The backend did not stop after launcher stdin closed.") from None
            assert code == 0, f"Desktop backend exited with status {code}."


def starter_approvals(studio):
    initial = studio.post("resources")
    starters = studio.post("starter-catalog")["starters"]
    kinds = {entry["kind"]: entry for entry in starters}
    assert set(kinds) == {"weekly-brief", "reviewed-intake"}
    approved = {}
    for kind, item in kinds.items():
        inputs = {"from": "2026-11-23", "through": "2026-11-29"} if kind == "weekly-brief" else item["sample"]
        command = {
            "kind": kind, "version": item["version"], "source": "sample",
            "input": inputs, "request": str(uuid4()),
        }
        pending = studio.post("starter-run", **command)
        assert pending["status"] == "awaiting-review"
        assert studio.post("starter-run", **command)["id"] == pending["id"]
        before = studio.post("resources")
        assert (before["files"], before["tables"]) == (initial["files"], initial["tables"])
        receipt = studio.post("starter-decide", id=pending["id"], approve=True)
        assert receipt["status"] == "approved" and receipt["effect"]["id"]
        retry = studio.post("starter-decide", id=pending["id"], approve=True)
        assert retry["effect"] == receipt["effect"] and retry["history"] == receipt["history"]
        assert studio.post("starter-read", id=receipt["id"]) == receipt
        approved[kind] = receipt
        initial = studio.post("resources")
    read_effects(studio, approved)
    return approved


def read_effects(studio, approved):
    brief = approved["weekly-brief"]
    impact = brief["effect"]
    assert impact["type"] == "file"
    assert brief["output"]["period"] == {"from": "2026-11-23", "through": "2026-11-29"}
    saved = studio.post("resource", action="file-read", data={"id": impact["id"]})
    assert saved["text"] == brief["output"]["markdown"] and saved["version"] == 1
    assert all(part in saved["text"] for part in ("## Decisions", "## Overdue items", "Source:"))
    intake = approved["reviewed-intake"]
    impact = intake["effect"]
    assert impact["type"] == "table"
    rows = studio.post("resource", action="table-read", data={"id": impact["id"]})
    assert rows["total"] == 1
    assert dict(zip(rows["table"]["columns"], rows["rows"][0])) == intake["output"]["row"]
    assert rows["records"][0]["id"] == impact["rowId"]
    inventory = studio.post("resources")
    assert len(inventory["files"]) == len(inventory["tables"]) == 1
    notifications = studio.post("ops-notifications")
    assert len(notifications["items"]) == 4
    assert not notifications["deliveries"]


def seed_legacy_workspace(workspace, probe):
    """Seed the original workflow tables before project and lifecycle metadata existed."""
    config = workspace / "connections.toml"
    with config.open("a", encoding="utf-8") as handle:
        handle.write(f'''
# Owner-owned settings retained on upgrade.
[providers.release_offline_probe]
protocol = "chat-completions"
model = "never-call-this-provider"
url = "{probe}/model"

[services.release_offline_probe]
protocol = "http"
method = "POST"
url = "{probe}/service"
''')
    memo = workspace / "legacy-notes.txt"
    memo.write_bytes(b"Owner's pre-upgrade notes\n")
    skill = workspace / "skills" / "summarize.md"
    skill.write_text("Owner's saved instructions.\n", encoding="utf-8")

    workflow, run = str(uuid4()), str(uuid4())
    plan = {"name": "Owner's saved workflow", "steps": [
        {"id": "start", "type": "start", "next": "finish"}, {"id": "finish", "type": "finish"},
    ]}
    graph = {"nodes": [
        {"id": "start", "type": "start", "position": {"x": 0, "y": 0}, "config": {}},
        {"id": "finish", "type": "finish", "position": {"x": 270, "y": 0}, "config": {}},
    ], "edges": [{"id": "link-1", "source": "start", "target": "finish", "sourcePort": "success", "targetPort": "in"}]}
    with closing(sqlite3.connect(workspace / "workspace.sqlite3")) as db:
        db.executescript("""
            CREATE TABLE versions(
                workflow TEXT, version INTEGER, name TEXT, plan TEXT, graph TEXT, digest TEXT, binding TEXT,
                published INTEGER DEFAULT 0, PRIMARY KEY(workflow,version));
            CREATE TABLE runs(
                id TEXT PRIMARY KEY, workflow TEXT, version INTEGER, mode TEXT,
                request TEXT UNIQUE, input TEXT, state TEXT, cursor TEXT, context TEXT,
                history TEXT, created REAL, FOREIGN KEY(workflow,version) REFERENCES versions);
        """)
        digest = sha256(json.dumps(graph, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        binding = sha256(b"[]").hexdigest()
        db.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?,?)",
                   (workflow, 1, plan["name"], json.dumps(plan), json.dumps(graph), digest, binding, 1))
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            run, workflow, 1, "test", str(uuid4()), json.dumps("pre-upgrade input"), "finished", None,
            json.dumps({"result": "pre-upgrade result"}), json.dumps([]), 1,
        ))
        db.commit()
    return workflow, run, {path: path.read_bytes() for path in (config, memo, skill)}


def assert_preserved(studio, workflow, run):
    version = studio.post("version", workflow=workflow, version=1)
    saved = studio.post("run", id=run)
    assert version["name"] == "Owner's saved workflow" and version["published"]
    assert saved["context"]["result"] == "pre-upgrade result"
    assert saved["input"] == "pre-upgrade input" and saved["workflow"] == workflow and saved["version"] == 1


def qualify(backend, node, *, tier, source=False):
    node = Path(node).resolve(strict=True)
    backend = [str(Path(part).resolve(strict=True)) for part in backend]
    env = process_env(source, node)
    with tempfile.TemporaryDirectory(prefix="penstock-desktop-smoke-") as temporary, contact_probe() as (probe, touched):
        scratch = Path(temporary)
        expect_status(request.build_opener(request.ProxyHandler({})), probe, 418)
        assert touched == ["GET"], "The registered loopback probe did not observe its self-check."
        touched.clear()
        for scheme in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env[scheme] = env[scheme.lower()] = probe
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
        status = subprocess.check_output([
            *backend, "desktop-models", "status", "--cache", scratch / "unallocated-models",
        ], cwd=scratch, env=env, timeout=20)
        packs = json.loads(status)["packs"]
        assert {pack["id"] for pack in packs} == {"smollm3-3b", "olmo-3-7b-think"}
        assert all(not pack["present"] for pack in packs)
        subprocess.check_call([node, "--version"], cwd=scratch, env=env, timeout=10)

        def initialize(workspace):
            subprocess.check_call([*backend, "init", "--workspace", workspace], cwd=scratch, env=env, timeout=20)

        fresh = scratch / "fresh-workspace"
        initialize(fresh)
        with launch(backend, fresh, env) as studio:
            assert not studio.get("workspace")["providers"]
            for name in ("app.js", "starterStudio.js", "studioFields.js", "guidedStudio.css", "workflowLifecycle.js"):
                studio.asset(name)
            calendar = studio.post("schedule-preview", config={
                "scheduleType": "daily", "dailyTime": "09:00", "timezone": "America/Chicago",
            })
            assert calendar["occurrences"]
            approvals = starter_approvals(studio)
        with launch(backend, fresh, env) as studio:
            for receipt in approvals.values():
                assert studio.post("starter-read", id=receipt["id"]) == receipt
            read_effects(studio, approvals)

        legacy = scratch / "legacy-workspace"
        initialize(legacy)
        workflow, old_run, fixture = seed_legacy_workspace(legacy, probe)
        fixture[legacy / "owner.key"] = (legacy / "owner.key").read_bytes()
        initialize(legacy)
        for path, content in fixture.items():
            assert path.read_bytes() == content
        with launch(backend, legacy, env) as studio:
            assert "release_offline_probe" in studio.get("workspace")["providers"]
            assert_preserved(studio, workflow, old_run)
            approvals = starter_approvals(studio)
        with launch(backend, legacy, env) as studio:
            assert_preserved(studio, workflow, old_run)
            read_effects(studio, approvals)
        for path, content in fixture.items():
            assert path.read_bytes() == content
        assert not touched, "The offline flows unexpectedly contacted the registered provider, service, or proxy."
    print(f"{tier}: both starter approvals persisted, retries created no duplicate effects, "
          "the old workspace survived, and the registered network probe received no calls.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="store_true")
    args = parser.parse_args()
    if args.source:
        node = shutil.which("node")
        if not node:
            parser.error("A system Node.js installation is required for the source smoke check.")
        qualify([sys.executable, DESKTOP / "build/sidecar.py"], node, tier="Source", source=True)
    else:
        qualify([STAGE / f"backend/penstock-backend{SUFFIX}"], STAGE / f"node/node{SUFFIX}", tier="Staged package")


if __name__ == "__main__":
    main()
