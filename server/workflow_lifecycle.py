"""Project-scoped workflow recovery and reviewed activation of a saved release."""
import json
from pathlib import Path
import time

import structured_start
from project_resources import ident
from store import row, rows


ENDPOINTS = {
    "workflow-lifecycle-state", "workflow-lifecycle-archive",
    "workflow-lifecycle-recover", "workflow-lifecycle-rollback",
}


class LifecycleConflict(ValueError):
    pass


def setup(c):
    c.execute("""CREATE TABLE IF NOT EXISTS workflow_lifecycle(
        workflow TEXT PRIMARY KEY REFERENCES workflow_projects(workflow),
        generation INTEGER NOT NULL,
        active_version INTEGER,
        archived INTEGER NOT NULL CHECK(archived IN (0,1)),
        live_paused INTEGER NOT NULL CHECK(live_paused IN (0,1)))""")
    c.execute("""CREATE TABLE IF NOT EXISTS workflow_lifecycle_actions(
        project TEXT NOT NULL, workflow TEXT NOT NULL REFERENCES workflow_projects(workflow),
        actor TEXT NOT NULL, request TEXT NOT NULL, fingerprint TEXT NOT NULL, receipt TEXT NOT NULL,
        PRIMARY KEY(project,actor,request))""")
    c.execute("""CREATE INDEX IF NOT EXISTS lifecycle_workflow_history
                 ON workflow_lifecycle_actions(project,workflow)""")


def metadata(c, workflow):
    saved = row(c, "SELECT generation,active_version,archived,live_paused FROM workflow_lifecycle WHERE workflow=?", workflow)
    if saved is not None:
        return {"generation": saved["generation"], "activeVersion": saved["active_version"],
                "archived": bool(saved["archived"]), "livePaused": bool(saved["live_paused"])}
    active = c.execute("SELECT max(version) FROM versions WHERE workflow=? AND published=1", (workflow,)).fetchone()[0]
    return {"generation": 0, "activeVersion": active, "archived": False, "livePaused": False}


def require_available(c, workflow, *, live=False):
    state = metadata(c, workflow)
    if state["archived"]:
        raise LifecycleConflict("Recover this archived workflow before editing, publishing, or starting it.")
    if live and state["livePaused"]:
        raise LifecycleConflict("Activate a published release before running this recovered draft live.")
    return state


def store_state(c, workflow, prior, *, active, archived=False, paused=False):
    generation = prior["generation"] + 1
    c.execute("""INSERT INTO workflow_lifecycle VALUES(?,?,?,?,?)
                ON CONFLICT(workflow) DO UPDATE SET generation=excluded.generation,
                active_version=excluded.active_version,archived=excluded.archived,live_paused=excluded.live_paused""",
              (workflow, generation, active, archived, paused))
    return generation


def published(c, workflow, version, state):
    store_state(c, workflow, state, active=version)


def edited(c, workflow, state):
    store_state(c, workflow, state, active=state["activeVersion"], paused=state["livePaused"])


def withdrawn(c, workflow, version, state):
    active = None if state["activeVersion"] == version else state["activeVersion"]
    store_state(c, workflow, state, active=active, paused=state["livePaused"])


def workflow_scope(c, project, workflow):
    if not c.execute("""SELECT 1 FROM workflow_projects WHERE project=? AND workflow=?
                         AND NOT EXISTS (SELECT 1 FROM container_bodies WHERE workflow=?)""",
                     (project, workflow, workflow)).fetchone():
        raise ValueError("Choose a workflow in this project.")


def attach_project(workspace, c, project, *, atomic_writes=False):
    path = Path(workspace.data.file(project))
    if not path.is_file():
        raise ValueError("The project workspace is unavailable.")
    c.execute("ATTACH DATABASE ? AS lifecycle_project", (str(path),))
    if atomic_writes:
        # Different files participate in one durable commit using SQLite's rollback journals.
        for database in ("main", "lifecycle_project"):
            journal = c.execute(f"PRAGMA {database}.journal_mode").fetchone()[0]
            if journal.lower() not in {"delete", "truncate", "persist"}:
                raise ValueError("Changes spanning workflow and schedule data need SQLite rollback journaling.")


