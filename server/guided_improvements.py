"""Suggest exact, offline-replayed Start type corrections from complete bounded evidence."""
from collections import Counter
import copy
import json
import re

import structured_start


RECENT_FAILURES = 80
VERSIONS_PER_VIEW = 8
HISTORY_PER_VERSION = 100
MAX_SUGGESTIONS = 12
START_ERROR = re.compile(r"Start ([A-Za-z][A-Za-z0-9_]{0,63}): .+")
START_HISTORY_BYTES = 2048
LABELS = {"string": "text", "number": "a number", "boolean": "a boolean",
          "object": "a JSON object", "array": "a JSON array"}
JSON_TYPES = {str: "string", int: "number", float: "number", bool: "boolean", dict: "object", list: "array"}


def start_error(text):
    match = START_ERROR.fullmatch(text) if isinstance(text, str) else None
    return match[1] if match else None


def recent_scopes(c, project):
    records = c.execute("""
        WITH recent AS (
            SELECT r.workflow,r.version,r.history FROM runs r JOIN workflow_projects p USING(workflow)
            WHERE p.project=? AND r.state='failed' ORDER BY r.created DESC,r.rowid DESC LIMIT ?
        )
        SELECT workflow,version,CASE WHEN length(CAST(history AS BLOB))<=? THEN
            CASE WHEN json_array_length(history)=1 AND json_extract(history,'$[0].type')='start'
                 THEN json_extract(history,'$[0].error') END END AS error FROM recent
        """, (project, RECENT_FAILURES, START_HISTORY_BYTES))
    counts = Counter()
    for record in records:
        field = start_error(record["error"])
        if field:
            counts[record["workflow"], record["version"], field] += 1
    scopes = {}
    for (workflow, version, field), count in counts.items():
        if count >= 2:
            scopes.setdefault((workflow, version), []).append(field)
    return list(scopes.items())[:VERSIONS_PER_VIEW]


def edition_history(c, project, workflow, version):
    edition = c.execute("""
        SELECT v.name,v.plan FROM versions v JOIN workflow_projects p USING(workflow)
        WHERE p.project=? AND v.workflow=? AND v.version=?
        """, (project, workflow, version)).fetchone()
    if edition is None:
        return None
    records = c.execute("""
        SELECT id,input,CASE WHEN state='failed' AND length(CAST(history AS BLOB))<=? THEN
            CASE WHEN json_array_length(history)=1 AND json_extract(history,'$[0].type')='start'
                 THEN json_extract(history,'$[0].error') END END AS error
        FROM runs WHERE workflow=? AND version=? ORDER BY created,rowid LIMIT ?
        """, (START_HISTORY_BYTES, workflow, version, HISTORY_PER_VERSION + 1)).fetchall()
    if len(records) > HISTORY_PER_VERSION:
        return None
    return dict(edition), records


def replay(cfg, value):
    try:
        return structured_start.prepare(cfg, value), None
    except ValueError as exc:
        return None, str(exc)


def same_values(left, right):
    return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(right, sort_keys=True, ensure_ascii=False)


def evaluate(workflow, version, field_name, history):
    edition, records = history
    plan = json.loads(edition["plan"])
    start = next(step for step in plan["steps"] if step["type"] == "start")
    current = start.get("config", {})
    field = next((f for f in current.get("inputFormat", []) if f["name"] == field_name), None)
    if field is None:
        return None
    examples = [(record, json.loads(record["input"])) for record in records]
    if any(not isinstance(value, dict) for _, value in examples):
        return None
    observed = {JSON_TYPES[type(value[field_name])] for _, value in examples if value.get(field_name) is not None}
    if len(observed) != 1 or field["type"] in observed:
        return None
    target = observed.pop()
    after = copy.deepcopy(current)
    next(f for f in after["inputFormat"] if f["name"] == field_name)["type"] = target
    try:
        structured_start.config(after)
        if "value" in field and not same_values(structured_start.coerce(field["type"], field["value"]),
                                                 structured_start.coerce(target, field["value"])):
            return None
    except ValueError:
        return None

    evidence, results = [], []
    for record, value in examples:
        before_result, before_error = replay(current, value)
        after_result, _ = replay(after, value)
        if after_result is None or before_result is not None and not same_values(before_result, after_result):
            return None
        if before_error is not None:
            if start_error(before_error) != field_name or before_error != record["error"]:
                return None
            evidence.append(record["id"])
        results.append({"run": record["id"], "before": before_result is not None, "after": True})
    if len(evidence) < 2:
        return None
    start["config"] = after
    details = {"workflow": workflow, "version": version, "field": field_name, "name": edition["name"],
               "fromType": field["type"], "toType": target, "evidence": evidence, "historicalInputs": len(results),
               "proposal": f"Accept {LABELS[target]} in {field_name} at Start. Review downstream steps that use this field."}
    return details, plan, results


def improvements(workspace, project):
    with workspace.db() as c:
        versions = [(w, v, fields, edition_history(c, project, w, v)) for (w, v), fields in recent_scopes(c, project)]
    items = []
    for workflow, version, fields, history in versions:
        if history is None:
            continue
        for field in fields:
            change = evaluate(workflow, version, field, history)
            if change:
                items.append(change[0])
                if len(items) >= MAX_SUGGESTIONS:
                    return {"items": items}
    return {"items": items}


def test_change(workspace, project, data):
    workflow, version, field = data["workflow"], data["version"], data["field"]
    if not isinstance(workflow, str) or type(version) is not int or not isinstance(field, str):
        raise ValueError("Choose an exact workflow version and field.")
    with workspace.db() as c:
        history = edition_history(c, project, workflow, version)
    change = evaluate(workflow, version, field, history) if history is not None else None
    if change is None:
        raise ValueError("The change needs two matching Start failures and an exact type that safely fits every saved input for this version.")
    details, plan, results = change
    return {**details, "results": results, "plan": plan, "passed": True,
            "summary": "Only the input step was replayed against the saved inputs for this version. Test the complete draft before publishing."}
