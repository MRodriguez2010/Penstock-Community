"""Typed reads from IT-installed project lookup adapters."""
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import graph_values
import project_resources


SCHEMAS = {
    "web-search": {"search": ({"title": 160}, {"snippet": 500})},
    "github": {
        "repository": ({"name": 200}, {"description": 500, "defaultBranch": 120}),
        "issues": ({"title": 160, "number": int}, {"state": {"open", "closed"}}),
        "pull-requests": ({"title": 160, "number": int}, {"state": {"open", "closed", "merged"}}),
    },
    "enrichment": {
        "person": ({"name": 160}, {"jobTitle": 160, "company": 160}),
        "company": ({"name": 160}, {"domain": 253, "industry": 160, "description": 500}),
    },
    "service-health": {"service-health": (
        {"service": 160, "status": {"healthy", "degraded", "unavailable", "unknown"}, "checkedAt": datetime},
        {"latencyMs": (0, 300000)},
    )},
    "worker-status": {"worker-status": (
        {"worker": 160, "status": {"ready", "busy", "offline", "unknown"}, "observedAt": datetime}, {},
    )},
    "published-workflow": {"published-workflow": (
        {"workflow": 160, "version": 120, "publishedAt": datetime}, {"name": 160},
    )},
    "weather-observation": {"weather-observation": (
        {"station": 120, "observedAt": datetime, "temperatureC": (-100, 70)},
        {"humidityPercent": (0, 100), "windSpeedMps": (0, 150), "pressureHpa": (300, 1150)},
    )},
}
KINDS = frozenset(SCHEMAS)
RESOURCE_LOOKUPS = {"service-health", "worker-status", "published-workflow", "weather-observation"}
SINGLE_RESULTS = RESOURCE_LOOKUPS | {"repository"}
LABELS = {"search": "Search the web", "repository": "Look up a repository", "issues": "List repository issues",
          "pull-requests": "List repository pull requests", "person": "Find a professional profile by work email",
          "company": "Look up a company by domain", "service-health": "Check service health",
          "worker-status": "Check worker status", "published-workflow": "Look up a published workflow",
          "weather-observation": "Read a weather observation"}
SAMPLES = {
    "search": {"title": "Example search result", "snippet": "Sample evidence for your offline workflow."},
    "repository": {"name": "example/repository", "defaultBranch": "main", "description": "Sample repository."},
    "issues": {"number": 1, "title": "Example issue", "state": "open"},
    "pull-requests": {"number": 2, "title": "Example pull request", "state": "merged"},
    "person": {"name": "Example Person", "jobTitle": "Operations lead", "company": "Example Company"},
    "company": {"name": "Example Company", "domain": "example.org", "industry": "Business services"},
    "service-health": {"service": "Example service", "status": "healthy", "latencyMs": 42,
                       "checkedAt": "2025-01-01T12:00:00Z"},
    "worker-status": {"worker": "Example worker", "status": "ready", "observedAt": "2025-01-01T12:00:00Z"},
    "published-workflow": {"workflow": "example-workflow", "version": "2.0", "name": "Example workflow",
                           "publishedAt": "2025-01-01T12:00:00Z"},
    "weather-observation": {"station": "example-station", "observedAt": "2025-01-01T12:00:00Z",
                            "temperatureC": 12.5, "humidityPercent": 64, "windSpeedMps": 5},
}
PROFILE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
RESOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)\Z", re.ASCII)
REPOSITORY = re.compile(r"[A-Za-z0-9_-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
DOMAIN = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}\Z")
WORK_EMAIL = re.compile(r"[A-Za-z0-9_.+%-]{1,64}@([^@]+)\Z")


def load(config, check_endpoint):
    if not isinstance(config, dict) or len(config) > 32:
        raise ValueError("Configure up to 32 named lookup adapters.")
    for name, entry in config.items():
        if not PROFILE.fullmatch(name):
            raise ValueError("Use an alphanumeric name for each lookup adapter.")
        if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
            raise ValueError("Give the lookup adapter a full endpoint URL.")
        if "key_env" in entry and not isinstance(entry["key_env"], str):
            raise ValueError("Set an environment variable name for the lookup adapter.")
        check_endpoint(name, entry, {"kind", "projects"})
        endpoint = urlsplit(entry["url"])
        if (not entry["url"].isascii() or any(c.isspace() or c == "\\" for c in entry["url"]) or
                endpoint.scheme != "https" and endpoint.hostname not in {"127.0.0.1", "::1", "localhost"}):
            raise ValueError("Lookup adapters require HTTPS or a loopback address.")
        if endpoint.port is not None and not 0 < endpoint.port <= 65535:
            raise ValueError("Choose a valid lookup adapter port.")
        if not isinstance(entry.get("kind"), str) or entry["kind"] not in KINDS:
            raise ValueError("Choose a supported lookup adapter kind.")
        projects = entry.get("projects")
        if not isinstance(projects, list) or not 1 <= len(projects) <= 64:
            raise ValueError("Assign each lookup adapter to one or more project IDs.")
        for project in projects:
            project_resources.ident(project)
        if len(set(projects)) != len(projects):
            raise ValueError("List each project once per lookup adapter.")
    return config


def profiles(connections, project):
    return {name: entry for name, entry in getattr(connections, "lookups", {}).items() if project in entry["projects"]}


def catalog(connections, project):
    return {name: {"kind": entry["kind"], "host": urlsplit(entry["url"]).netloc,
                   "operations": sorted(SCHEMAS[entry["kind"]])}
            for name, entry in profiles(connections, project).items()}