def schedule_list(c, workflow):
    items = rows(c, """SELECT id,version,enabled,next_due,config FROM lifecycle_project.triggers
                       WHERE workflow=? AND kind='schedule' ORDER BY id""", workflow)
    return [{"id": item["id"], "version": item["version"], "enabled": bool(item["enabled"]),
             "nextDue": item["next_due"], "timing": json.loads(item["config"])["timing"]}
            for item in items]


def snapshot(workspace, c, project, workflow):
    attach_project(workspace, c, project)
    versions = rows(c, """SELECT version,name,published FROM versions
                           WHERE workflow=? ORDER BY version DESC""", workflow)
    receipts = rows(c, """SELECT receipt FROM workflow_lifecycle_actions WHERE project=? AND workflow=?
                           ORDER BY rowid DESC LIMIT 30""", project, workflow)
    return {**metadata(c, workflow), "project": project, "workflow": workflow,
            "latestVersion": versions[0]["version"], "versions": versions,
            "schedules": schedule_list(c, workflow), "history": [json.loads(r["receipt"]) for r in receipts]}


def validate_action(body, action):
    expected = {"project", "workflow", "expectedGeneration", "request"}
    if action == "rollback":
        expected |= {"version", "schedules"}
    if set(body) != expected:
        raise ValueError("Review the workflow lifecycle action and its required fields.")
    if type(body["expectedGeneration"]) is not int or body["expectedGeneration"] < 0:
        raise ValueError("Reload the workflow lifecycle state before confirming.")
    request = body["request"]
    if not isinstance(request, str) or not request.isascii() or not 1 <= len(request) <= 80:
        raise ValueError("Supply a request identifier between 1 and 80 characters.")
    if action != "rollback":
        return []
    if type(body["version"]) is not int or body["version"] < 1:
        raise ValueError("Choose an exact published version.")
    changes = body["schedules"]
    if not isinstance(changes, list) or len(changes) > 100:
        raise ValueError("Choose up to 100 individually reviewed schedules.")
    ids = set()
    for item in changes:
        if (not isinstance(item, dict) or set(item) != {"id", "version", "enabled"} or
                type(item["version"]) is not int or item["version"] < 1 or type(item["enabled"]) is not bool):
            raise ValueError("Each reviewed schedule needs its ID, pinned version and enabled state.")
        key = ident(item["id"])
        if key in ids:
            raise ValueError("Choose each schedule at most once.")
        ids.add(key)
    return sorted(changes, key=lambda item: item["id"])


def target_version(workspace, c, workflow, version):
    target = row(c, "SELECT graph,binding,published FROM versions WHERE workflow=? AND version=?", workflow, version)
    if target is None or not target["published"]:
        raise ValueError("Choose a previously published version of this workflow.")
    if not c.execute("""SELECT 1 FROM runs WHERE workflow=? AND version=?
                         AND mode IN ('offline','test') AND state='finished'""",
                     (workflow, version)).fetchone():
        raise LifecycleConflict("Finish a test of this exact version before activating it.")
    graph = json.loads(target["graph"])
    workspace.check_binding({**target, "graph": graph})
    return graph


