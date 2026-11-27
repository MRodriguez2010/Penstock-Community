"""First-time owner setup for managed access on a self-hosted workspace."""
import json
import threading
from urllib.parse import urlsplit

from managed_access import configure, public_origin


configuration_lock = threading.Lock()


def authorize_browser(access, headers, *, desktop):
    access.owner_only()
    if desktop or access.identity["method"] != "owner-key":
        raise PermissionError("Team setup is available to the owner of a self-hosted workspace.")
    raw = headers.get("Origin")
    try:
        origin = public_origin(raw)
    except ValueError:
        raise PermissionError("Open Team setup over HTTPS or on a local loopback address.") from None
    if (raw != origin or headers.get("Host") != urlsplit(origin).netloc or
            headers.get("Sec-Fetch-Site") in {"same-site", "cross-site"}):
        raise PermissionError("Open Team setup from this Penstock workspace.")
    return origin


def describe(configuration, state):
    origin = configuration["origin"]
    oidc = bool(configuration.get("oidc"))
    return {"state": state, "origin": origin, "method": "oidc" if oidc else "local",
            "redirectUri": origin + "/api/auth/oidc/callback" if oidc else None}


def current(root, managed, browser_origin, activation_error):
    if managed:
        return describe({"origin": managed.origin, "oidc": managed.oidc}, "active")
    try:
        configuration = json.loads((root / "managed.json").read_text("utf-8"))
    except FileNotFoundError:
        return describe({"origin": browser_origin}, "unconfigured")
    result = describe(configuration, "restart-required")
    if activation_error:
        result["activationError"] = activation_error
    return result


def dispatch(access, root, data, headers, *, desktop, activation_error=None):
    origin = authorize_browser(access, headers, desktop=desktop)
    action = data.get("action")
    if action == "status":
        with configuration_lock:
            return current(root, access.managed, origin, activation_error)
    if action != "save":
        raise ValueError("Choose a team setup action.")
    if access.managed:
        return current(root, access.managed, origin, activation_error)
    method = data.get("method")
    if method not in ("local", "oidc"):
        raise ValueError("Choose local invitations or organization sign-in.")
    fields = {"origin": 1000}
    if method == "oidc":
        fields.update({"issuer": 2000, "clientId": 512, "keyEnv": 128})
    if set(data) - (set(fields) | {"action", "method"}):
        raise ValueError("Only the displayed Team setup fields can be saved.")
    settings = {}
    for name, limit in fields.items():
        value = data.get(name, "")
        if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 for char in value):
            raise ValueError("Enter valid values for Team setup.")
        settings[name] = value.strip()
    if method == "oidc" and not (settings["issuer"] and settings["clientId"]):
        raise ValueError("Enter the OIDC issuer and client ID.")
    with configuration_lock:
        try:
            configuration = configure(root, settings["origin"], settings.get("issuer"), settings.get("clientId"),
                                      settings.get("keyEnv") or None, check_runtime=True)
        except FileExistsError:
            return current(root, access.managed, origin, activation_error)
    return describe(configuration, "restart-required")
