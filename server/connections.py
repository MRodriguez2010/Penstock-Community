"""Workspace-owned endpoints and instruction files. Workflow data can only select registered names."""
import agent_schema
import hashlib
import json
import os
import portable_lookup
import re
import tomllib
from pathlib import Path
from urllib import error, request
from urllib.parse import urlsplit


DESKTOP_PACK_PORTS = {"smollm3-3b": 45371, "olmo-3-7b-think": 45372}
DESKTOP_PACK_TIMEOUT = 300


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def post(url, payload, headers, *, timeout=90):
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    req = request.Request(url, data=body, headers={"Content-Type": "application/json", **headers})
    # Contact only the configured endpoint. Do not forward credentials through redirects.
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as response:
            data = response.read(262145)
    except error.HTTPError as exc:
        exc.close()
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
        if set(config) - {"providers", "services", "skills", "lookups"}:
            raise ValueError("Unknown section in connections.toml.")
        self.providers = config.get("providers", {})
        self._desktop_pack = None
        self.services = config.get("services", {})
        self.lookups = portable_lookup.load(config.get("lookups", {}), self.check_endpoint)
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
            self.check_endpoint(name, entry, {"protocol","method","tools","endpoint_urls","description","input_schema"})
            protocol = entry.get("protocol","http")
            if protocol not in ("http","rss","mcp","a2a"):
                raise ValueError(f"Unknown service protocol for {name}.")
            allowed = {"http":{"method","input_schema"}, "mcp":{"tools"}, "a2a":{"endpoint_urls"}, "rss":set()}[protocol]
            if set(entry) - {"url","key_env","protocol","description"} - allowed:
                raise ValueError(f"Unexpected protocol fields for {name}.")
            if "description" in entry and (not isinstance(entry["description"],str) or len(entry["description"])>1000):
                raise ValueError("Service descriptions may contain up to 1,000 characters.")
            if "input_schema" in entry:
                agent_schema.check(entry["input_schema"])
            if "method" in entry and entry["method"] not in ("GET","POST"):
                raise ValueError("HTTP service method must be GET or POST.")
            for field in ("tools","endpoint_urls"):
                if field in entry and (not isinstance(entry[field],list) or len(entry[field])>64 or
                                       not all(isinstance(v,str) and 1<=len(v)<=2048 for v in entry[field])):
                    raise ValueError(f"Supply a bounded list of {field} for {name}.")
            for url in entry.get("endpoint_urls",[]):
                self.check_endpoint(name,{"url":url},set())
        for name, relative in config.get("skills", {}).items():
            if not isinstance(relative, str) or Path(relative).is_absolute():
                raise ValueError(f"Use a workspace-relative Markdown file for {name}.")
            file = (self.root / relative).resolve()
            if not file.is_relative_to(self.root.resolve()) or file.suffix.lower() != ".md" or file.stat().st_size > 16384:
                raise ValueError(f"Skill {name} must be a workspace Markdown file of at most 16 KiB.")
            self.skills[name] = file.read_text("utf-8")
        from setup_profiles import load_profiles
        load_profiles(self)

    def add_desktop_providers(self, raw):
        """Register the current desktop selection in memory, without rewriting the workspace."""
        if not raw:
            return
        if len(raw) > 4096:
            raise ValueError("The desktop model selection is too large.")
        selection = json.loads(raw)
        if not isinstance(selection, dict) or len(selection) != 1 or len(self.providers) >= 128:
            raise ValueError("Select one desktop model, with room for another workspace provider.")
        name, entry = next(iter(selection.items()))
        if name not in {"desktop-smollm3-3b", "desktop-olmo-3-7b-think", "desktop-endpoint"}:
            raise ValueError("Choose a recognized desktop provider.")
        if name in self.providers:
            raise ValueError(f"The workspace already defines {name}. Choose another desktop model or open without one.")
        self.check_endpoint(name, entry, {"protocol", "model"})
        protocol = entry.get("protocol")
        model = entry.get("model")
        if protocol not in {"chat-completions", "ollama", "anthropic-messages"} or not isinstance(model, str) or not model.strip():
            raise ValueError("Choose a supported protocol and model ID.")
        if name != "desktop-endpoint":
            pack_id = name.removeprefix("desktop-")
            url = f"http://127.0.0.1:{DESKTOP_PACK_PORTS[pack_id]}/v1/chat/completions"
            if entry != {"url": url, "protocol": "chat-completions", "model": pack_id,
                         "key_env": "PENSTOCK_DESKTOP_MODEL_KEY"}:
                raise ValueError("The desktop model pack selection must use its bundled loopback runtime.")
            self._desktop_pack = name
        self.providers[name] = dict(entry)

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
                "skills": sorted(self.skills), "services": sorted(self.services),
                "serviceCapabilities": {name:{k:e[k] for k in ("description","input_schema","tools","method") if k in e}
                                        for name,e in self.services.items()},
                "serviceProtocols": {name: entry.get("protocol","http") for name,entry in self.services.items()}}

    def binding(self, graph):
        """Fingerprint the exact selected destinations, model IDs and skill text, excluding secret values."""
        selected = []
        for node in graph["nodes"]:
            cfg = node["config"]
            if node["type"] in ("loop-container","parallel-container"):
                from graph_spec import graph_spec
                selected.append(self.binding(graph_spec(cfg["body"]["steps"],{})))
            elif "provider" in cfg:
                selected.append([self.providers.get(cfg["provider"]), self.skills.get(cfg.get("skill"))])
            elif "service" in cfg:
                selected.append(self.services.get(cfg["service"]))
            elif node["type"] in portable_lookup.KINDS:
                selected.append(self.lookups.get(cfg["adapter"]))
        encoded = json.dumps(selected, sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(encoded).hexdigest()

    def complete(self, name, system, prompt, image=None, *, draft=False):
        entry = self.providers[name]
        protocol, model = entry["protocol"], entry["model"]
        key = self.secret(entry)
        headers = {"Authorization": "Bearer " + key} if key else {}
        messages = [{"role": "user", "content": prompt}]
        if image:
            mime, data = image
            if protocol == "ollama":
                messages[0]["images"] = [data]
            elif protocol == "anthropic-messages":
                messages[0]["content"] = [{"type":"image","source":{"type":"base64","media_type":mime,"data":data}},
                                          {"type":"text","text":prompt}]
            else:
                messages[0]["content"] = [{"type":"text","text":prompt},
                                          {"type":"image_url","image_url":{"url":f"data:{mime};base64,{data}"}}]
        if protocol == "anthropic-messages":
            headers = {"anthropic-version": "2023-06-01"}
            if key:
                headers["x-api-key"] = key
            payload = {"model": model, "system": system, "max_tokens": 4096, "messages": messages}
        else:
            messages.insert(0, {"role": "system", "content": system})
            payload = {"model": model, "messages": messages, "stream": False}
        if name == self._desktop_pack:
            if draft:
                payload["response_format"] = {"type": "json_object"}
                payload["temperature"] = 0
                if model == "smollm3-3b":
                    payload["chat_template_kwargs"] = {"enable_thinking": False}
                    payload["reasoning_effort"] = "none"
                else:
                    payload["reasoning_budget_tokens"] = 128
            response = post(entry["url"], payload, headers, timeout=DESKTOP_PACK_TIMEOUT)
        else:
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
