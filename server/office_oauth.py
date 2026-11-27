"""Confidential-client OAuth for the four fixed project inbox/file readers."""
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from connections import NoRedirect
from managed_access import ENV, fingerprint, public_origin, token, valid_token
from managed_oidc import portable_hash


COOKIE = "__Host-penstock_office"
FLOW_TTL = 600
REFRESH_LEASE = 25
SCOPES = {
    "google-mail": "https://www.googleapis.com/auth/gmail.metadata",
    "google-drive": "https://www.googleapis.com/auth/drive.metadata.readonly",
    "microsoft-mail": "Mail.ReadBasic",
    "microsoft-files": "Files.Read",
}


@dataclass(frozen=True)
class Registration:
    kind: str
    origin: str
    client: str
    secret_env: str
    tenant: str | None

    @property
    def google(self):
        return self.kind.startswith("google-")

    @property
    def issuer(self):
        return "https://accounts.google.com" if self.google else f"https://login.microsoftonline.com/{self.tenant}/v2.0"

    @property
    def authorize_url(self):
        if self.google:
            return "https://accounts.google.com/o/oauth2/v2/auth"
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/authorize"

    @property
    def token_url(self):
        if self.google:
            return "https://oauth2.googleapis.com/token"
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token"

    @property
    def callback(self):
        return f"{self.origin}/api/office-oauth/callback/{self.kind}"

    @property
    def binding(self):
        fields = [self.kind, self.origin, self.client, self.secret_env, self.tenant, SCOPES[self.kind]]
        return hashlib.sha256(json.dumps(fields).encode()).hexdigest()

    def secret(self):
        value = os.environ.get(self.secret_env)
        if not value:
            raise ValueError("Ask IT to supply the OAuth client secret for this connection.")
        return value


def registration(kind, cfg):
    if kind not in SCOPES or not isinstance(cfg, dict):
        raise ValueError("Choose a supported Google or Microsoft reader.")
    origin_raw, client, secret = (cfg.get(k) for k in ("oauth_origin", "oauth_client_id", "oauth_client_secret_env"))
    if not isinstance(client, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,255}", client) or not isinstance(secret, str) or not ENV.fullmatch(secret):
        raise ValueError("Ask IT to configure this connector’s OAuth client.")
    origin = public_origin(origin_raw)
    if not origin.startswith("https://"):
        raise ValueError("Set the public HTTPS Penstock origin for OAuth.")
    tenant = None
    if kind.startswith("microsoft-"):
        try:
            tenant = str(uuid.UUID(cfg["oauth_tenant"]))
            client = str(uuid.UUID(client))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError("Register an Entra client and a specific organization tenant UUID.") from exc
    return Registration(kind, origin, client, secret, tenant)


def configured(kind, cfg):
    try:
        registration(kind, cfg)
        return True
    except (TypeError, ValueError):
        return False


def prepare(db):
    db.execute("""CREATE TABLE IF NOT EXISTS office_oauth_flows (
        state TEXT PRIMARY KEY, browser TEXT NOT NULL, project TEXT NOT NULL, kind TEXT NOT NULL,
        actor TEXT NOT NULL, origin TEXT NOT NULL, issuer TEXT NOT NULL, binding TEXT NOT NULL,
        verifier BLOB NOT NULL, expires REAL NOT NULL, ticket TEXT UNIQUE, code BLOB, error INTEGER, claim TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS office_oauth_tokens (
        project TEXT NOT NULL, kind TEXT NOT NULL, binding TEXT NOT NULL, generation TEXT NOT NULL,
        access BLOB NOT NULL, refresh BLOB NOT NULL, expires REAL NOT NULL, lease TEXT, lease_expires REAL,
        PRIMARY KEY(project,kind))""")


def cipher(workspace):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise ValueError("Install the dependencies in requirements-managed.txt to use office sign-in.") from exc
    path = Path(workspace.file).parent / ".office-oauth.key"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or (os.name != "nt" and path.stat().st_mode & 0o077):
            raise ValueError("Restrict access to the workspace office OAuth key to its service account.")
        key = path.read_bytes()
    else:
        key = AESGCM.generate_key(bit_length=256)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
    if len(key) != 32:
        raise ValueError("The workspace office OAuth key is invalid.")
    return AESGCM(key)


