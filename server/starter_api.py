"""Local starter versions, run receipts, and transactional approval actions."""
import json
import time
import uuid

import starter_recipes as recipes

TABLE_ENDPOINTS = frozenset({"starter-table-list", "starter-table-input"})
STARTER_ENDPOINTS = frozenset({
    "starter-catalog", "starter-save", "starter-run", "starter-read", "starter-decide",
    "starter-request-changes",
}) | TABLE_ENDPOINTS
STATUS_WAITING = "awaiting-review"
STATUS_CHANGES = "changes-requested"
RECIPE_VERSION = 1


class StarterConflict(ValueError):
    pass


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS starter_versions (
        project TEXT NOT NULL, kind TEXT NOT NULL, version INTEGER NOT NULL, recipe_version INTEGER NOT NULL,
        settings TEXT NOT NULL, PRIMARY KEY(project, kind, version))""",
    """CREATE TABLE IF NOT EXISTS starter_runs (
        project TEXT NOT NULL, id TEXT NOT NULL, request TEXT NOT NULL, kind TEXT NOT NULL,
        version INTEGER NOT NULL, source TEXT NOT NULL, input TEXT NOT NULL, output TEXT NOT NULL,
        status TEXT NOT NULL, history TEXT NOT NULL, effect TEXT,
        started_by TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        PRIMARY KEY(project,id), UNIQUE(project,request),
        FOREIGN KEY(project,kind,version) REFERENCES starter_versions(project,kind,version))""",
    """CREATE TABLE IF NOT EXISTS starter_destinations (
        project TEXT NOT NULL, kind TEXT NOT NULL, resource TEXT NOT NULL, PRIMARY KEY(project,kind))""",
    """CREATE TABLE IF NOT EXISTS starter_revisions (
        project TEXT NOT NULL, run_id TEXT NOT NULL, parent_id TEXT NOT NULL,
        PRIMARY KEY(project,run_id), UNIQUE(project,parent_id),
        FOREIGN KEY(project,run_id) REFERENCES starter_runs(project,id),
        FOREIGN KEY(project,parent_id) REFERENCES starter_runs(project,id))""",
    "CREATE INDEX IF NOT EXISTS starter_runs_recent ON starter_runs(project,updated_at DESC)",
)


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


def identity(value, label):
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise ValueError(f"{label} is missing or invalid.") from None


def time_now():
    return time.time_ns() // 1_000_000


def event(step, state, at, actor=None):
    record = {"step": step, "state": state, "at": at}
    if actor is not None:
        record["actor"] = actor
    return record


def version_for(c, project, kind, version=None):
    sql = "SELECT * FROM starter_versions WHERE project=? AND kind=?"
    if version is None:
        v = c.execute(sql + " ORDER BY version DESC LIMIT 1", (project, kind)).fetchone()
    else:
        if type(version) is not int or version < 1:
            raise ValueError("Choose a workflow version.")
        v = c.execute(sql + " AND version=?", (project, kind, version)).fetchone()
    if v is None:
        if version not in (None, 1):
            raise ValueError("This workflow version is unavailable in the project.")
        return {"kind": kind, "version": 1, "recipeVersion": RECIPE_VERSION, "settings": dict(recipes.DEFAULTS[kind])}
    return {"kind": kind, "version": v["version"], "recipeVersion": v["recipe_version"],
            "settings": json.loads(v["settings"])}


def store_version(c, project, version):
    c.execute("INSERT OR IGNORE INTO starter_versions VALUES(?,?,?,?,?)",
              (project, version["kind"], version["version"], version["recipeVersion"], encode(version["settings"])))


def get_run(c, project, run_id):
    row = c.execute("SELECT * FROM starter_runs WHERE project=? AND id=?", (project, run_id)).fetchone()
    if row is None:
        raise ValueError("This starter run is unavailable in the project.")
    version = version_for(c, project, row["kind"], row["version"])
    history = json.loads(row["history"])
    parent = c.execute("SELECT parent_id FROM starter_revisions WHERE project=? AND run_id=?",
                       (project, run_id)).fetchone()
    successor = c.execute("SELECT run_id FROM starter_revisions WHERE project=? AND parent_id=?",
                          (project, run_id)).fetchone()
    return {
        **version, "project": project, "id": row["id"], "source": row["source"], "status": row["status"],
        "input": json.loads(row["input"]), "output": json.loads(row["output"]),
        "history": history, "effect": json.loads(row["effect"]) if row["effect"] else None,
        "startedBy": json.loads(row["started_by"]), "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        "revises": parent["parent_id"] if parent else None, "revisedBy": successor["run_id"] if successor else None,
        "changeRequest": next((entry for entry in reversed(history) if entry["state"] == STATUS_CHANGES), None),
    }


def get_catalog(c, project, can_edit, can_run, can_review):
    examples = recipes.samples()
    starters = [{**version_for(c, project, kind), "sample": examples[kind]} for kind in recipes.DEFAULTS]
    runs = []
    for r in c.execute("""SELECT r.id,r.kind,r.source,r.status,r.version,r.created_at,r.updated_at,v.settings,
                         json_extract(r.started_by,'$.displayName') AS submitted,
                         (SELECT json_extract(j.value,'$.actor.displayName') FROM json_each(r.history) j
                          WHERE json_extract(j.value,'$.step')='review' AND json_extract(j.value,'$.actor') IS NOT NULL
                          ORDER BY CAST(j.key AS INTEGER) DESC LIMIT 1) AS decided
                         FROM starter_runs r JOIN starter_versions v USING(project,kind,version)
                         WHERE r.project=? ORDER BY r.updated_at DESC,r.rowid DESC LIMIT 60""", (project,)):
        runs.append({"id": r["id"], "kind": r["kind"], "source": r["source"], "status": r["status"],
                     "version": r["version"], "createdAt": r["created_at"], "updatedAt": r["updated_at"],
                     "title": json.loads(r["settings"])["title"], "submittedBy": r["submitted"], "decidedBy": r["decided"]})
    return {"starters": starters, "runs": runs, "permissions": {"edit": can_edit, "run": can_run, "review": can_review}}


def save(c, project, kind, body):
    previous = version_for(c, project, kind)
    if type(body.get("baseVersion")) is not int or previous["version"] != body["baseVersion"]:
        raise StarterConflict("This workflow has a newer version. Reopen it before saving.")
    new_settings = recipes.settings_for(kind, body.get("settings"))
    if new_settings == previous["settings"]:
        return previous
    store_version(c, project, previous)
    version = {**previous, "version": previous["version"] + 1, "settings": new_settings}
    store_version(c, project, version)
    return version


def start(c, project, kind, body, actor, record_event):
    version = version_for(c, project, kind, body.get("version"))
    if type(body.get("version")) is not int:
        raise ValueError("Choose the workflow version to run.")
    if version["recipeVersion"] != RECIPE_VERSION:
        raise ValueError("This version needs a matching starter runtime.")
    source = body.get("source")
    input_data, output = recipes.prepare(kind, body.get("input"), source, version["settings"])
    request = identity(body.get("request"), "Run request ID")
    revises = identity(body["revises"], "Prior receipt") if body.get("revises") is not None else None
    values = (kind, version["version"], source, encode(input_data))
    previous = c.execute("""SELECT r.id,r.kind,r.version,r.source,r.input,v.parent_id FROM starter_runs r
                           LEFT JOIN starter_revisions v ON v.project=r.project AND v.run_id=r.id
                           WHERE r.project=? AND r.request=?""", (project, request)).fetchone()
    if previous is not None:
        if (*values, revises) != tuple(previous[field] for field in ("kind", "version", "source", "input", "parent_id")):
            raise StarterConflict("This request ID already belongs to a different run.")
        return get_run(c, project, previous["id"])
    if revises:
        parent = get_run(c, project, revises)
        if parent["status"] != STATUS_CHANGES or parent["kind"] != kind:
            raise ValueError("Choose an earlier receipt with a change request for this starter.")
        if version["version"] < parent["version"]:
            raise ValueError("Use the same workflow version or a newer version for the revision.")
        if parent["revisedBy"]:
            raise StarterConflict("A new preview already exists. Open it from the earlier receipt.")
    store_version(c, project, version)
    now = time_now()
    history = [
        event("input", "completed", now, actor),
        event("prepare", "completed", now),
        event("review", "waiting", now),
    ]
    run_id = str(uuid.uuid4())
    c.execute("""INSERT INTO starter_runs
                 (project,id,request,kind,version,source,input,output,status,history,started_by,created_at,updated_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (project, run_id, request, *values, encode(output), STATUS_WAITING, encode(history), encode(actor), now, now))
    if revises:
        c.execute("INSERT INTO starter_revisions VALUES(?,?,?)", (project, run_id, revises))
    record_event(c, project, "starter:" + run_id, version["version"], "waiting", version["settings"]["title"],
                 deliver=False)
    return get_run(c, project, run_id)


