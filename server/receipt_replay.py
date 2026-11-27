"""Compare an exact historical run with a fresh, read-only evaluation."""
from contextlib import closing
from hashlib import sha256
import json
import operator
import re
import sqlite3

import agent_schema
import blocks
import graph_values
import guided_improvements
import portable_lookup
from graph_cursor import advance
from graph_spec import digest
from project_resources import ident
from workflows import step_value


PURE_BLOCKS = {"variables", "response", "verify-value", "group-records", "ordered-condition"}
REPLAYABLE = PURE_BLOCKS | {"start", "condition", "review", "human-input", "pinned-table", "finish"}
TERMINAL = {"finished", "failed", "declined"}
MAX_CAPTURE_BYTES = 3 * 1024 * 1024
MAX_OUTPUT_BYTES = 65536
DIFF_FIELDS = ("port", "output", "error", "decision", "inputs")
HASH = re.compile(r"[a-f0-9]{64}")
NUMERIC = {"gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le}


class ReplayBlock(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def capture(workspace, data):
    if set(data) != {"project", "workflow", "version", "run", "digest"}:
        raise ValueError("Choose one saved run receipt and its exact workflow version.")
    project, workflow, run_id = (ident(data[key]) for key in ("project", "workflow", "run"))
    version = data["version"]
    expected_digest = data["digest"]
    if type(version) is not int or version < 1 or not isinstance(expected_digest, str) or not HASH.fullmatch(expected_digest):
        raise ValueError("Choose a saved receipt and its exact workflow version.")
    uri = workspace.file.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        row = db.execute("""
            SELECT r.id, r.workflow, r.version, r.input, r.mode, r.state, r.cursor,
                   CASE WHEN length(CAST(r.history AS BLOB)) <= ? THEN r.history END AS history,
                   CASE WHEN length(CAST(r.context AS BLOB)) <= ? THEN r.context END AS context,
                   v.graph, v.digest, v.binding
            FROM runs r JOIN run_details d USING(id)
            JOIN versions v ON v.workflow=r.workflow AND v.version=r.version
            JOIN workflow_projects p ON p.workflow=r.workflow
            WHERE r.id=? AND d.project=? AND p.project=? AND r.workflow=? AND r.version=?
        """, (MAX_CAPTURE_BYTES, MAX_CAPTURE_BYTES, run_id, project, project, workflow, version)).fetchone()
        if row is None:
            raise PermissionError("Choose a receipt for this exact workflow version and project.")
        if row["digest"] != expected_digest:
            raise ValueError("The saved graph does not match this selection. Reopen the receipt.")
        pins = dict(db.execute("""
            SELECT node, snapshot FROM table_pins WHERE workflow=? AND version=?
        """, (workflow, version)))
        decisions = dict(db.execute("SELECT node, approved FROM run_decisions WHERE id=?", (run_id,)))
    return {**row, "project": project}, pins, decisions


def unsupported(kind):
    if kind == "agent" or kind in blocks.MODELS:
        return ReplayBlock("model", "A model response cannot be regenerated offline from this receipt.")
    if kind in {"service", "source-read"} | blocks.SERVICES | portable_lookup.KINDS:
        return ReplayBlock("external", "This step depends on an external operation or its preview. The connector stays offline.")
    if kind in blocks.RESOURCES | {"deployments", "credential"}:
        return ReplayBlock("project-state", "This step depends on project state or an effect whose exact pre-run state was not captured.")
    if kind in blocks.CHILDREN | blocks.CONTAINERS:
        return ReplayBlock("child-workflow", "Replay a saved child receipt on its own. The parent cannot reproduce its child calls.")
    if kind in {"function", "wait", "guardrails"}:
        return ReplayBlock("runtime", "This step depends on worker or timing behavior that the receipt cannot reproduce.")
    return ReplayBlock("unsupported", "This step has no deterministic offline replay path.")


def condition(config, value, bindings):
    left, right = (graph_values.pin(config[key], bindings) for key in ("left", "right"))
    operation = config["operator"]
    if operation in ("equals", "differs"):
        same = guided_improvements.same_values(left, right)
        passed = same if operation == "equals" else not same
    elif operation == "contains":
        passed = isinstance(left, (str, list, dict)) and right in left
    else:
        if type(left) not in (float, int) or type(right) not in (float, int):
            raise ValueError("Numeric comparison requires two numbers.")
        passed = NUMERIC[operation](left, right)
    return ("true" if passed else "false"), value


def review(kind, value, node, entry, expected, decisions):
    choice = expected.get("decision")
    if choice not in ("approved", "declined"):
        raise ReplayBlock("missing-decision", "This receipt has no recorded decision for the selected branch.")
    approved = choice == "approved"
    if node in decisions and bool(decisions[node]) != approved:
        raise ReplayBlock("inconsistent-decision", "The saved decision does not agree with the step receipt.")
    entry["decision"] = choice
    if kind == "review" or not approved:
        return choice, value
    if "output" not in expected:
        raise ReplayBlock("missing-answer", "This receipt has no captured answer for the human input step.")
    return "success", expected["output"]


def execute(run, node, value, bindings, ctx, pins, decisions, expected, entry):
    kind, config = node["type"], node["config"]
    if kind == "start" and config:
        prepared, error = guided_improvements.replay(config, value)
        if error:
            raise ValueError(error)
        output, entry["inputs"] = prepared
        if config["runMetadata"]:
            output["metadata"] = {"run": run["id"], "workflow": run["workflow"],
                                  "version": run["version"], "mode": run["mode"]}
        return "success", output
    if kind in {"review", "human-input"}:
        return review(kind, value, node["id"], entry, expected, decisions)
    if kind == "pinned-table":
        if node["id"] not in pins:
            raise ReplayBlock("missing-snapshot", "The pinned table snapshot for this version is unavailable.")
        return "success", json.loads(pins[node["id"]])
    if kind == "condition" and "operator" in config:
        return condition(config, value, bindings)
    if kind in PURE_BLOCKS:
        config = {key: graph_values.pin(item, bindings) for key, item in config.items()}
        if kind == "variables":
            config["values"] = {key: graph_values.pin(item, bindings) for key, item in config["values"].items()}
        return blocks.execute(kind, config, value, None, ctx)
    return step_value(node, value, None)


def comparison(entry):
    return {key: entry[key] for key in DIFF_FIELDS if key in entry}


def diff_preview(entry):
    if entry is None:
        return None
    view = {}
    for key, value in comparison(entry).items():
        text = agent_schema.canonical(value)
        if len(text) <= 1024:
            view[key] = {"value": value}
        else:
            encoded = text.encode("utf-8")
            view[key] = {"bytes": len(encoded), "sha256": sha256(encoded).hexdigest()}
    return view


def diff_row(node, expected, observed, *, blocked=None):
    changes = []
    if expected is not None and observed is not None:
        changes = [field for field in DIFF_FIELDS if (field in expected) != (field in observed)
                   or field in expected and not guided_improvements.same_values(expected[field], observed[field])]
    status = ("blocked" if blocked else "not-reached" if observed is None else
              "added" if expected is None else "changed" if changes else "matched")
    row = {"node": node["id"], "label": node.get("label"), "kind": node["type"], "status": status,
           "expected": diff_preview(expected), "observed": diff_preview(observed), "changes": changes}
    if blocked:
        row.update(code=blocked.code, reason=str(blocked))
    elif observed is not None and "decision" in observed:
        row["basis"] = ("Recorded decision and answer" if node["type"] == "human-input"
                        and observed["decision"] == "approved" else "Recorded decision")
    return row


def replay_receipt(workspace, data):
    run, pins, decisions = capture(workspace, data)
    result = {"project": run["project"], "run": run["id"], "workflow": run["workflow"], "version": run["version"],
              "digest": run["digest"], "binding": run["binding"], "status": "blocked",
              "expectedState": run["state"], "observedState": None, "steps": []}
    if run["state"] not in TERMINAL:
        result["reason"] = ("The run ended without a complete receipt boundary." if run["state"] == "interrupted"
                            else "This run has not recorded a terminal receipt.")
        return result
    if run["history"] is None or run["context"] is None:
        result["reason"] = "The captured receipt exceeds the offline comparison size limit."
        return result
    graph = json.loads(run["graph"])
    if digest(graph) != run["digest"] or not isinstance(run["binding"], str) or not HASH.fullmatch(run["binding"]):
        result["reason"] = "The saved graph or its original connection binding could not be verified."
        return result
    history = json.loads(run["history"])
    if not history:
        result["reason"] = "This receipt has no recorded steps for comparison."
        return result
    nodes = {node["id"]: node for node in graph["nodes"]}
    expected = {entry["node"]: entry for entry in history}
    if len(expected) != len(history) or any(key not in nodes or entry.get("type") != nodes[key]["type"]
                                            for key, entry in expected.items()):
        result["reason"] = "The step receipts are inconsistent with the saved workflow version."
        return result
    return compare_run(result, run, graph, nodes, expected, pins, decisions)


def compare_run(result, run, graph, nodes, expected, pins, decisions):
    captured_input = json.loads(run["input"])
    cursor = next(node["id"] for node in nodes.values() if node["type"] == "start")
    ctx, values, visited = {}, {}, {}
    state = None
    while cursor:
        if cursor in visited or cursor not in nodes:
            result["reason"] = "The saved route cannot be traversed consistently."
            break
        node = nodes[cursor]
        kind = node["type"]
        saved = expected.get(cursor)
        if kind not in REPLAYABLE:
            blocked = unsupported(kind)
            visited[cursor] = diff_row(node, saved, None, blocked=blocked)
            result["reason"] = str(blocked)
            break
        value = values.get(ctx.get("previousNode"), captured_input)
        bindings = {"input": captured_input, "previous": value, "outputs": values, "variables": ctx.get("variables", {})}
        entry = {}
        try:
            port, output = execute(run, node, value, bindings, ctx, pins, decisions, saved or {}, entry)
            if len(json.dumps(output, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()) > MAX_OUTPUT_BYTES:
                raise ValueError("Block output exceeds 64 KiB.")
        except ReplayBlock as exc:
            visited[cursor] = diff_row(node, saved, None, blocked=exc)
            result["reason"] = str(exc)
            break
        except Exception as exc:
            entry["error"] = str(exc) if isinstance(exc, (ValueError, KeyError)) else "Step could not complete."
            visited[cursor] = diff_row(node, saved, entry)
            state = "failed"
            break
        entry.update(port=None if kind == "finish" else port, output=output)
        visited[cursor] = diff_row(node, saved, entry)
        if kind == "human-input" and port == "declined":
            state = "declined"
            break
        values[cursor] = output
        ctx.update(previousNode=cursor, previousSucceeded=True)
        if kind == "finish":
            ctx["result"] = output
        targets = [edge["target"] for edge in graph["edges"] if edge["source"] == cursor and edge["sourcePort"] == port]
        cursor, state = advance(graph, ctx, targets, "finished")
    result["steps"] = [*visited.values(), *(diff_row(nodes[key], entry, None)
                                           for key, entry in expected.items() if key not in visited)]
    result["observedState"] = state
    if "reason" not in result:
        original = json.loads(run["context"])
        expected_result = {"output": original["result"]} if "result" in original else {}
        observed_result = {"output": ctx["result"]} if "result" in ctx else {}
        if expected_result or observed_result:
            result["outcome"] = {"expected": diff_preview(expected_result) or None,
                                 "observed": diff_preview(observed_result) or None,
                                 "changed": not guided_improvements.same_values(expected_result, observed_result)}
        result["orderChanged"] = set(visited) == set(expected) and list(visited) != list(expected)
        same_result = not result.get("outcome", {}).get("changed")
        result["status"] = ("matched" if state == run["state"] and not result["orderChanged"] and same_result
                            and all(step["status"] == "matched" for step in result["steps"]) else "different")
    return result