def seal_box(key, purpose, clear):
    iv = os.urandom(12)
    return iv + key.encrypt(iv, clear.encode(), purpose.encode())


def open_box(key, purpose, raw):
    from cryptography.exceptions import InvalidTag
    try:
        return key.decrypt(raw[:12], raw[12:], purpose.encode()).decode()
    except (InvalidTag, ValueError) as exc:
        raise ValueError("The saved office authorization cannot be opened. Ask IT to reconnect it.") from exc


def begin(workspace, project, kind, actor, cfg):
    workspace.require_project(project)
    app = registration(kind, cfg)
    app.secret()
    state, browser, verifier = token(), token(), token() + token()
    now = time.time()
    state_hash = fingerprint(state)
    with workspace.db() as db:
        prepare(db)
        db.execute("DELETE FROM office_oauth_flows WHERE expires<=? OR (project=? AND kind=? AND actor=?)",
                   (now, project, kind, actor))
        db.execute("INSERT INTO office_oauth_flows VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)",
                   (state_hash, fingerprint(browser), project, kind, actor, app.origin, app.issuer, app.binding,
                    seal_box(cipher(workspace), "verifier/" + state_hash, verifier), now + FLOW_TTL))
    fields = {"client_id": app.client, "redirect_uri": app.callback, "response_type": "code", "state": state,
              "scope": SCOPES[kind], "code_challenge": portable_hash(verifier), "code_challenge_method": "S256"}
    if app.google:
        fields.update(access_type="offline", prompt="consent", include_granted_scopes="false")
    else:
        fields.update(scope=SCOPES[kind] + " offline_access", response_mode="query", prompt="select_account")
    return {"url": app.authorize_url + "?" + urlencode(fields), "_browser": browser, "_origin": app.origin}


def callback(workspace, kind, query, browser, host):
    states, codes, issuers, errors = (query.get(field, []) for field in ("state", "code", "iss", "error"))
    if (kind not in SCOPES or not valid_token(browser) or len(states) != 1 or not valid_token(states[0]) or
            len(codes) + len(errors) != 1 or len(issuers) > 1 or codes and not 1 <= len(codes[0]) <= 4096):
        raise PermissionError("This sign-in cannot be resumed. Open Connections and try again.")
    state = fingerprint(states[0])
    ticket = token()
    with workspace.db() as db:
        prepare(db)
        flow = db.execute("""SELECT * FROM office_oauth_flows WHERE state=? AND browser=? AND kind=?
                             AND expires>? AND ticket IS NULL""", (state, fingerprint(browser), kind, time.time())).fetchone()
        if not flow or urlsplit(flow["origin"]).netloc != host or issuers and issuers[0] != flow["issuer"]:
            raise PermissionError("This sign-in cannot be resumed. Open Connections and try again.")
        code = seal_box(cipher(workspace), "code/" + state, codes[0]) if codes else None
        db.execute("UPDATE office_oauth_flows SET ticket=?,code=?,error=? WHERE state=?",
                   (fingerprint(ticket), code, bool(errors), state))
    return ticket


def find_flow(db, field, raw, actor, browser=None):
    if not valid_token(raw):
        raise ValueError("The connection sign-in expired. Start again from Connections.")
    if not valid_token(browser):
        raise PermissionError("Return to this browser’s original connection sign-in.")
    predicate = "ticket=?" if field == "ticket" else "state=?"
    flow = db.execute("SELECT * FROM office_oauth_flows WHERE " + predicate + " AND browser=? AND expires>? AND claim IS NULL",
                      (fingerprint(raw), fingerprint(browser), time.time())).fetchone()
    if not flow:
        raise ValueError("The connection sign-in expired. Start again from Connections.")
    if flow["actor"] != actor:
        raise PermissionError("Sign in as the Penstock user who started this connection.")
    return flow


