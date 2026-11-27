"""HTTP policy for optional managed sessions and a one-time desktop handoff."""
import hmac
import threading
import time
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

from managed_access import DESKTOP_OWNER, OIDC_TTL, OWNER, OWNER_CAPABILITIES, ROLES, SESSION_TTL, token, valid_token


SESSION = "penstock_session"
SIGNIN = "penstock_oidc"


def cookie(name, value, *, age, secure=True, same_site="Lax"):
    return ("Set-Cookie", f"{name}={value}; Path=/; Max-Age={age}; HttpOnly; SameSite={same_site}" + ("; Secure" if secure else ""))


def get_cookie(headers, name):
    try:
        jar = SimpleCookie(headers.get("Cookie", ""))
        return jar[name].value if name in jar else None
    except CookieError:
        return None


def session_cookie(managed, name=SESSION):
    return "__Host-" + name if managed.origin.startswith("https://") else name


class DesktopSession:
    def __init__(self, bootstrap):
        if not valid_token(bootstrap):
            raise ValueError("Desktop bootstrap must be a high-entropy URL-safe token.")
        self.bootstrap = bootstrap
        self.secret = None
        self.csrf = None
        self.cookie_id = token()[:16]
        self.session_expiry = 0
        self.lock = threading.Lock()
        self.expires = time.monotonic() + 120

    @staticmethod
    def is_local(handler):
        return (handler.client_address[0] == "127.0.0.1" and handler.server.server_address[0] == "127.0.0.1"
                and handler.headers.get("Host") == f"127.0.0.1:{handler.server.server_port}")

    def accepts(self, handler):
        origin = handler.headers.get("Origin")
        return (self.is_local(handler) and (origin is None or origin == f"http://127.0.0.1:{handler.server.server_port}")
                and handler.headers.get("Sec-Fetch-Site") not in {"same-site", "cross-site"})

    def name(self, handler):
        return f"penstock_desktop_{handler.server.server_port}_{self.cookie_id}"

    def exchange(self, handler, candidate):
        with self.lock:
            if (not self.is_local(handler) or not self.bootstrap or time.monotonic() > self.expires
                    or not valid_token(candidate) or not hmac.compare_digest(candidate, self.bootstrap)):
                return None
            self.bootstrap = None
            self.secret, self.csrf = token(), token()
            self.session_expiry = time.monotonic() + SESSION_TTL
            # Older WebKit versions reject Secure cookies over HTTP loopback. The desktop
            # launcher owns the exact listener and limits navigation to that origin.
            return cookie(self.name(handler), self.secret, age=SESSION_TTL, secure=False, same_site="Strict")

    def identity(self, handler):
        raw = get_cookie(handler.headers, self.name(handler))
        with self.lock:
            if (self.is_local(handler) and self.secret and time.monotonic() < self.session_expiry and
                    valid_token(raw) and hmac.compare_digest(raw, self.secret)):
                return {**DESKTOP_OWNER, "csrf": self.csrf}
        return None

    def logout(self):
        with self.lock:
            self.secret = self.csrf = None


class Access:
    def __init__(self, workspace, managed, identity):
        self.workspace = workspace
        self.managed = managed
        self.identity = identity
        self.owner = identity["id"] == OWNER["id"]

    def project(self, project, capability="view"):
        if not isinstance(project, str) or len(project) > 80:
            raise ValueError("Choose a project.")
        role = "owner" if self.owner else self.managed.role(self.identity["id"], project)
        capabilities = OWNER_CAPABILITIES if self.owner else ROLES.get(role, set())
        if capability not in capabilities:
            raise PermissionError("Your role does not allow this action in this project.")
        self.workspace.require_project(project)
        return role, capabilities

    def actor(self, project):
        role, _ = self.project(project)
        return {"id": self.identity["id"], "displayName": self.identity.get("email", self.identity.get("displayName")),
                "method": self.identity["method"], "role": role}

    def owner_only(self):
        if not self.owner:
            raise PermissionError("This action requires the workspace owner.")

    def resolve(self, query, identifier):
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 80:
            raise ValueError("Choose a workflow or run ID.")
        with self.workspace.db() as db:
            row = db.execute(query, (identifier,)).fetchone()
        if row is None:
            if self.owner:
                raise ValueError("Unknown workflow or run.")
            raise PermissionError("You do not have access to this workflow or run.")
        return row[0]

    def workflow_project(self, workflow):
        return self.resolve("SELECT project FROM workflow_projects WHERE workflow=?", workflow)

    def run_project(self, run):
        return self.resolve("SELECT project FROM run_details WHERE id=?", run)

    def catalog(self, project=None):
        if self.owner:
            result = self.workspace.catalog(project) if project else self.workspace.catalog()
        else:
            projects = self.managed.projects(self.identity["id"])
            choices = [p for p in self.workspace.projects() if p["id"] in projects]
            if not choices:
                raise PermissionError("A project administrator needs to grant you access.")
            project = project or choices[0]["id"]
            self.project(project)
            result = self.workspace.catalog(project)
            result["projects"] = choices
            for field in ("services", "serviceProtocols", "serviceCapabilities"):
                result[field] = [] if field == "services" else {}
        role, capabilities = self.project(result["project"])
        if self.managed or self.identity["method"] == "desktop":
            result["managed"] = {"role": role, "capabilities": sorted(capabilities)}
        return result