def decide(c, project, body, actor, effects, record_event):
    approve = body.get("approve")
    if type(approve) is not bool:
        raise ValueError("Choose Approve or Decline.")
    run = get_run(c, project, identity(body.get("id"), "Run ID"))
    transition = "approved" if approve else "declined"
    if run["status"] != STATUS_WAITING:
        if run["status"] == transition:
            return run
        raise StarterConflict("This run already has a decision. Open its receipt to see the outcome.")
    now = time_now()
    if approve:
        c.execute("SAVEPOINT starter_effect")
        try:
            effect = effects.publish(c, project, run["kind"], run)
            if not isinstance(effect, dict) or effect.get("type") not in ("file", "table") or not effect.get("id"):
                raise ValueError("The project store could not confirm the approved change.")
        except (StarterConflict, ValueError) as error:
            c.execute("ROLLBACK TO starter_effect")
            c.execute("RELEASE starter_effect")
            problem = {**event("publish", "failed", now, actor), "message": str(error)}
            c.execute("UPDATE starter_runs SET history=?,updated_at=? WHERE project=? AND id=?",
                      (encode([*run["history"], problem]), now, project, run["id"]))
            record_event(c, project, "starter:" + run["id"], run["version"], "failed",
                         run["settings"]["title"], deliver=False)
            return get_run(c, project, run["id"])
        c.execute("RELEASE starter_effect")
    else:
        effect = None
    history = [*run["history"], event("review", transition, now, actor),
               event("publish", "completed" if approve else "skipped", now)]
    c.execute("UPDATE starter_runs SET status=?,history=?,effect=?,updated_at=? WHERE project=? AND id=?",
              (transition, encode(history), encode(effect) if effect else None, now, project, run["id"]))
    record_event(c, project, "starter:" + run["id"], run["version"], "finished" if approve else "declined",
                 run["settings"]["title"], deliver=False)
    return get_run(c, project, run["id"])