def selected(connections, kind, name, project=None):
    known = getattr(connections, "lookups", {}) if project is None else profiles(connections, project)
    entry = known.get(name) if isinstance(name, str) else None
    if not entry or entry["kind"] != kind:
        raise ValueError("Choose an installed lookup adapter for this project and step.")
    return entry


def bounded_text(value, size):
    return (isinstance(value, str) and 1 <= len(value.encode("utf-8", errors="replace")) <= size and
            bool(value.strip()) and all(c.isprintable() or c in "\t\n\r" for c in value))


def request_fields(cfg):
    operation = cfg["operation"]
    query = cfg["query"]
    if not bounded_text(query, 300) or any(c in "\t\n\r" for c in query):
        raise ValueError("Supply one line of lookup input, up to 300 UTF-8 bytes.")
    query = query.strip()
    if operation in SCHEMAS["github"] and (not REPOSITORY.fullmatch(query) or query.split("/")[-1] in {".", ".."}):
        raise ValueError("Enter a repository as owner/name.")
    if operation == "person":
        email = WORK_EMAIL.fullmatch(query)
        if not email or not DOMAIN.fullmatch(email[1]) or len(query) > 254:
            raise ValueError("Enter a work email address for the professional profile lookup.")
    if operation == "company" and (not DOMAIN.fullmatch(query) or len(query) > 253):
        raise ValueError("Enter a company domain, such as example.org.")
    if operation in RESOURCE_LOOKUPS and not RESOURCE_ID.fullmatch(query):
        raise ValueError("Enter the named resource ID supplied by the lookup adapter, up to 80 characters.")
    limit = cfg["limit"]
    maximum = 1 if operation in SINGLE_RESULTS else 10
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise ValueError("This lookup accepts exactly one result." if maximum == 1 else "Choose up to ten results.")
    return {"operation": operation, "query": query, "limit": limit}


def validate(kind, cfg):
    if not isinstance(cfg["adapter"], str) or not PROFILE.fullmatch(cfg["adapter"]):
        raise ValueError("Choose an installed lookup adapter for this project and step.")
    if not isinstance(cfg["operation"], str) or cfg["operation"] not in SCHEMAS[kind]:
        raise ValueError("Choose an operation for this lookup block.")
    bound = cfg["query"]
    if isinstance(bound, dict) and set(bound) == {"ref"}:
        graph_values.check_reference(bound["ref"])
        if cfg["operation"] in RESOURCE_LOOKUPS:
            bound = "example-resource"
        else:
            bound = {"search": "example", "person": "person@example.org", "company": "example.org"}.get(cfg["operation"], "example/repository")
    request_fields({**cfg, "query": bound})
    return {"success"}


def valid_source(value):
    if not bounded_text(value, 1200) or not value.isascii() or any(c.isspace() or c == "\\" for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and bool(parsed.hostname) and
                not (parsed.username or parsed.password or parsed.fragment) and
                (parsed.port is None or 0 < parsed.port <= 65535))
    except ValueError:
        return False


def valid_value(value, rule):
    if rule is int:
        return type(value) is int and 1 <= value < 2**31
    if rule is datetime:
        if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
            return False
        try:
            moment = datetime.fromisoformat(value)
            return moment <= datetime.now(timezone.utc) + timedelta(minutes=5)
        except (ValueError, OverflowError):
            return False
    if isinstance(rule, tuple):
        return type(value) in (float, int) and rule[0] <= value <= rule[1]
    if isinstance(rule, set):
        return isinstance(value, str) and value in rule
    return bounded_text(value, rule)


def result_items(response, operation, limit, kind):
    if (not isinstance(response, dict) or set(response) != {"items"} or not isinstance(response["items"], list)
            or len(response["items"]) > limit):
        raise ValueError("Lookup adapter returned an unexpected result list.")
    required, optional = SCHEMAS[kind][operation]
    items = response["items"]
    for item in items:
        if (not isinstance(item, dict) or not (required.keys() | {"sourceUrl"}) <= item.keys() or
                item.keys() - (required.keys() | optional.keys() | {"sourceUrl"}) or not valid_source(item["sourceUrl"])):
            raise ValueError("Lookup adapter returned an invalid record or evidence URL.")
        if any(field in item and not valid_value(item[field], rule) for field, rule in (required | optional).items()):
            raise ValueError("Lookup adapter returned an invalid field value.")
    return items


def execute(connections, kind, cfg, project, preview):
    entry = selected(connections, kind, cfg["adapter"], project)
    payload = request_fields(cfg)
    operation = payload["operation"]
    credential = None
    if preview:
        response = {"items": [{**SAMPLES[operation], "sourceUrl": "https://example.org/fixtures/" + operation}]}
    else:
        from connections import post
        credential = connections.secret(entry)
        headers = {"Authorization": "Bearer " + credential} if credential else {}
        response = post(entry["url"], payload, headers, timeout=10)
    items = result_items(response, operation, payload["limit"], kind)
    if credential and any(isinstance(value, str) and credential in value for item in items for value in item.values()):
        raise ValueError("Lookup adapter returned its credential in the response.")
    provenance = {"adapter": cfg["adapter"], "operation": operation, "mode": "sample" if preview else "live"}
    if not preview:
        provenance["retrievedAt"] = datetime.now(timezone.utc).isoformat()
    return {"items": items, "provenance": provenance}