def context(workspace, ticket, actor, browser, host):
    with workspace.db() as db:
        prepare(db)
        flow = find_flow(db, "ticket", ticket, actor, browser)
        if urlsplit(flow["origin"]).netloc != host:
            raise PermissionError("Use the configured Penstock origin.")
        # Internal assertion for finish. The HTTP adapter removes this from all client input.
        return {"project": flow["project"], "kind": flow["kind"], "state": ticket, "_browser": browser}


class TokenError(ValueError):
    def __init__(self, expired=False):
        super().__init__("Your delegated connection has expired. Reconnect it from Connections." if expired else
                         "The identity provider did not confirm authorization. Check the registered client and try again.")
        self.expired = expired


def token_request(app, payload):
    fields = {"client_id": app.client, "client_secret": app.secret(), **payload}
    req = Request(app.token_url, data=urlencode(fields).encode("ascii"),
                  headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=10) as response:
            raw = response.read(65537)
        result = json.loads(raw) if len(raw) <= 65536 else None
    except HTTPError as exc:
        with exc:
            try:
                code = json.loads(exc.read(8192)).get("error")
            except (ValueError, AttributeError):
                code = None
        raise TokenError(expired=code in {"invalid_grant", "interaction_required"}) from None
    except (OSError, ValueError):
        raise TokenError() from None
    if not isinstance(result, dict):
        raise TokenError()
    return result


def valid_secret(value):
    return isinstance(value, str) and 1 <= len(value) <= 16384 and all(32 < ord(c) < 127 for c in value)


def normalize_scopes(value, app):
    if app.google:
        return set(value.split())
    return {scope.lower().removeprefix("https://graph.microsoft.com/") for scope in value.split()}


def parse_grant(app, response, *, previous_refresh=None):
    access = response.get("access_token")
    refresh = response.get("refresh_token", previous_refresh)
    expires = response.get("expires_in")
    if not isinstance(response.get("token_type"), str) or response["token_type"].lower() != "bearer":
        raise TokenError()
    if not valid_secret(access) or not valid_secret(refresh) or type(expires) is not int or not 1 <= expires <= 86400:
        raise ValueError("The provider did not return a renewable read grant. Ask IT to check offline consent.")
    granted = response.get("scope")
    if granted is not None:
        if not isinstance(granted, str):
            raise TokenError()
        needed = normalize_scopes(SCOPES[app.kind], app)
        allowed = needed | (set() if app.google else {"offline_access", "openid", "profile", "email"})
        scopes = normalize_scopes(granted, app)
        if not needed <= scopes <= allowed:
            raise PermissionError("The provider returned a different scope than the approved project reader.")
    return access, refresh, time.time() + expires


def box_purpose(project, kind, binding, use):
    return f"tokens/{project}/{kind}/{binding}/{use}"


def finish(workspace, project, kind, actor, cfg, code, state, *, browser=None):
    app = registration(kind, cfg)
    claimed = token()
    with workspace.db() as db:
        prepare(db)
        flow = find_flow(db, "ticket" if code is None else "state", state, actor, browser)
        if (flow["project"], flow["kind"], flow["binding"]) != (project, kind, app.binding):
            raise PermissionError("This sign-in was created for a different project connection.")
        key = cipher(workspace)
        verifier = open_box(key, "verifier/" + flow["state"], flow["verifier"])
        error = flow["error"]
        if code is None and not error:
            code = open_box(key, "code/" + flow["state"], flow["code"])
        if not error and not valid_secret(code):
            raise ValueError("The provider returned an invalid authorization code.")
        if error:
            db.execute("DELETE FROM office_oauth_flows WHERE state=?", (flow["state"],))
        else:
            db.execute("UPDATE office_oauth_flows SET claim=? WHERE state=?", (claimed,flow["state"]))
    if error:
        raise ValueError("Organization sign-in was canceled or declined. No connection changed.")
    data = token_request(app, {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                               "redirect_uri": app.callback})
    access, refresh, expires = parse_grant(app, data)
    with workspace.db() as db:
        if not db.execute("DELETE FROM office_oauth_flows WHERE state=? AND claim=? AND expires>?",
                          (flow["state"],claimed,time.time())).rowcount:
            raise PermissionError("This project connection changed before sign-in could complete.")
        key = cipher(workspace)
        db.execute("""INSERT INTO office_oauth_tokens VALUES(?,?,?,?,?,?,?,NULL,NULL) ON CONFLICT(project,kind)
                      DO UPDATE SET binding=excluded.binding,generation=excluded.generation,access=excluded.access,
                      refresh=excluded.refresh,expires=excluded.expires,lease=NULL,lease_expires=NULL""",
                   (project, kind, app.binding, token(), seal_box(key, box_purpose(project,kind,app.binding,"access"),access),
                    seal_box(key, box_purpose(project,kind,app.binding,"refresh"),refresh), expires))
    return {"connected": True}


