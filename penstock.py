#!/usr/bin/env python3
"""Launch Penstock Community Edition with Python 3.11 or newer."""
import argparse
import contextlib
import json
import mimetypes
import os
import secrets
import shutil
import sys
import threading
import time
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "server"))
from connections import Connections
from workspace import Workspace, Conflict
from workflow_lifecycle import ENDPOINTS as LIFECYCLE_ENDPOINTS, LifecycleConflict, dispatch as dispatch_lifecycle
from workflows import propose
from resources import DEFAULT_PROJECT
from managed_access import ManagedAccess, ManagedConfigurationError, configure as configure_managed
from managed_http import (Access, DesktopSession, auth_get, auth_post, auth_status, authorize_post,
                          check_origin, cookie, get_cookie, identify, manage)
import team_setup
import office_oauth
import desktop_brief
from ops_events import NOTIFIERS
import project_resources
from triggers import occurrences
from operations import OPS_ENDPOINTS, dispatch as dispatch_ops, serve as serve_ops
from starter_api import STARTER_ENDPOINTS, dispatch as dispatch_starter


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


class ProposalConnections:
    def __init__(self, connections):
        self.connections = connections
        self.providers = connections.providers
        self.skills = connections.skills
        self.services = {}

    def catalog(self):
        original = self.connections.catalog()
        return {**original, "services": [], "serviceProtocols": {}, "serviceCapabilities": {}}

    def complete(self, *args, **kwargs):
        return self.connections.complete(*args, **kwargs)


