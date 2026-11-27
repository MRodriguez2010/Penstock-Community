"""Immutable workflow editions, review gates, and durable serial run cursors."""
import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import agent_schema
import blocks
import portable_lookup
import graph_values
import ops_events
import structured_start
import workflow_lifecycle
from resources import DEFAULT_PROJECT, ProjectData
from triggers import Triggers
from project_resources import ident, name

from graph_cursor import advance
from graph_spec import digest
from workflows import bounded, plan_graph, step_value


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class Conflict(ValueError):
    pass


class Workspace:
    def __init__(self, root, connections):
        self.file = Path(root) / "workspace.sqlite3"
        self.connections = connections
        self.data = ProjectData(root)
        self.triggers = Triggers(self)
        with self.db() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS versions(
                    workflow TEXT, version INTEGER, name TEXT, plan TEXT, graph TEXT, digest TEXT, binding TEXT,
                    published INTEGER DEFAULT 0, PRIMARY KEY(workflow,version));
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, workflow TEXT, version INTEGER, mode TEXT,
                    request TEXT UNIQUE, input TEXT, state TEXT, cursor TEXT, context TEXT,
                    history TEXT, created REAL, FOREIGN KEY(workflow,version) REFERENCES versions);
                CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS workflow_projects(workflow TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects);
                CREATE TABLE IF NOT EXISTS run_roots(id TEXT PRIMARY KEY, root TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS run_root_lookup ON run_roots(root);
                CREATE TABLE IF NOT EXISTS container_bodies(workflow TEXT PRIMARY KEY,parent TEXT,version INTEGER,node TEXT,
                    UNIQUE(parent,version,node));
                CREATE TABLE IF NOT EXISTS table_pins(workflow TEXT,version INTEGER,node TEXT,snapshot TEXT,
                    PRIMARY KEY(workflow,version,node));
                CREATE TABLE IF NOT EXISTS run_details(id TEXT PRIMARY KEY REFERENCES runs, project TEXT NOT NULL,
                    preview TEXT, ancestors TEXT NOT NULL, lineage TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS run_permissions(id TEXT PRIMARY KEY REFERENCES runs, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS run_decisions(
                    id TEXT NOT NULL REFERENCES runs, node TEXT NOT NULL, at REAL NOT NULL,
                    actor TEXT NOT NULL, approved INTEGER NOT NULL, PRIMARY KEY(id,node));
            """)
            c.execute("INSERT OR IGNORE INTO projects VALUES(?,?)",(DEFAULT_PROJECT,"My project"))
            c.execute("INSERT OR IGNORE INTO workflow_projects SELECT DISTINCT workflow,? FROM versions",(DEFAULT_PROJECT,))
            workflow_lifecycle.setup(c)
            c.execute("""INSERT OR IGNORE INTO run_details SELECT id,project,CASE WHEN mode='test' THEN id END,'[]','[]'
                         FROM runs JOIN workflow_projects USING(workflow)""")
            for project in c.execute("SELECT id FROM projects"):
                self.data.prepare(project[0])
            c.execute("INSERT OR IGNORE INTO run_roots SELECT id,id FROM runs")
            ops_events.initialize_journal(c)
            ops_events.journal_interrupted(c)
            c.execute("UPDATE runs SET state='interrupted' WHERE state='running'")
            stale=[r[0] for r in c.execute("""SELECT DISTINCT preview FROM run_details WHERE preview IS NOT NULL
                    AND preview NOT IN (SELECT d.preview FROM run_details d JOIN runs USING(id)
                    WHERE d.preview IS NOT NULL AND state IN ('waiting','scheduled','running'))""")]
        for rid in stale:
            self.data.release(rid)

    @contextlib.contextmanager
    def db(self):
        c = sqlite3.connect(self.file, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        finally:
            c.close()

    def projects(self):
        with self.db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY rowid")]

    def require_project(self, project):
        with self.db() as c:
            if not c.execute("SELECT 1 FROM projects WHERE id=?",(ident(project),)).fetchone():
                raise ValueError("Unknown project.")
        return project

    def create_project(self, title):
        project = str(uuid.uuid4())
        title = name(title,"project name")
        with self.db() as c:
            self.data.prepare(project)
            c.execute("INSERT INTO projects VALUES(?,?)",(project,title))
        return {"id":project,"name":title}

    def save(self, plan, workflow=None, base_version=None, project=None, *, transaction=None):
        graph = plan_graph(plan, self.connections)
        with (contextlib.nullcontext(transaction) if transaction is not None else self.db()) as c:
            if workflow:
                if c.execute("SELECT 1 FROM container_bodies WHERE workflow=?",(workflow,)).fetchone():
                    raise ValueError("Edit the container body in its parent plan.")
                current_lifecycle = workflow_lifecycle.require_available(c,workflow)
                head = c.execute("SELECT max(version) FROM versions WHERE workflow=?", (workflow,)).fetchone()[0]
                if head is None or head != base_version:
                    raise Conflict("This draft changed. Reopen the latest version before saving.")
            else:
                workflow, head = str(uuid.uuid4()), 0
            if head:
                owning = c.execute("SELECT project FROM workflow_projects WHERE workflow=?",(workflow,)).fetchone()[0]
                if project and project != owning:
                    raise ValueError("Workflow belongs to another project.")
                project = owning
            else:
                project = project or DEFAULT_PROJECT
                if not c.execute("SELECT 1 FROM projects WHERE id=?",(project,)).fetchone():
                    raise ValueError("Unknown project.")
                c.execute("INSERT INTO workflow_projects VALUES(?,?)",(workflow,project))
            for node in graph["nodes"]:
                if node["type"] in portable_lookup.KINDS:
                    portable_lookup.selected(self.connections,node["type"],node["config"]["adapter"],project)
                if node["type"] in blocks.CONTAINERS:
                    body=self.save(node["config"]["body"],project=project,transaction=c)
                    c.execute("INSERT INTO container_bodies VALUES(?,?,?,?)",(body["workflow"],workflow,head+1,node["id"]))
                if node["type"]=="pinned-table":
                    cfg=node["config"]
                    with self.data.db(project) as resource_db:
                        snapshot=self.data.perform(resource_db,{"project":project,"id":workflow},node,cfg,None)
                    if snapshot["total"]>len(snapshot["rows"]):
                        raise ValueError("A pinned table supports up to 250 rows. Use a paged Table query for larger data.")
                    graph_values.bound(snapshot)
                    c.execute("INSERT INTO table_pins VALUES(?,?,?,?)",(workflow,head+1,node["id"],encode(snapshot)))
                if node["type"] in blocks.CHILDREN:
                    cfg = node["config"]
                    target = c.execute("""SELECT published FROM versions JOIN workflow_projects USING(workflow)
                                       WHERE workflow=? AND version=? AND project=?""",
                                       (cfg["workflow"],cfg["version"],project)).fetchone()
                    if not target or not target[0]:
                        raise ValueError("Choose a published child version within this project.")
                    workflow_lifecycle.require_available(c,cfg["workflow"],live=True)
            version = head + 1
            c.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?,0)",
                      (workflow, version, plan["name"], encode(plan), encode(graph), digest(graph), self.connections.binding(graph)))
            if head:
                workflow_lifecycle.edited(c,workflow,current_lifecycle)
        if transaction is not None:
            return {"workflow":workflow,"version":version}
        return self.version(workflow, version)

    def version(self, workflow, version):
        with self.db() as c:
            row = c.execute("SELECT v.*,p.project FROM versions v JOIN workflow_projects p USING(workflow) WHERE workflow=? AND version=?", (workflow,version)).fetchone()
            lifecycle = workflow_lifecycle.metadata(c,workflow) if row is not None else {}
        if row is None:
            raise ValueError("Unknown workflow version.")
        return {**dict(row), **lifecycle, "graph": json.loads(row["graph"]), "plan": json.loads(row["plan"])}

    def catalog(self, project=DEFAULT_PROJECT):
        self.require_project(project)
        with self.db() as c:
            versions = c.execute("SELECT workflow,version,name,published FROM versions v JOIN workflow_projects USING(workflow) WHERE project=? AND workflow NOT IN (SELECT workflow FROM container_bodies) ORDER BY v.rowid DESC",(project,)).fetchall()
            workflows = {item["workflow"] for item in versions}
            states = {workflow:workflow_lifecycle.metadata(c,workflow) for workflow in workflows}
            runs = c.execute("SELECT id,workflow,version,mode,state FROM runs JOIN workflow_projects USING(workflow) WHERE project=? ORDER BY created DESC LIMIT 60",(project,)).fetchall()
        return {"project":project, "projects":self.projects(), "blocks":blocks.catalog(),
                "versions": [{**dict(item),**states[item["workflow"]]} for item in versions], "runs": [dict(x) for x in runs],
                **self.connections.catalog(), "lookupAdapters":portable_lookup.catalog(self.connections,project)}

    def check_binding(self, version):
        if self.connections.binding(version["graph"]) != version["binding"]:
            raise Conflict("A selected connection or skill changed. Save and test a new version.")

    def publish(self, workflow, version):
        self.check_binding(self.version(workflow, version))
        with self.db() as c:
            state = workflow_lifecycle.require_available(c,workflow)
            if c.execute("SELECT 1 FROM container_bodies WHERE workflow=?",(workflow,)).fetchone():
                raise ValueError("Publish the parent workflow to use its container body.")
            passed = c.execute("SELECT 1 FROM runs WHERE workflow=? AND version=? AND mode IN ('offline','test') AND state='finished'",
                               (workflow,version)).fetchone()
            if not passed:
                raise Conflict("Finish a test of this exact version before publishing it.")
            changed = c.execute("UPDATE versions SET published=1 WHERE workflow=? AND version=? AND published=0",
                                (workflow,version)).rowcount
            if changed:
                workflow_lifecycle.published(c,workflow,version,state)
        return self.version(workflow,version)

    def withdraw(self, workflow, version):
        with self.db() as c:
            lifecycle = workflow_lifecycle.require_available(c,workflow)
            changed = c.execute("UPDATE versions SET published=0 WHERE workflow=? AND version=? AND published=1",
                                (workflow,version)).rowcount
            if changed:
                workflow_lifecycle.withdrawn(c,workflow,version,lifecycle)

    def start(self, workflow, version, mode, value, request_id, *, lineage=(), ancestors=(), preview=None,
              root=None, permissions=None, active_release=False, trigger_delivery=False):
        if mode not in ("offline", "test", "live"):
            raise ValueError("Choose an offline preview, model test, or live run.")
        if type(active_release) is not bool or active_release and mode != "live":
            raise ValueError("Active release selection applies only to a live run.")
        bounded(request_id, "Request ID", 80)
        input_json = encode(value)
        if len(input_json.encode()) > 32768:
            raise ValueError("Input exceeds 32 KiB.")
        if workflow in ancestors or len(ancestors)>3:
            raise ValueError("Child workflow cycle or nesting limit reached.")
        v = self.version(workflow, version)
        self.check_binding(v)
        with self.db() as c:
            body=c.execute("SELECT parent FROM container_bodies WHERE workflow=?",(workflow,)).fetchone()
        if body and (not ancestors or ancestors[-1]!=body[0]):
            raise Conflict("Run this container through its parent.")
        if mode == "live" and not v["published"] and not body:
            raise Conflict("Publish this version before starting a live run.")
        plan_graph(v["plan"], self.connections)
        with self.db() as c:
            prior = c.execute("SELECT * FROM runs WHERE request=?", (request_id,)).fetchone()
            if prior:
                if (workflow,version,mode,input_json) != tuple(prior[k] for k in ("workflow","version","mode","input")):
                    raise Conflict("This request ID belongs to a different run.")
                previous_access = c.execute("SELECT value FROM run_permissions WHERE id=?", (prior["id"],)).fetchone()
                if (json.loads(previous_access[0]) if previous_access else None) != permissions:
                    raise Conflict("This request ID belongs to a run with different permissions.")
                rid = prior["id"]
            else:
                lifecycle = workflow_lifecycle.require_available(c,workflow,live=mode=="live")
                if active_release and lifecycle["activeVersion"] != version:
                    raise workflow_lifecycle.LifecycleConflict("The active release changed. Open the current release before running.")
                if mode=="live" and not body and not c.execute(
                    "SELECT 1 FROM versions WHERE workflow=? AND version=? AND published=1",(workflow,version)
                ).fetchone():
                    raise Conflict("Publish this version before starting a live run.")
                if trigger_delivery:
                    self.triggers.require_ready(v["project"],request_id,workflow,version)
                rid = str(uuid.uuid4())
                if root and c.execute("SELECT count(*) FROM run_roots WHERE root=?",(root,)).fetchone()[0]>=500:
                    raise ValueError("A run can admit up to 500 related workflow runs.")
                c.execute("INSERT INTO run_roots VALUES(?,?)",(rid,root or rid))
                start = next(n["id"] for n in v["graph"]["nodes"] if n["type"] == "start")
                c.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,'running',?,'{}','[]',?)",
                          (rid,workflow,version,mode,request_id,input_json,start,time.time()))
                c.execute("INSERT INTO run_details VALUES(?,?,?,?,?)",(rid,v["project"],(preview or rid) if mode!="live" else None,
                          encode(ancestors),encode(lineage)))
                if permissions is not None:
                    c.execute("INSERT INTO run_permissions VALUES(?,?)", (rid,encode(permissions)))
        if not prior:
            self.drive(rid)
        return self.run(rid)

    def run(self, rid):
        with self.db() as c:
            row = c.execute("""SELECT runs.*,project,preview,ancestors,lineage,root,p.value AS permissions
                             FROM runs JOIN run_details USING(id) JOIN run_roots USING(id)
                             LEFT JOIN run_permissions p USING(id) WHERE id=?""", (rid,)).fetchone()
            decisions = c.execute("SELECT node,at,actor,approved FROM run_decisions WHERE id=? ORDER BY at,node", (rid,)).fetchall()
        if row is None:
            raise ValueError("Unknown run.")
        result = dict(row)
        for field in ("input", "context", "history","ancestors","lineage"):
            result[field] = json.loads(result[field])
        result.pop("request")
        result["permissions"] = json.loads(result["permissions"]) if result["permissions"] else None
        result["decisions"] = [{**dict(d), "actor":json.loads(d["actor"]),"approved":bool(d["approved"])} for d in decisions]
        result["graph"] = self.version(row["workflow"], row["version"])["graph"]
        return result

    def record(self, rid, state, cursor, ctx, history):
        with self.db() as c:
            c.execute("UPDATE runs SET state=?,cursor=?,context=?,history=? WHERE id=?",
                      (state,cursor,encode(ctx),encode(history),rid))
            ops_events.journal_transition(c, rid, state, cursor)

        if state in ("finished","failed","declined","interrupted"):
            with self.db() as c:
                owner = c.execute("SELECT preview FROM run_details WHERE id=?",(rid,)).fetchone()
            if owner and owner[0]:
                with self.db() as c:
                    active=c.execute("""SELECT 1 FROM run_details JOIN runs USING(id)
                                        WHERE preview=? AND state IN ('waiting','running','scheduled') LIMIT 1""",(owner[0],)).fetchone()
                if not active:
                    self.data.release(owner[0])

    def decide(self, rid, node_id, approve, answer=None, *, actor=None):
        record = self.run(rid)
        self.check_binding(self.version(record["workflow"], record["version"]))
        node = next((n for n in record["graph"]["nodes"] if n["id"]==node_id),None)
        if node and node["type"]=="human-input" and approve:
            graph_values.bound(answer)
            if "schema" in node["config"]:
                agent_schema.validate(node["config"]["schema"],answer)
        if type(approve) is not bool:
            raise ValueError("A decision must be true or false.")
        with self.db() as c:
            workflow_lifecycle.require_available(c,record["workflow"],live=record["mode"]=="live")
            changed = c.execute("UPDATE runs SET state='running' WHERE id=? AND state='waiting' AND cursor=?",
                               (rid,node_id)).rowcount
            if not changed:
                raise Conflict("This step is no longer waiting for a decision.")
            if actor is None:
                from managed_access import OWNER
                actor = OWNER
            c.execute("INSERT INTO run_decisions VALUES(?,?,?,?,?)", (rid,node_id,time.time(),encode(actor),int(approve)))
        self.drive(rid, approve, answer, actor=actor)
        return self.run(rid)

    def wake(self):
        allowed = """NOT EXISTS (SELECT 1 FROM workflow_lifecycle w
            WHERE w.workflow=runs.workflow AND (w.archived=1 OR (w.live_paused=1 AND runs.mode='live')))"""
        with self.db() as c:
            waiting = list(c.execute(f"SELECT id,context FROM runs WHERE state='scheduled' AND {allowed} ORDER BY created LIMIT 1000"))
        for r in waiting:
            if json.loads(r["context"]).get("wakeAt",0) > time.time():
                continue
            with self.db() as c:
                claimed = c.execute(f"UPDATE runs SET state='running' WHERE id=? AND state='scheduled' AND {allowed}",
                                    (r["id"],)).rowcount
            if claimed:
                self.drive(r["id"])

    def children(self, record, node, cfg, value, ctx):
        kind=node["type"]
        items=[value] if kind=="invoke-workflow" else value
        if kind in blocks.CONTAINERS:
            with self.db() as c:
                saved=c.execute("SELECT workflow FROM container_bodies WHERE parent=? AND version=? AND node=?",
                                (record["workflow"],record["version"],node["id"])).fetchone()
            cfg={**cfg,"workflow":saved[0],"version":1}
            collection=list(range(cfg["count"])) if "count" in cfg else cfg.get("items",value)
            if isinstance(collection,dict):
                collection=list(collection.items())
            if not isinstance(collection,list) or len(collection)>100:
                raise ValueError("Container collection must have up to 100 entries.")
            items=[{"index":i,"item":v} for i,v in enumerate(collection)]
        if not isinstance(items,list) or len(items)>100:
            raise ValueError("Provide an array of up to 100 child inputs.")
        def start_child(pair):
            index,item=pair
            key=str(uuid.uuid5(uuid.UUID(record["id"]),f"{node['id']}/{index}"))
            return self.start(cfg["workflow"],cfg["version"],record["mode"],item,key,
                              ancestors=[*record["ancestors"],record["workflow"]],
                              lineage=record["lineage"],preview=record["preview"],root=record["root"],
                              permissions=record["permissions"])
        if kind in ("parallel-each","parallel-container") and not record["ancestors"]:
            with ThreadPoolExecutor(max_workers=4) as pool:
                children=list(pool.map(start_child,enumerate(items)))
        else:
            children=[]
            for pair in enumerate(items):
                child=start_child(pair)
                children.append(child)
                if child["state"]!="finished":
                    break
        ctx["childRuns"]=[child["id"] for child in children]
        if any(child["state"] in ("failed","interrupted","declined") for child in children):
            raise ValueError("A child workflow did not finish successfully. Inspect its receipt.")
        if any(child["state"]!="finished" for child in children):
            return False,None
        results=[child["context"].get("result") for child in children]
        return True,results[0] if kind=="invoke-workflow" else results

    def drive(self, rid, decision=None, answer=None, *, actor=None):
        record = self.run(rid)
        version = self.version(record["workflow"], record["version"])
        graph, ctx, history = version["graph"], record["context"], record["history"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        values = {entry["node"]: entry.get("output") for entry in history}
        cursor = record["cursor"]
        preview = record["mode"] != "live"
        permissions = record["permissions"]
        while cursor:
            node = nodes[cursor]
            kind, cfg = node["type"], node["config"]
            value = values.get(ctx.get("previousNode"), record["input"])
            entry = {"node": cursor, "type": kind, "at": time.time()}
            model_step = kind == "agent" or kind in blocks.MODELS
            service_step = kind == "service" or kind in blocks.SERVICES
            lookup_step = kind in portable_lookup.KINDS
            if model_step:
                route = self.connections.providers.get(cfg["provider"], {})
                entry.update(provider=cfg["provider"], model=route.get("model"), skill=cfg.get("skill"))
            if service_step:
                entry.update(service=cfg["service"], preview=preview)
            try:
                if model_step and (record["mode"] == "offline" or permissions is not None and not permissions["model"]):
                    raise ValueError("This step needs a model call. Start a model test with explicit consent.")
                bindings={"input":record["input"],"previous":value,"outputs":values,"variables":ctx.get("variables",{})}
                if kind in blocks.FIELDS or kind in blocks.RESOURCES:
                    cfg={key:graph_values.pin(v,bindings) for key,v in cfg.items()}
                if kind=="variables":
                    cfg["values"]={key:graph_values.pin(v,bindings) for key,v in cfg["values"].items()}
                if service_step and not preview and permissions is not None:
                    scope = "read" if blocks.external_read(kind, cfg, self.connections) else "write"
                    if not permissions[scope]:
                        raise ValueError(f"This run is not authorized for the external {scope} operation.")
                if kind in ("http","service"):
                    schema=self.connections.services[cfg["service"]].get("input_schema")
                    if schema:
                        agent_schema.validate(schema,value)
                deployment_change = kind=="deployments" and cfg["operation"]!="list"
                if (kind=="source-read" or lookup_step) and not preview and (permissions or {}).get("projectRead") is not True:
                    raise ValueError("Start this version with live source read permission.")
                if lookup_step:
                    portable_lookup.selected(self.connections,kind,cfg["adapter"],record["project"])
                    lookup_input = portable_lookup.request_fields(cfg)
                    entry.update(adapter=cfg["adapter"], operation=cfg["operation"], requestedLimit=lookup_input["limit"], preview=preview)
                if kind in ("review","service","human-input","source-read") or kind in blocks.SERVICES or deployment_change or lookup_step:
                    if decision is None:
                        if lookup_step:
                            lookup_action = portable_lookup.LABELS[cfg["operation"]]
                            question = ("Use sample results to " + lookup_action.lower() + "?" if preview else
                                        lookup_action + " using " + cfg["adapter"] + "?")
                        else:
                            question = (("Use sample items for " if preview else "Read up to five items from ")+cfg["source"]+" into this project?" if kind=="source-read" else
                                        cfg.get("question") or (f"{cfg.get('operation')} workflow {cfg.get('workflow')} version {cfg.get('version')}?" if deployment_change else None) or
                                        ("Preview delivery to " if preview else "Run external operation with ") + cfg["service"] + "?")
                        ctx["pending"] = {"node": cursor, "value": lookup_input if lookup_step else value,
                                          "question": question, "kind": kind, "preview": preview}
                        if kind in blocks.SERVICES or deployment_change:
                            ctx["pending"]["operation"] = cfg
                        if kind=="human-input":
                            ctx["pending"]["answerRequired"] = True
                        self.record(rid, "waiting", cursor, ctx, history)
                        return
                    ctx.pop("pending", None)
                    entry["decision"] = "approved" if decision else "declined"
                    entry["decisionBy"] = actor
                    if kind == "review":
                        port, result = entry["decision"], value
                    elif not decision:
                        entry.update(port="declined", output=value)
                        self.record(rid, "declined", None, ctx, history + [entry])
                        return
                    elif kind=="source-read":
                        from operations import execute_source
                        result={"source":cfg["source"],"items":cfg["sample"]} if preview else execute_source(self,record["project"],cfg)
                        port="success"
                    elif lookup_step:
                        result = portable_lookup.execute(self.connections,kind,cfg,record["project"],preview)
                        port = "success"
                    elif deployment_change:
                        target=self.version(cfg["workflow"],cfg["version"])
                        if target["project"]!=record["project"]:
                            raise ValueError("Select a workflow in this project.")
                        if record["mode"]=="live":
                            if cfg["operation"]=="publish":
                                self.publish(cfg["workflow"],cfg["version"])
                            else:
                                self.withdraw(cfg["workflow"],cfg["version"])
                        port,result="success",{"workflow":cfg["workflow"],"version":cfg["version"],"operation":cfg["operation"],
                                               "preview":preview}
                    elif kind=="human-input":
                        port,result="success",answer
                    elif kind in blocks.SERVICES:
                        port="success"
                        result=cfg.get("preview",value) if preview else blocks.deliver(kind,self.connections,cfg,value,rid+"-"+cursor)
                    else:
                        result = cfg.get("preview",value) if preview else self.connections.deliver(cfg["service"], value)
                        port = "success"
                    decision = None
                elif kind=="deployments":
                    port,result="success",[v for v in self.catalog(record["project"])["versions"] if v["workflow"]==cfg["workflow"]]
                elif kind=="vision":
                    port,result="success",blocks.vision(self.data,record,cfg,self.connections)
                elif kind=="condition" and "operator" in cfg:
                    left=graph_values.pin(cfg["left"],bindings)
                    right=graph_values.pin(cfg["right"],bindings)
                    op=cfg["operator"]
                    if op in ("equals","differs"):
                        equal=agent_schema.canonical(left)==agent_schema.canonical(right)
                        passed=equal if op=="equals" else not equal
                    elif op=="contains":
                        passed=(isinstance(left,(str,list,dict)) and right in left)
                    else:
                        import operator
                        if type(left) not in (float,int) or type(right) not in (float,int):
                            raise ValueError("Numeric comparison requires two numbers.")
                        passed=getattr(operator,op)(left,right) if op in ("gt","lt") else (left>=right if op=="gte" else left<=right)
                    port,result=("true" if passed else "false"),value
                elif kind=="start" and cfg:
                    result,entry["inputs"]=structured_start.prepare(cfg,value)
                    if cfg["runMetadata"]:
                        result["metadata"]={"run":rid,"workflow":record["workflow"],"version":record["version"],"mode":record["mode"]}
                    port="success"
                elif kind=="wait":
                    if "wakeAt" not in ctx:
                        ctx["wakeAt"]=time.time()+cfg["seconds"]
                        self.record(rid,"scheduled",cursor,ctx,history)
                        return
                    ctx.pop("wakeAt")
                    port,result="success",value
                elif kind in blocks.CHILDREN | blocks.CONTAINERS:
                    complete,result=self.children(record,node,cfg,value,ctx)
                    if not complete:
                        ctx["wakeAt"]=time.time()+1
                        self.record(rid,"scheduled",cursor,ctx,history)
                        return
                    entry["children"]=ctx.pop("childRuns")
                    ctx.pop("wakeAt",None)
                    port="success"
                elif kind=="pinned-table":
                    with self.db() as c:
                        saved=c.execute("SELECT snapshot FROM table_pins WHERE workflow=? AND version=? AND node=?",
                                        (record["workflow"],record["version"],cursor)).fetchone()
                    port,result="success",json.loads(saved["snapshot"])
                elif kind in blocks.RESOURCES:
                    result = self.data.block(record,node,cfg,value)
                    if kind == "verify-artifact":
                        port = "success" if result["verified"] else "error"
                        entry["verification"] = "passed" if result["verified"] else "failed"
                    else:
                        port = "success"
                elif kind in blocks.FIELDS:
                    port,result=blocks.execute(kind,cfg,value,self.connections,ctx)
                else:
                    port, result = step_value(node, value, self.connections)
                if len(encode(result).encode()) > 65536:
                    raise ValueError("Block output exceeds 64 KiB.")
                values[cursor] = result
                entry.update(port=port if kind != "finish" else None, output=result)
                ctx["previousNode"] = cursor
                ctx["previousSucceeded"] = True
                if kind == "finish":
                    ctx["result"] = result
                targets = [e["target"] for e in graph["edges"] if e["source"] == cursor and e["sourcePort"] == port]
                cursor, state = advance(graph, ctx, targets, "finished")
            except Exception as exc:
                entry["error"] = str(exc) if isinstance(exc, (ValueError, KeyError)) else "Step could not complete."
                self.record(rid, "failed", cursor, ctx, history + [entry])
                return
            history.append(entry)
            self.record(rid, state or "running", cursor, ctx, history)