def identify(handler, key, managed, desktop, *, allow_cookie=True):
    header = handler.headers.get("Authorization")
    if header is not None:
        if header.isascii() and hmac.compare_digest(header, "Bearer " + key):
            return OWNER
        return None
    if allow_cookie:
        if desktop:
            identity = desktop.identity(handler)
            if identity:
                return identity
        if managed and handler.headers.get("Host") == urlsplit(managed.origin).netloc:
            return managed.session(get_cookie(handler.headers, session_cookie(managed)))
    return None


def check_origin(handler, managed=None, desktop=None, *, required=False):
    origin = handler.headers.get("Origin")
    host = handler.headers.get("Host")
    if desktop and desktop.is_local(handler):
        expected = f"http://{host}"
        valid = origin == expected
    elif managed:
        valid = origin == managed.origin and host == urlsplit(managed.origin).netloc
    else:
        valid = bool(origin and urlsplit(origin).netloc == host)
    if (required or origin is not None) and not valid:
        raise PermissionError("Use the configured Penstock origin.")


def authorize_post(handler, identity, managed, desktop):
    csrf = identity.get("csrf")
    check_origin(handler, managed, desktop, required=bool(csrf))
    if csrf:
        given = handler.headers.get("X-CSRF-Token")
        if not valid_token(given) or not hmac.compare_digest(given, csrf):
            raise PermissionError("Refresh Penstock and try again.")


def public_identity(identity):
    return {key: identity[key] for key in ("id", "email", "displayName", "method") if key in identity} if identity else None


def auth_status(identity, managed, desktop):
    return {"enabled": bool(managed or desktop), "invitations": bool(managed), "oidc": bool(managed and managed.oidc),
            "user": public_identity(identity), "csrf": identity.get("csrf") if identity else None}


def auth_get(handler, path, query, managed):
    if not managed or not managed.oidc or handler.headers.get("Host") != urlsplit(managed.origin).netloc:
        return handler.send_json(404, {"error": "OIDC sign-in is unavailable."})
    secure = managed.origin.startswith("https://")
    context_name = session_cookie(managed, SIGNIN)
    session_name = session_cookie(managed)
    if path == "/api/auth/oidc/start":
        if handler.headers.get("Sec-Fetch-Site") == "cross-site":
            raise PermissionError("Open OIDC sign-in from Penstock.")
        managed.rate_limit(handler.client_address[0], "oidc-start")
        url, raw = managed.begin_oidc()
        return handler.redirect(url, cookie(context_name, raw, age=OIDC_TTL, secure=secure))
    clear = cookie(context_name, "", age=0, secure=secure)
    try:
        raw = managed.finish_oidc(query, get_cookie(handler.headers, context_name), get_cookie(handler.headers, session_name))
    except (PermissionError, ValueError):
        return handler.redirect("/#sign-in-error", clear)
    return handler.redirect("/", clear, cookie(session_name, raw, age=SESSION_TTL, secure=secure))


def auth_post(handler, path, data, identity, managed, desktop):
    if path == "/api/auth/logout" and identity and identity.get("csrf"):
        authorize_post(handler, identity, managed, desktop)
        if identity["method"] == "desktop":
            desktop.logout()
            clear = cookie(desktop.name(handler), "", age=0, secure=False, same_site="Strict")
        else:
            name = session_cookie(managed)
            managed.logout(get_cookie(handler.headers, name))
            clear = cookie(name, "", age=0, secure=managed.origin.startswith("https://"))
        return handler.send_json(200, {"signedOut": True}, headers=(clear,))
    if not managed:
        return handler.send_json(404, {"error": "Managed access is unavailable."})
    check_origin(handler, managed, required=True)
    secure = managed.origin.startswith("https://")
    address = handler.client_address[0]
    name = session_cookie(managed)
    old = get_cookie(handler.headers, name)
    if path == "/api/auth/login":
        email = data["email"]
        if not isinstance(email, str) or len(email) > 254:
            raise ValueError("Enter a valid email address.")
        managed.rate_limit(address, email.strip().lower())
        raw = managed.login(email, data["password"], old)
    elif path == "/api/auth/accept":
        managed.rate_limit(address, "accept")
        raw = managed.accept_local(data["token"], data["password"], old)
    elif path == "/api/auth/oidc/start" and managed.oidc:
        managed.rate_limit(address, "oidc-start")
        url, raw = managed.begin_oidc(data.get("token"))
        return handler.send_json(200, {"url": url}, headers=(cookie(session_cookie(managed, SIGNIN), raw, age=OIDC_TTL, secure=secure),))
    else:
        return handler.send_json(404, {"error": "Unknown sign-in endpoint."})
    return handler.send_json(200, {"signedIn": True}, headers=(cookie(name, raw, age=SESSION_TTL, secure=secure),))


def manage(path, data, access):
    if not access.managed:
        raise PermissionError("Managed access is unavailable.")
    project = data["project"]
    access.project(project, "admin")
    if path == "/api/managed/members":
        return {**access.managed.members(project), "oidc": bool(access.managed.oidc)}
    actor = access.actor(project)
    if path == "/api/managed/invite":
        return access.managed.invite(project, data["email"], data["role"], data["method"], actor, data.get("subject"))
    if path == "/api/managed/member":
        return access.managed.set_member(project, data["id"], data["role"], actor)
    if path == "/api/managed/revoke":
        if ("id" in data) == ("email" in data):
            raise ValueError("Choose one member or invitation to revoke.")
        return access.managed.revoke(project, actor, data.get("id"), data.get("email"))
    raise ValueError("Unknown membership endpoint.")