def rebind_schedules(workspace, c, project, workflow, version, schedule_changes, target_graph):
    attach_project(workspace, c, project, atomic_writes=True)
    start = next(node["config"] for node in target_graph["nodes"] if node["type"] == "start")
    applied = []
    for review in schedule_changes:
        existing = row(c, """SELECT id,version,enabled,config FROM lifecycle_project.triggers
                              WHERE id=? AND workflow=? AND kind='schedule'""", review["id"], workflow)
        if (existing is None or existing["version"] != review["version"] or
                bool(existing["enabled"]) != review["enabled"]):
            raise LifecycleConflict("A reviewed schedule changed. Reload and review the exact pins again.")
        if start:
            structured_start.prepare(start, json.loads(existing["config"])["input"])
        if existing["version"] != version:
            c.execute("UPDATE lifecycle_project.triggers SET version=? WHERE id=?", (version, existing["id"]))
            applied.append({"id": existing["id"], "from": existing["version"], "to": version,
                            "enabled": bool(existing["enabled"])})
    return applied


def archive(workspace, c, project, workflow):
    attach_project(workspace, c, project, atomic_writes=True)
    paused = c.execute("""UPDATE lifecycle_project.triggers SET enabled=0
                           WHERE workflow=? AND enabled=1""", (workflow,)).rowcount
    queued = c.execute("""UPDATE lifecycle_project.deliveries
                           SET state='paused',detail='Workflow archived before delivery.'
                           WHERE workflow=? AND state='ready'""", (workflow,)).rowcount
    return {"pausedTriggers": paused, "pausedDeliveries": queued}


def dispatch(workspace, endpoint, body, *, actor):
    if endpoint not in ENDPOINTS:
        raise ValueError("Unknown workflow lifecycle action.")
    if not isinstance(actor, str) or not actor:
        raise PermissionError("A signed-in workflow actor is required.")
    project, workflow = ident(body["project"]), ident(body["workflow"])
    action = endpoint.removeprefix("workflow-lifecycle-")
    if action == "state":
        if set(body) != {"project", "workflow"}:
            raise ValueError("Choose the project and workflow.")
        with workspace.db() as c:
            workflow_scope(c, project, workflow)
            return snapshot(workspace, c, project, workflow)

    reviewed = validate_action(body, action)
    canonical = {**body, **({"schedules": reviewed} if action == "rollback" else {})}
    fingerprint = json.dumps([action, canonical], sort_keys=True, separators=(",", ":"))
    with workspace.db() as c:
        workflow_scope(c, project, workflow)
        prior = row(c, """SELECT fingerprint,receipt FROM workflow_lifecycle_actions
                           WHERE project=? AND actor=? AND request=?""", project, actor, body["request"])
        if prior is not None:
            if prior["fingerprint"] != fingerprint:
                raise LifecycleConflict("That request identifier belongs to a different lifecycle action.")
            return json.loads(prior["receipt"])
        state = metadata(c, workflow)
        if state["generation"] != body["expectedGeneration"]:
            raise LifecycleConflict("The workflow lifecycle changed. Reload and review before confirming.")
        receipt = {"action": action, "project": project, "workflow": workflow, "actor": actor,
                   "at": time.time(), "fromVersion": state["activeVersion"], "request": body["request"]}
        active = None
        archived = False
        paused = True
        if action == "archive":
            if state["archived"]:
                raise LifecycleConflict("This workflow is already archived.")
            receipt.update(archive(workspace, c, project, workflow))
            archived = True
        elif action == "recover":
            if not state["archived"]:
                raise LifecycleConflict("This workflow is already in the draft library.")
        else:
            if state["archived"]:
                raise LifecycleConflict("Recover the archived draft before selecting a release.")
            active = body["version"]
            if state["activeVersion"] == active:
                raise LifecycleConflict("This is already the active release.")
            graph = target_version(workspace, c, workflow, active)
            receipt["schedules"] = rebind_schedules(workspace, c, project, workflow, active, reviewed, graph) if reviewed else []
            paused = False
        generation = store_state(c, workflow, state, active=active, archived=archived, paused=paused)
        receipt.update(activeVersion=active, generation=generation, archived=archived, livePaused=paused)
        c.execute("INSERT INTO workflow_lifecycle_actions VALUES(?,?,?,?,?,?)",
                  (project, workflow, actor, body["request"], fingerprint, json.dumps(receipt, separators=(",", ":"))))
        return receipt