def request_changes(c, project, body, actor, record_event):
    run = get_run(c, project, identity(body.get("id"), "Run ID"))
    reason = recipes.string(body.get("reason"), "Reason for the requested change", 1000)
    if run["status"] == STATUS_CHANGES and run["changeRequest"]["reason"] == reason:
        return run
    if run["status"] != STATUS_WAITING:
        raise StarterConflict("This run already has a decision. Open its receipt to see the outcome.")
    now = time_now()
    feedback = {**event("review", STATUS_CHANGES, now, actor), "reason": reason}
    c.execute("UPDATE starter_runs SET status=?,history=?,updated_at=? WHERE project=? AND id=?",
              (STATUS_CHANGES, encode([*run["history"], feedback]), now, project, run["id"]))
    record_event(c, project, "starter:" + run["id"], run["version"], STATUS_CHANGES,
                 run["settings"]["title"], deliver=False)
    return get_run(c, project, run["id"])


def dispatch(workspace, endpoint, body, *, can_edit=True, can_review=True, can_run=None, actor=None,
             effects=None, record_event=None):
    """Handle an authenticated, project-authorized request from the edition host."""
    if endpoint not in STARTER_ENDPOINTS:
        raise ValueError("Unknown starter endpoint.")
    project = identity(body.get("project"), "Project")
    workspace.require_project(project)
    can_run = can_edit if can_run is None else can_run
    actor = actor or {"displayName": "Workspace owner", "role": "owner"}
    if endpoint == "starter-save" and not can_edit:
        raise PermissionError("Only project editors can save workflow settings.")
    if endpoint == "starter-run" and not can_run:
        raise PermissionError("You need run access to start this workflow.")
    if endpoint in TABLE_ENDPOINTS and not can_run:
        raise PermissionError("You need run access to load a table into the brief.")
    if endpoint in ("starter-decide", "starter-request-changes") and not can_review:
        raise PermissionError("Only project reviewers can make this decision.")
    kind = body.get("kind")
    if endpoint in ("starter-run", "starter-save") and (not isinstance(kind, str) or kind not in recipes.DEFAULTS):
        raise ValueError("Choose Weekly executive brief or Reviewed intake.")
    with workspace.data.db(project) as c:
        if endpoint in TABLE_ENDPOINTS:
            from starter_tables import list_tables, snapshot
            if endpoint == "starter-table-list":
                return list_tables(c, project)
            return snapshot(c, project, identity(body.get("table"), "Table"))
        for statement in SCHEMA:
            c.execute(statement)
        if endpoint == "starter-catalog":
            return get_catalog(c, project, can_edit, can_run, can_review)
        if endpoint == "starter-save":
            return save(c, project, kind, body)
        if endpoint == "starter-read":
            return get_run(c, project, identity(body.get("id"), "Run ID"))
        if record_event is None:
            from operations import record_event
        if endpoint == "starter-run":
            return start(c, project, kind, body, actor, record_event)
        if endpoint == "starter-request-changes":
            return request_changes(c, project, body, actor, record_event)
        if effects is None:
            import starter_resources as effects
        return decide(c, project, body, actor, effects, record_event)
