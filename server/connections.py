"""Workspace-owned endpoints and instruction files. Workflow data can only select registered names."""
import hashlib
import json
import os
import re
import tomllib
from pathlib import Path
from urllib import error, request
from urllib.parse import urlsplit


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def post(url, payload, headers):
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    req = request.Request(url, data=body, headers={"Content-Type": "application/json", **headers})
    # Contact only the configured endpoint. Do not forward credentials through redirects.
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=90) as response:
            data = response.read(262145)
    except error.HTTPError as exc:
        raise ValueError(f"Endpoint returned HTTP {exc.code}. Check its URL, credentials and request format.") from None
    except (OSError, TimeoutError):
        raise ValueError("Endpoint did not confirm the request. Check its status before retrying.") from None
    if len(data) > 262144:
        raise ValueError("Endpoint response exceeded 256 KiB.")
    try:
        return json.loads(data) if data.strip() else None
    except (UnicodeError, ValueError):
        raise ValueError("Endpoint must return JSON or an empty response.") from None


class Connections:
    def __init__(self, workspace):
        self.root = Path(workspace)
        config = tomllib.loads((self.root / "connections.toml").read_text("utf-8"))
        if set(config) - {"providers", "services", "skills"}:
            raise ValueError("Unknown section in connections.toml.")
        self.providers = config.get("providers", {})
        self.services = config.get("services", {})
        self.skills = {}
        for entries in (self.providers, self.services, config.get("skills", {})):
            if not isinstance(entries, dict) or len(entries) > 128:
                raise ValueError("Use named entries in connections.toml.")
            for name in entries:
                if not re.fullmatch("[A-Za-z0-9_-]{1,64}", name):
                    raise ValueError("Use a short alphanumeric name for each connection or skill.")
        for name, entry in self.providers.items():
            self.check_endpoint(name, entry, {"protocol", "model"})
            if entry.get("protocol") not in ("chat-completions", "ollama", "anthropic-messages"):
                raise ValueError(f"Unknown provider protocol for {name}.")
            if not isinstance(entry.get("model"), str) or not entry["model"].strip():
                raise ValueError(f"Set the model ID for {name}.")
        for name, entry in self.services.items():
            self.check_endpoint(name, entry, set())
        for name, relative in config.get("skills", {}).items():
            if not isinstance(relative, str) or Path(relative).is_absolute():
                raise ValueError(f"Use a workspace-relative Markdown file for {name}.")
            file = (self.root / relative).resolve()
            if not file.is_relative_to(self.root.resolve()) or file.suffix.lower() != ".md" or file.stat().st_size > 16384:
                raise ValueError(f"Skill {name} must be a workspace Markdown file of at most 16 KiB.")
            self.skills[name] = file.read_text("utf-8")

    @staticmethod
    def check_endpoint(name, entry, fields):
        if not isinstance(entry, dict) or set(entry) - ({"url", "key_env"} | fields):
            raise ValueError(f"Invalid fields for {name}.")
        url = urlsplit(entry.get("url", ""))
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.fragment or url.query:
            raise ValueError(f"Set a full HTTP(S) endpoint without credentials, query or fragment for {name}.")
        if "key_env" in entry and not re.fullmatch("[A-Za-z_][A-Za-z0-9_]*", entry["key_env"]):
            raise ValueError(f"Set an environment variable name for {name}.")

    @staticmethod
    def secret(entry):
        name = entry.get("key_env")
        if name and not os.environ.get(name):
            raise ValueError(f"Set the environment variable {name} and restart Penstock.")
        return os.environ[name] if name else None

    def catalog(self):
        return {"providers": {name: {"protocol": e["protocol"], "model": e["model"]}
                              for name, e in self.providers.items()},
                "skills": sorted(self.skills), "services": sorted(self.services)}

    def binding(self, graph):
        """Fingerprint the exact selected destinations, model IDs and skill text, excluding secret values."""
        selected = []
        for node in graph["nodes"]:
            cfg = node["config"]
            if node["type"] == "agent":
                selected.append([self.providers.get(cfg["provider"]), self.skills.get(cfg.get("skill"))])
            elif node["type"] == "service":
                selected.append(self.services.get(cfg["service"]))
        encoded = json.dumps(selected, sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(encoded).hexdigest()

    def complete(self, name, system, prompt):
        entry = self.providers[name]
        protocol, model = entry["protocol"], entry["model"]
        key = self.secret(entry)
        headers = {"Authorization": "Bearer " + key} if key else {}
        messages = [{"role": "user", "content": prompt}]
        if protocol == "anthropic-messages":
            headers = {"anthropic-version": "2023-06-01"}
            if key:
                headers["x-api-key"] = key
            payload = {"model": model, "system": system, "max_tokens": 4096, "messages": messages}
        else:
            messages.insert(0, {"role": "system", "content": system})
            payload = {"model": model, "messages": messages, "stream": False}
        response = post(entry["url"], payload, headers)
        try:
            if protocol == "anthropic-messages":
                result = "\n".join(part["text"] for part in response["content"] if part["type"] == "text")
            elif protocol == "ollama":
                result = response["message"]["content"]
            else:
                result = response["choices"][0]["message"]["content"]
        except (TypeError, KeyError, IndexError):
            raise ValueError("Endpoint response did not match the configured model protocol.") from None
        if not isinstance(result, str) or not result.strip() or len(result.encode()) > 65536:
            raise ValueError("Expected a nonempty model answer of at most 64 KiB.")
        return result

    def deliver(self, name, value):
        entry = self.services[name]
        key = self.secret(entry)
        headers = {"Authorization": "Bearer " + key} if key else {}
        return post(entry["url"], {"input": value}, headers)
