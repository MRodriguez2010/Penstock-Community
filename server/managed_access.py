"""Opt-in project identities, invitations and revocable sessions."""
import contextlib
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit


ROLES = {
    "viewer": {"view"},
    "reviewer": {"view", "review"},
    "editor": {"view", "edit", "test"},
    "executive": {"view", "edit", "test", "review"},
    "operator": {"view", "review", "edit", "test", "publish", "live"},
    "admin": {"view", "review", "edit", "test", "publish", "live", "admin"},
}
OWNER = {"id": "workspace-owner", "displayName": "Workspace owner", "method": "owner-key", "role": "owner"}
DESKTOP_OWNER = {**OWNER, "method": "desktop"}
OWNER_CAPABILITIES = set().union(*ROLES.values(), {"owner"})
SESSION_TTL = 8 * 3600
INVITE_TTL = 48 * 3600
OIDC_TTL = 10 * 60
TOKEN = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")
EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z", re.ASCII)
ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ManagedConfigurationError(ValueError):
    """The workspace can serve, but managed sign-in cannot start."""


def fingerprint(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def token():
    return secrets.token_urlsafe(32)


def valid_token(value):
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None


def normalize_email(value):
    if not isinstance(value, str) or len(value) > 254 or not value.isascii() or not EMAIL.fullmatch(value.strip()):
        raise ValueError("Enter a valid email address.")
    return value.strip().lower()


def password_bytes(value, *, new=False):
    if not isinstance(value, str) or len(value) > 256 or not value or (new and len(value) < 15):
        raise ValueError("Enter a password between 15 and 256 characters.")
    return value.encode("utf-8")


def derive_password(password, salt):
    return hashlib.scrypt(password, salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)


def public_origin(value):
    if not isinstance(value, str) or not value.isascii() or any(c.isspace() or c == "\\" for c in value):
        raise ValueError("Specify a valid public origin.")
    p = urlsplit(value)
    if not p.hostname or p.username or p.password or p.path not in ("", "/") or p.query or p.fragment:
        raise ValueError("Specify a full public origin without a path, query or credentials.")
    if p.scheme != "https" and not (p.scheme == "http" and p.hostname in {"127.0.0.1", "::1", "localhost"}):
        raise ValueError("Managed access requires HTTPS except on loopback for development.")
    if p.port is not None and not 0 < p.port <= 65535:
        raise ValueError("Use a valid port in the public origin.")
    host = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
    default_port = 443 if p.scheme == "https" else 80
    return f"{p.scheme}://{host}" + (f":{p.port}" if p.port not in (None, default_port) else "")


def configure(root, origin, issuer=None, client_id=None, key_env=None, *, check_runtime=False):
    origin = public_origin(origin)
    config = {"origin": origin}
    if issuer or client_id or key_env:
        from managed_oidc import issuer_url
        if not issuer or not client_id or len(client_id) > 512 or (key_env and not ENV.fullmatch(key_env)):
            raise ValueError("OIDC requires an issuer, a client ID and an optional client-secret environment variable name.")
        config["oidc"] = {"issuer": issuer_url(issuer), "client_id": client_id, "secret_env": key_env}
        if check_runtime:
            from managed_oidc import OIDC
            OIDC(origin, **config["oidc"])
    path = Path(root) / "managed.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    return config


class ManagedAccess:
    def __init__(self, root):
        root = Path(root)
        config = json.loads((root / "managed.json").read_text("utf-8"))
        self.origin = public_origin(config["origin"])
        self.oidc = None
        if "oidc" in config:
            from managed_oidc import OIDC
            try:
                self.oidc = OIDC(self.origin, **config["oidc"])
            except ValueError as exc:
                raise ManagedConfigurationError(str(exc)) from exc
        self.path = root / "managed.sqlite3"
        self.attempts = deque()
        self.attempt_lock = threading.Lock()
        self.password_slots = threading.BoundedSemaphore(2)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS identities (
                    id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, method TEXT NOT NULL,
                    issuer TEXT, subject TEXT, salt BLOB, password BLOB, UNIQUE(issuer, subject));
                CREATE TABLE IF NOT EXISTS memberships (
                    project TEXT NOT NULL, identity TEXT NOT NULL REFERENCES identities(id), role TEXT NOT NULL,
                    PRIMARY KEY(project, identity));
                CREATE TABLE IF NOT EXISTS invites (
                    hash TEXT PRIMARY KEY, project TEXT NOT NULL, email TEXT NOT NULL,
                    role TEXT NOT NULL, method TEXT NOT NULL, subject TEXT, expires REAL NOT NULL,
                    UNIQUE(project, email));
                CREATE UNIQUE INDEX IF NOT EXISTS invites_subject ON invites(project,subject) WHERE subject IS NOT NULL;
                CREATE TABLE IF NOT EXISTS sessions (
                    hash TEXT PRIMARY KEY, identity TEXT NOT NULL REFERENCES identities(id), csrf TEXT NOT NULL,
                    expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS oidc_attempts (
                    state TEXT PRIMARY KEY, browser TEXT NOT NULL, nonce TEXT NOT NULL,
                    verifier TEXT NOT NULL, invite TEXT, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS membership_audit (
                    at REAL NOT NULL, project TEXT NOT NULL, subject TEXT NOT NULL, operation TEXT NOT NULL,
                    role TEXT, actor TEXT NOT NULL);
            """)

    @contextlib.contextmanager
    def db(self):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db

    def rate_limit(self, address, identifier):
        now = time.monotonic()
        with self.attempt_lock:
            while self.attempts and self.attempts[0][0] < now - 60:
                self.attempts.popleft()
            addresses = sum(1 for _, ip, _ in self.attempts if address == ip)
            matches = sum(1 for _, ip, key in self.attempts if address == ip and key == identifier)
            if len(self.attempts) >= 500 or addresses >= 30 or matches >= 8:
                raise PermissionError("Too many sign-in attempts. Wait one minute and try again.")
            self.attempts.append((now, address, identifier))

    def hash_password(self, password, salt):
        if not self.password_slots.acquire(blocking=False):
            raise PermissionError("Sign-in is busy. Try again shortly.")
        try:
            return derive_password(password, salt)
        finally:
            self.password_slots.release()

    def session(self, raw):
        if not valid_token(raw):
            return None
        with self.db() as db:
            row = db.execute("""SELECT i.id, i.email, i.method, s.csrf FROM sessions s JOIN identities i ON i.id=s.identity
                                WHERE s.hash=? AND s.expires>? AND EXISTS
                                (SELECT 1 FROM memberships WHERE identity=i.id)""", (fingerprint(raw), time.time())).fetchone()
        return dict(row) if row else None

    def create_session(self, db, identity, old=None):
        now = time.time()
        db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
        if valid_token(old):
            db.execute("DELETE FROM sessions WHERE hash=?", (fingerprint(old),))
        raw = token()
        db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (fingerprint(raw), identity, token(), now + SESSION_TTL))
        return raw

    def logout(self, raw):
        if valid_token(raw):
            with self.db() as db:
                db.execute("DELETE FROM sessions WHERE hash=?", (fingerprint(raw),))

    def role(self, identity, project):
        with self.db() as db:
            row = db.execute("SELECT role FROM memberships WHERE identity=? AND project=?", (identity, project)).fetchone()
        return row[0] if row else None

    def projects(self, identity):
        with self.db() as db:
            return {row[0] for row in db.execute("SELECT project FROM memberships WHERE identity=?", (identity,))}

    def members(self, project):
        with self.db() as db:
            rows = db.execute("""SELECT i.id,i.email,i.method,m.role FROM memberships m JOIN identities i ON i.id=m.identity
                               WHERE m.project=? ORDER BY lower(i.email)""", (project,)).fetchall()
            invites = db.execute("SELECT email,method,role,expires FROM invites WHERE project=? AND expires>? ORDER BY email",
                                 (project, time.time())).fetchall()
        return {"members": [dict(r) for r in rows], "invites": [dict(r) for r in invites]}

    @staticmethod
    def audit(db, project, subject, operation, actor, role=None):
        db.execute("INSERT INTO membership_audit VALUES(?,?,?,?,?,?)",
                   (time.time(), project, subject, operation, role, json.dumps(actor, ensure_ascii=True)))

    def invite(self, project, email, role, method, actor, subject=None):
        email = normalize_email(email)
        if role not in ROLES or method not in {"local", "oidc"} or method == "oidc" and not self.oidc:
            raise ValueError("Choose a supported role and sign-in method.")
        if subject is not None and (method != "oidc" or not isinstance(subject, str) or not 1 <= len(subject) <= 255):
            raise ValueError("Specify an OIDC subject of up to 255 characters.")
        raw, expires = token(), time.time() + INVITE_TTL
        with self.db() as db:
            db.execute("DELETE FROM invites WHERE expires<=?", (time.time(),))
            member = db.execute("""SELECT 1 FROM memberships m JOIN identities i ON m.identity=i.id
                                   WHERE m.project=? AND i.email=?""", (project, email)).fetchone()
            if member:
                raise ValueError("This person is already a project member. Change their role in the member list.")
            db.execute("""INSERT INTO invites VALUES(?,?,?,?,?,?,?) ON CONFLICT(project,email) DO UPDATE SET
                          hash=excluded.hash,role=excluded.role,method=excluded.method,subject=excluded.subject,expires=excluded.expires""",
                       (fingerprint(raw), project, email, role, method, subject, expires))
            self.audit(db, project, email, "invite", actor, role)
        return {"url": f"{self.origin}/#invite={raw}&method={method}", "expires": expires}

    def set_member(self, project, identity, role, actor):
        if role not in ROLES or not isinstance(identity, str):
            raise ValueError("Choose a project role.")
        with self.db() as db:
            if not db.execute("UPDATE memberships SET role=? WHERE project=? AND identity=?", (role, project, identity)).rowcount:
                raise ValueError("Unknown project member.")
            self.audit(db, project, identity, "change-role", actor, role)
        return {"updated": True}

    def revoke(self, project, actor, identity=None, email=None):
        with self.db() as db:
            if identity is not None:
                if not isinstance(identity, str):
                    raise ValueError("Choose a project member.")
                db.execute("DELETE FROM memberships WHERE identity=? AND project=?", (identity, project))
                db.execute("DELETE FROM sessions WHERE identity=? AND NOT EXISTS (SELECT 1 FROM memberships WHERE identity=?)",
                           (identity, identity))
                self.audit(db, project, identity, "revoke-member", actor)
            else:
                email = normalize_email(email)
                db.execute("DELETE FROM invites WHERE email=? AND project=?", (email, project))
                self.audit(db, project, email, "revoke-invite", actor)
        return {"revoked": True}

    def get_invite(self, db, invite_hash, method):
        invite = db.execute("SELECT * FROM invites WHERE hash=? AND method=? AND expires>?",
                            (invite_hash, method, time.time())).fetchone()
        if invite is None:
            raise PermissionError("The invitation is invalid or expired. Request a new link from a project administrator.")
        return invite

    def accept(self, db, invite, identity):
        db.execute("INSERT OR IGNORE INTO memberships VALUES(?,?,?)", (invite["project"], identity, invite["role"]))
        db.execute("DELETE FROM invites WHERE hash=?", (invite["hash"],))
        self.audit(db, invite["project"], identity, "accept", {"id": identity}, invite["role"])

    def accept_local(self, raw, password, old=None):
        if not valid_token(raw):
            raise PermissionError("Invalid invitation.")
        invite_hash = fingerprint(raw)
        with self.db() as db:
            invite = self.get_invite(db, invite_hash, "local")
            member = db.execute("SELECT * FROM identities WHERE email=?", (invite["email"],)).fetchone()
        if member is not None and member["method"] != "local":
            raise PermissionError("This invitation uses a different sign-in method from your account.")
        salt = member["salt"] if member else secrets.token_bytes(16)
        derived = self.hash_password(password_bytes(password, new=member is None), salt)
        if member and not hmac.compare_digest(member["password"], derived):
            raise PermissionError("Use your existing Penstock password to accept this invitation.")
        with self.db() as db:
            invite = self.get_invite(db, invite_hash, "local")
            if member is None:
                if db.execute("SELECT 1 FROM identities WHERE email=?", (invite["email"],)).fetchone():
                    raise PermissionError("Sign in with your existing password to accept this invitation.")
                identity = str(uuid.uuid4())
                db.execute("INSERT INTO identities VALUES(?,?,'local',NULL,NULL,?,?)", (identity, invite["email"], salt, derived))
            else:
                identity = member["id"]
            self.accept(db, invite, identity)
            return self.create_session(db, identity, old)

    def login(self, email, password, old=None):
        email = normalize_email(email)
        with self.db() as db:
            row = db.execute("""SELECT id,salt,password FROM identities i WHERE email=? AND method='local'
                                AND EXISTS (SELECT 1 FROM memberships WHERE identity=i.id)""", (email,)).fetchone()
        derived = self.hash_password(password_bytes(password), row["salt"] if row else b"\0" * 16)
        if not row or not hmac.compare_digest(row["password"], derived):
            raise PermissionError("Email or password is incorrect.")
        with self.db() as db:
            return self.create_session(db, row["id"], old)

    def begin_oidc(self, invite=None):
        invite_hash = fingerprint(invite) if valid_token(invite) else None
        if invite is not None and invite_hash is None:
            raise PermissionError("Invalid invitation.")
        now = time.time()
        state, browser, nonce, verifier = token(), token(), token(), secrets.token_urlsafe(64)
        with self.db() as db:
            if invite_hash:
                self.get_invite(db, invite_hash, "oidc")
            db.execute("DELETE FROM oidc_attempts WHERE expires<=?", (now,))
            db.execute("INSERT INTO oidc_attempts VALUES(?,?,?,?,?,?)",
                       (fingerprint(state), fingerprint(browser), nonce, verifier, invite_hash, now + OIDC_TTL))
        return self.oidc.authorization(state, nonce, verifier), browser

    def finish_oidc(self, query, browser, old=None):
        state, code = query.get("state", [None]), query.get("code", [None])
        if len(state) != 1 or len(code) != 1 or not valid_token(state[0]) or not valid_token(browser):
            raise PermissionError("Invalid sign-in response.")
        with self.db() as db:
            attempt = db.execute("""SELECT * FROM oidc_attempts WHERE state=? AND browser=? AND expires>?""",
                                (fingerprint(state[0]), fingerprint(browser), time.time())).fetchone()
            if not attempt:
                raise PermissionError("Sign-in expired. Start again from Penstock.")
            db.execute("DELETE FROM oidc_attempts WHERE state=?", (attempt["state"],))
        if "error" in query or not isinstance(code[0], str) or not 1 <= len(code[0]) <= 4096:
            raise PermissionError("The identity provider did not approve sign-in.")
        claims = self.oidc.exchange(code[0], attempt["nonce"], attempt["verifier"])
        issuer, subject = claims["iss"], claims["sub"]
        with self.db() as db:
            identity = db.execute("SELECT id,email FROM identities WHERE issuer=? AND subject=?", (issuer, subject)).fetchone()
            if attempt["invite"]:
                invite = self.get_invite(db, attempt["invite"], "oidc")
                if invite["subject"]:
                    if not hmac.compare_digest(invite["subject"].encode(), subject.encode()):
                        raise PermissionError("The identity provider account does not match the invitation.")
                elif claims.get("email_verified") is not True or normalize_email(claims.get("email")) != invite["email"]:
                    raise PermissionError("Sign in with the verified email address used for the invitation.")
                if identity and identity["email"] != invite["email"]:
                    raise PermissionError("This invitation does not match your existing Penstock account.")
                if not identity:
                    if db.execute("SELECT 1 FROM identities WHERE email=?", (invite["email"],)).fetchone():
                        raise PermissionError("This address already belongs to a different Penstock identity.")
                    identity_id = str(uuid.uuid4())
                    db.execute("INSERT INTO identities VALUES(?,?,'oidc',?,?,NULL,NULL)", (identity_id, invite["email"], issuer, subject))
                else:
                    identity_id = identity["id"]
                self.accept(db, invite, identity_id)
            elif identity and db.execute("SELECT 1 FROM memberships WHERE identity=?", (identity["id"],)).fetchone():
                identity_id = identity["id"]
            else:
                raise PermissionError("A project administrator must invite you before you can sign in.")
            return self.create_session(db, identity_id, old)