def status(workspace, project, kind, cfg=None):
    binding = registration(kind, cfg).binding if cfg is not None and configured(kind, cfg) else None
    with contextlib.closing(sqlite3.connect(workspace.file, timeout=5)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='office_oauth_tokens'").fetchone():
            return False
        row = db.execute("SELECT binding FROM office_oauth_tokens WHERE project=? AND kind=?", (project,kind)).fetchone()
        return bool(row and (cfg is None or row[0] == binding))


def revoke(workspace, project, kind):
    with workspace.db() as db:
        prepare(db)
        db.execute("DELETE FROM office_oauth_tokens WHERE project=? AND kind=?", (project,kind))
        db.execute("DELETE FROM office_oauth_flows WHERE project=? AND kind=?", (project,kind))
    return {"revoked": True}


def access_token(workspace, project, kind, cfg):
    if not configured(kind, cfg):
        return None
    app = registration(kind, cfg)
    now = time.time()
    lease = token()
    with workspace.db() as db:
        prepare(db)
        row = db.execute("SELECT * FROM office_oauth_tokens WHERE project=? AND kind=? AND binding=?",
                         (project,kind,app.binding)).fetchone()
        if not row:
            return None
        key = cipher(workspace)
        if row["expires"] > now + 60 or row["lease"] and row["expires"] > now + 5 and row["lease_expires"] > now:
            return open_box(key, box_purpose(project,kind,app.binding,"access"), row["access"])
        if row["lease"] and row["lease_expires"] > now:
            raise ValueError("This connection is refreshing. Try again shortly.")
        refresh = open_box(key, box_purpose(project,kind,app.binding,"refresh"), row["refresh"])
        identity = (project, kind, row["generation"], lease)
        db.execute("UPDATE office_oauth_tokens SET lease=?,lease_expires=? WHERE project=? AND kind=?",
                   (lease,now + REFRESH_LEASE,project,kind))
    try:
        data = token_request(app, {"grant_type":"refresh_token", "refresh_token":refresh})
        access, rotated, expires = parse_grant(app, data, previous_refresh=refresh)
    except Exception as exc:
        with workspace.db() as db:
            filter_sql = "WHERE project=? AND kind=? AND generation=? AND lease=?"
            if isinstance(exc, TokenError) and exc.expired:
                db.execute("DELETE FROM office_oauth_tokens " + filter_sql, identity)
            else:
                db.execute("UPDATE office_oauth_tokens SET lease=NULL,lease_expires=NULL " + filter_sql, identity)
        raise
    with workspace.db() as db:
        key = cipher(workspace)
        changed = db.execute("""UPDATE office_oauth_tokens SET access=?,refresh=?,expires=?,lease=NULL,lease_expires=NULL
                   WHERE project=? AND kind=? AND generation=? AND lease=?""",
              (seal_box(key,box_purpose(project,kind,app.binding,"access"),access),
               seal_box(key,box_purpose(project,kind,app.binding,"refresh"),rotated),expires,*identity)).rowcount
        if not changed:
            raise PermissionError("The project connection changed before this read could start.")
    return access