def make_handler(workspace, key, managed=None, desktop=None, *, team_setup_error=None, downloads=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Penstock"
        def log_message(self, *_):
            pass

        def send_bytes(self, status, content, mime, headers=()):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(content)

        def send_json(self, status, value, headers=()):
            self.send_bytes(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), "application/json", headers)

        def redirect(self, target, *headers):
            self.send_bytes(303, b"", "text/plain", (("Location",target), *headers))

        def do_GET(self):
            if desktop and not desktop.accepts(self):
                return self.send_json(404, {"error":"Desktop workspace is unavailable."})
            request_url = urlsplit(self.path)
            path = request_url.path
            if path == "/health":
                return self.send_json(200, {"status":"ok"})
            if path.startswith("/desktop/bootstrap/"):
                header = desktop.exchange(self, path.removeprefix("/desktop/bootstrap/")) if desktop and not request_url.query else None
                return self.redirect("/", header) if header else self.send_json(404, {"error":"Desktop bootstrap is unavailable."})
            if path.startswith("/api/office-oauth/callback/") and not desktop:
                try:
                    ticket = office_oauth.callback(workspace, path.removeprefix("/api/office-oauth/callback/"),
                                                    parse_qs(request_url.query, max_num_fields=12),
                                                    get_cookie(self.headers, office_oauth.COOKIE), self.headers.get("Host"))
                except (PermissionError, ValueError):
                    return self.redirect("/#office-oauth-error")
                return self.redirect("/#office-oauth=" + ticket)
            if path.startswith("/api/"):
                try:
                    identity = identify(self, key, managed, desktop)
                    if path == "/api/auth/status":
                        return self.send_json(200, auth_status(identity, managed, desktop))
                    if path in ("/api/auth/oidc/start", "/api/auth/oidc/callback"):
                        return auth_get(self, path, parse_qs(request_url.query, max_num_fields=12), managed)
                    if not identity:
                        return self.send_json(401, {"error":"Sign in or enter the workspace owner key."})
                    if path == "/api/workspace":
                        return self.send_json(200, Access(workspace, managed, identity).catalog())
                    return self.send_json(404, {"error":"Unknown endpoint."})
                except PermissionError as exc:
                    return self.send_json(403, {"error":str(exc)})
                except (KeyError, TypeError, ValueError) as exc:
                    return self.send_json(400, {"error":str(exc)})
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
            if desktop and not desktop.accepts(self):
                return self.send_json(403, {"error":"Use the Penstock desktop origin."})
            path = urlsplit(self.path).path
            hook = path.startswith("/hooks/")
            sign_in = path.startswith("/api/auth/")
            try:
                identity = None if hook else identify(self, key, managed, desktop)
                if hook:
                    check_origin(self, managed, desktop)
                elif not sign_in:
                    if not identity:
                        return self.send_json(401, {"error":"Sign in or enter the workspace owner key."})
                    authorize_post(self, identity, managed, desktop)
                    access = Access(workspace, managed, identity)
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= (1100000 if path=="/api/resource" else 196608) or self.headers.get_content_type() != "application/json":
                    return self.send_json(400, {"error":"Send a JSON object within the request size limit."})
                d = json.loads(self.rfile.read(size))
                if not isinstance(d, dict):
                    raise ValueError("Send a JSON object.")
                if sign_in:
                    return auth_post(self, path, d, identity, managed, desktop)
                endpoint = path.removeprefix("/api/")
                if hook:
                    parts = path.strip("/").split("/")
                    if len(parts)!=3:
                        raise ValueError("Invalid webhook path.")
                    project = workspace.require_project(parts[1])
                    auth = self.headers.get("Authorization","")
                    if not auth.startswith("Bearer "):
                        raise PermissionError("Invalid webhook credentials.")
                    result = workspace.triggers.webhook(project,parts[2],auth[7:],self.headers.get("Idempotency-Key"),d)
                elif path.startswith("/api/managed/"):
                    result = manage(path, d, access)
                elif path == "/api/team-setup":
                    result = team_setup.dispatch(access, workspace.file.parent, d, self.headers, desktop=desktop,
                                                 activation_error=team_setup_error)
                elif path == "/api/desktop/save-brief-copy":
                    if not desktop or identity["method"] != "desktop" or downloads is None:
                        return self.send_json(404, {"error":"Saving to Downloads is available in the desktop app."})
                    if set(d) != {"project", "run"}:
                        raise ValueError("Choose a saved executive brief in this project.")
                    access.project(d["project"])
                    result = desktop_brief.save_copy(workspace, d["project"], d["run"], downloads)
                elif endpoint in STARTER_ENDPOINTS:
                    project = d["project"]
                    _, capabilities = access.project(project)
                    result = dispatch_starter(workspace, endpoint, d, can_edit="edit" in capabilities,
                                              can_run="test" in capabilities, can_review="review" in capabilities,
                                              actor=access.actor(project))
                elif endpoint in LIFECYCLE_ENDPOINTS:
                    required = "view" if endpoint == "workflow-lifecycle-state" else (
                        "publish" if endpoint == "workflow-lifecycle-rollback" and not d.get("schedules") else "admin")
                    access.project(d["project"], required)
                    result = dispatch_lifecycle(workspace,endpoint,d,actor=access.identity["id"])
                elif endpoint in OPS_ENDPOINTS:
                    if endpoint == "ops-oauth-finish":
                        d = office_oauth.context(workspace, d.get("ticket"), access.identity["id"],
                                                 get_cookie(self.headers, office_oauth.COOKIE), self.headers.get("Host"))
                    if endpoint == "ops-oauth-start" and (desktop or d.get("consent") is not True):
                        raise PermissionError("Confirm the project’s delegated access from its HTTPS workspace.")
                    project = d["project"]
                    required = {"ops-source-draft":"edit", "ops-test-change":"test", "ops-oauth-start":"admin", "ops-oauth-finish":"admin"}.get(endpoint,"view")
                    _, capabilities = access.project(project, required)
                    if endpoint in ("ops-profile-add", "ops-skill-add") or endpoint == "ops-configure" and d.get("kind") in NOTIFIERS:
                        access.owner_only()
                    result = dispatch_ops(workspace, endpoint, d, admin="admin" in capabilities,
                                          actor=access.actor(project)["id"], owner=access.owner)
                    if endpoint == "ops-oauth-start":
                        if self.headers.get("Origin") != result["_origin"] or self.headers.get("Host") != urlsplit(result["_origin"]).netloc:
                            raise PermissionError("Open this connection from its registered HTTPS Penstock origin.")
                        return self.send_json(200, {"url":result["url"]}, headers=(
                            cookie(office_oauth.COOKIE, result["_browser"], age=office_oauth.FLOW_TTL),))
                    if endpoint == "ops-oauth-finish":
                        result = {**result, "project":project, "kind":d["kind"]}
                elif path == "/api/project-create":
                    access.owner_only()
                    result = workspace.create_project(d["name"])
                elif path == "/api/project":
                    result = access.catalog(d["project"])
                elif path in ("/api/resources","/api/resource","/api/triggers","/api/trigger-create","/api/trigger-enable"):
                    project = d.get("project",DEFAULT_PROJECT)
                    access.project(project)
                    if path == "/api/resources":
                        result = workspace.data.describe(project)
                    elif path == "/api/resource":
                        if d["action"] not in ("table-read", "table-export", "file-read", "document-read"):
                            access.project(project, "edit")
                        result = workspace.data.action(project,d["action"],d.get("data",{}))
                    elif path == "/api/triggers":
                        result = workspace.triggers.list(project)
                    elif path == "/api/trigger-create":
                        access.owner_only()
                        result = workspace.triggers.create(project,d["workflow"],d["version"],d["kind"],d["config"],d.get("input"))
                    else:
                        access.owner_only()
                        result = workspace.triggers.enable(project,d["id"],d["enabled"])
                elif path == "/api/schedule-preview":
                    if not access.owner:
                        access.project(d["project"])
                    result = {"occurrences":occurrences(d["config"],time.time())}
                elif path == "/api/drafting-providers":
                    access.project(d["project"], "edit")
                    from guided_diagnosis import provider_disclosures
                    result = {"providers":provider_disclosures(workspace.connections)}
                elif path == "/api/diagnose":
                    project = d["project"]
                    _, capabilities = access.project(project)
                    if access.workflow_project(d["workflow"]) != project:
                        raise PermissionError("Choose a workflow from this project.")
                    if type(d["version"]) is not int:
                        raise ValueError("Choose an exact workflow version.")
                    workspace.version(d["workflow"], d["version"])
                    if d.get("run") is not None:
                        if access.run_project(d["run"]) != project:
                            raise PermissionError("Choose a run from this project.")
                        run = workspace.run(d["run"])
                        if (run["workflow"],run["version"]) != (d["workflow"],d["version"]):
                            raise ValueError("Choose a run from this exact workflow version.")
                    if d.get("provider") is not None and ("edit" not in capabilities or d.get("modelConsent") is not True):
                        raise PermissionError("An editor must confirm before diagnosis contacts a model.")
                    from guided_diagnosis import diagnose
                    result = diagnose(workspace, project, d, can_edit="edit" in capabilities, connections=workspace.connections)
                elif path == "/api/propose":
                    project = d.get("project",DEFAULT_PROJECT)
                    access.project(project, "edit")
                    if (not access.owner or "modelConsent" in d) and d.get("modelConsent") is not True:
                        raise PermissionError("Confirm sending your brief to the selected model provider.")
                    if d.get("providerBinding") is not None:
                        from guided_diagnosis import provider_disclosures
                        profile = provider_disclosures(workspace.connections).get(d["provider"])
                        if not profile or profile["binding"] != d["providerBinding"]:
                            raise PermissionError("The drafting model changed. Review its destination and confirm again.")
                    context = {}
                    if d.get("includeResources") is True:
                        with workspace.data.db(project) as c:
                            tables = [dict(r) for r in c.execute("""SELECT t.id,t.name,t.columns_json AS columns,s.revision
                                      FROM project_tables t JOIN typed_schemas s ON s.table_id=t.id ORDER BY t.rowid DESC LIMIT 60""")]
                        children = [v for v in workspace.catalog(project)["versions"] if v["published"]][:60]
                        context = {"tables":tables,"workflows":children}
                    connections = workspace.connections if access.owner else ProposalConnections(workspace.connections)
                    plan = propose(connections, d["provider"], d["brief"], context)
                    result = {"plan": plan}
                elif path == "/api/save":
                    project = access.workflow_project(d["workflow"]) if d.get("workflow") else d.get("project",DEFAULT_PROJECT)
                    access.project(project, "edit")
                    result = workspace.save(d["plan"], d.get("workflow"), d.get("baseVersion"),d.get("project"))
                elif path == "/api/version":
                    access.project(access.workflow_project(d["workflow"]))
                    result = workspace.version(d["workflow"], d["version"])
                elif path == "/api/publish":
                    access.project(access.workflow_project(d["workflow"]), "publish")
                    result = workspace.publish(d["workflow"], d["version"])
                elif path == "/api/start":
                    project = access.workflow_project(d["workflow"])
                    mode = d["mode"]
                    _, capabilities = access.project(project, "live" if mode == "live" else "test")
                    active_release = d.get("activeRelease",False)
                    if type(active_release) is not bool:
                        raise ValueError("Choose whether to run the active release.")
                    if mode == "live" and not access.owner:
                        active_release = True
                    consent_fields = {"modelConsent", "liveReadConsent", "liveWriteConsent"}
                    permissions = None
                    if not access.owner or consent_fields.intersection(d):
                        permissions = {"model": d.get("modelConsent") is True,
                                       "read": access.owner and d.get("liveReadConsent") is True,
                                       "write": access.owner and d.get("liveWriteConsent") is True,
                                       "projectRead": "live" in capabilities and d.get("liveReadConsent") is True}
                    result = workspace.start(d["workflow"],d["version"],mode,d["input"],d["request"],
                                             permissions=permissions,active_release=active_release)
                elif path == "/api/run":
                    access.project(access.run_project(d["id"]))
                    result = workspace.run(d["id"])
                elif path == "/api/receipt-replay":
                    access.project(d["project"], "edit")
                    from receipt_replay import replay_receipt
                    result = replay_receipt(workspace, d)
                elif path == "/api/decide":
                    project = access.run_project(d["id"])
                    access.project(project, "review")
                    if d["approve"] is True and not access.owner:
                        run = workspace.run(d["id"])
                        kind = next((n["type"] for n in run["graph"]["nodes"] if n["id"] == d["node"]), None)
                        if kind == "deployments" and run["mode"] == "live":
                            access.project(project, "publish")
                    result = workspace.decide(d["id"], d["node"], d["approve"],d.get("answer"), actor=access.actor(project))
                else:
                    return self.send_json(404, {"error":"Unknown endpoint."})
                self.send_json(200, result)
            except PermissionError as exc:
                self.send_json(403, {"error":str(exc)})
            except (Conflict,LifecycleConflict,project_resources.Conflict,sqlite3.IntegrityError) as exc:
                self.send_json(409, {"error":str(exc)})
            except (KeyError, TypeError, ValueError, RecursionError) as exc:
                self.send_json(400, {"error":str(exc)})
    return Handler


