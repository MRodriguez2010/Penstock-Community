"""Validate the plan before any capability is used, then execute only a single selected block."""
import json
from graph_cursor import topological_order
from graph_spec import graph_id, graph_spec
import agent_schema
import blocks
import structured_start


def bounded(value, label, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must contain 1 to {maximum} characters.")
    return value


def plan_graph(plan, connections, depth=0):
    if depth>3:
        raise ValueError("Use up to three nested containers.")
    if not isinstance(plan, dict) or set(plan) != {"name", "steps"}:
        raise ValueError("A proposal needs a name and a list of steps.")
    bounded(plan["name"], "Workflow name", 100)
    graph = graph_spec(plan["steps"], {})
    ids = set()
    ports = {}
    for raw, node in zip(plan["steps"], graph["nodes"]):
        if set(raw) - {"id", "type", "config", "label", "next"}:
            raise ValueError("Unexpected field in proposal step.")
        key, kind, cfg = node["id"], node["type"], node["config"]
        if key in ids:
            raise ValueError("Use distinct step IDs.")
        ids.add(key)
        bounded(node.get("label", key), "Step label", 100)
        allowed = {"start": set(), "finish": set(), "review": {"question"}, "condition": {"contains"},
                   "agent": {"provider", "instructions", "skill", "schema"}, "service": {"service","preview"}}
        if kind=="condition" and "operator" in cfg:
            if set(cfg)!={"left","operator","right"} or cfg["operator"] not in ("equals","differs","gt","gte","lt","lte","contains"):
                raise ValueError("Choose a comparison operator, left value and right value.")
            ports[key]={"true","false"}
            continue
        if kind == "start" and cfg:
            structured_start.config(cfg)
            allowed["start"] = set(cfg)
        if kind not in allowed:
            ports[key] = blocks.validate(kind,cfg,connections)
            if kind in blocks.CONTAINERS:
                plan_graph(cfg["body"],connections,depth+1)
            continue
        if set(cfg) - allowed[kind]:
            raise ValueError(f"Unknown type or configuration at {key}.")
        if kind == "agent":
            if cfg.get("provider") not in connections.providers:
                raise ValueError(f"Select an installed provider for {key}.")
            bounded(cfg.get("instructions"), "Model instructions", 8000)
            if "skill" in cfg and cfg["skill"] not in connections.skills:
                raise ValueError(f"Select an installed skill for {key}.")
            if "schema" in cfg:
                agent_schema.check(cfg["schema"])
        if kind == "service" and connections.services.get(cfg.get("service"), {"protocol":"missing"}).get("protocol","http") != "http":
            raise ValueError(f"Select a registered service for {key}.")
        if kind == "review":
            bounded(cfg.get("question"), "Review question", 1000)
        if kind == "condition":
            bounded(cfg.get("contains"), "Condition text", 500)
        ports[key] = {"review": {"approved","declined"}, "condition": {"true","false"},
                      "finish": set()}.get(kind, {"success"})
    starts = [n["id"] for n in graph["nodes"] if n["type"] == "start"]
    if len(starts) != 1:
        raise ValueError("Use exactly one Start.")
    wired = {key: set() for key in ids}
    targets = set()
    edges = set()
    for e in graph["edges"]:
        source, port, target = e["source"], e["sourcePort"], e["target"]
        if target not in ids or target == starts[0] or port not in ports[source]:
            raise ValueError(f"Invalid connection at {source}.")
        if (source, port, target) in edges:
            raise ValueError("Duplicate connection.")
        edges.add((source, port, target))
        targets.add(target)
        wired[source].add(port)
    if ids - targets != set(starts) or len(topological_order(graph)) != len(ids):
        raise ValueError("Every step must be reachable from Start and the workflow must be acyclic.")
    if wired != ports:
        raise ValueError("Connect every output to a step that ultimately reaches Finish.")
    return graph


GUIDE = """Return only a JSON object with name (string) and steps (array, at most 40).
Each step has id (unique letters, digits, underscore or dash), type, label, config and next.
Start with type start and end each path with finish. Both use config {}. Omit next on finish.
A normal next is a step ID, or a mapping of output name to ID or list of IDs for fan-out.
A review has config {question:string} and next {approved:id,declined:id}.
A condition has config {contains:string} and next {true:id,false:id}.
An agent has config {provider:registeredName,instructions:string,skill?:registeredName}, next on success.
A service has config {service:registeredName}, next on success; its POST requires human approval.
Each step receives the previous selected step's output. A join uses its first selected predecessor.
Use only registered capabilities in the catalog. Acyclic graphs only. Do not invent capabilities.
Do not perform the requested task. Propose a workflow for a person to inspect and test."""


def propose(connections, provider, brief, project_catalog=None):
    bounded(brief, "Workflow brief", 8000)
    if provider not in connections.providers:
        raise ValueError("Choose a configured provider to draft the workflow.")
    reply = connections.complete(provider, GUIDE + "\nAdditional blocks and fields: " + json.dumps(blocks.catalog()) + "\nUse {ref:{node:'$input',path:[]}} to bind block fields. Router uses its choice names as ports. Guardrails and verify-value use success and error.\nCatalog: " + json.dumps({**connections.catalog(),"project":project_catalog or {}}), brief).strip()
    if reply.startswith("```") and reply.endswith("```"):
        reply = reply.split("\n", 1)[-1][:-3].strip()
    try:
        plan = json.loads(reply)
        plan_graph(plan, connections)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ValueError(f"Model returned an invalid workflow proposal: {exc}") from None
    return plan


def step_value(node, value, connections):
    cfg = node["config"]
    kind = node["type"]
    if kind == "condition":
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return ("true" if cfg["contains"].casefold() in text.casefold() else "false"), value
    if kind == "agent":
        system = cfg["instructions"]
        if "skill" in cfg:
            system += "\n\n" + connections.skills[cfg["skill"]]
        if "schema" in cfg:
            system += "\nReturn only JSON matching this schema:\n" + json.dumps(cfg["schema"])
        prompt = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        result = connections.complete(cfg["provider"], system, prompt)
        if "schema" in cfg:
            result = json.loads(result)
            agent_schema.validate(cfg["schema"], result)
        return "success", result
    return "success", value