ASSETS = {"index.html","app.js","projects.js","style.css","canvasLayout.js","pipeRouting.js","workflowEvidence.js","receiptReplay.js",
          "penstock.svg","starterStudio.js","studioFields.js","guidedStudio.css","connectionCenter.js","connections.css",
          "managed-access.js","inboxNotice.js","officeSignIn.js","workflowLifecycle.js","teamSetup.js"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init","check","serve","key","managed-init"))
    parser.add_argument("--workspace", default=os.environ.get("PENSTOCK_WORKSPACE", ".workspace"))
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--public-origin")
    parser.add_argument("--oidc-issuer")
    parser.add_argument("--oidc-client-id")
    parser.add_argument("--oidc-client-secret-env")
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
    if args.command == "managed-init":
        if not args.public_origin:
            parser.error("managed-init requires --public-origin.")
        configuration = configure_managed(root, args.public_origin, args.oidc_issuer, args.oidc_client_id,
                                          args.oidc_client_secret_env)
        print("Managed access configured for", configuration["origin"])
        if configuration.get("oidc"):
            print("OIDC redirect URI:", configuration["origin"] + "/api/auth/oidc/callback")
        return
    bootstrap = os.environ.pop("PENSTOCK_DESKTOP_BOOTSTRAP", None)
    desktop = DesktopSession(bootstrap) if bootstrap else None
    download_folder = os.environ.pop("PENSTOCK_DESKTOP_DOWNLOADS", None)
    downloads = Path(download_folder) if desktop and download_folder else None
    if downloads is not None and not downloads.is_absolute():
        downloads = None
    if desktop and args.bind != "127.0.0.1":
        parser.error("Desktop mode requires an IPv4 loopback listener.")
    connections = Connections(root)
    desktop_providers = os.environ.pop("PENSTOCK_DESKTOP_PROVIDERS", None)
    if desktop and desktop_providers:
        connections.add_desktop_providers(desktop_providers)
    if args.command == "check":
        print(json.dumps(connections.catalog(), indent=2))
        return
    with single_writer(root):
        workspace = Workspace(root, connections)
        managed = team_setup_error = None
        if not desktop and (root / "managed.json").is_file():
            try:
                managed = ManagedAccess(root)
            except ManagedConfigurationError as exc:
                team_setup_error = str(exc)
                print(f"Managed sign-in could not start. {team_setup_error}", file=sys.stderr, flush=True)
        if not workspace.catalog()["versions"]:
            workspace.save(json.loads((ROOT / "examples/review.json").read_text("utf-8")))
        handler = make_handler(workspace, key, managed, desktop, team_setup_error=team_setup_error, downloads=downloads)
        with ThreadingHTTPServer((args.bind, args.port), handler) as http:
            print(f"Penstock listening on {args.bind}:{http.server_port}", flush=True)
            stop = threading.Event()
            workers = [threading.Thread(target=workspace.triggers.serve,args=(stop,),name="penstock-scheduler",daemon=True),
                       threading.Thread(target=serve_ops,args=(workspace,stop),name="penstock-notifications",daemon=True)]
            for worker in workers:
                worker.start()
            if desktop:
                def stop_at_pipe_end():
                    try:
                        while os.read(0, 4096):
                            pass
                    except OSError:
                        pass
                    http.shutdown()
                threading.Thread(target=stop_at_pipe_end, name="penstock-desktop-lifetime", daemon=True).start()
            try:
                http.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                stop.set()
                for worker in workers:
                    worker.join(timeout=5)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))
