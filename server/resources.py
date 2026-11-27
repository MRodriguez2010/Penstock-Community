"""Project isolation, durable resource mutations, and disposable test data."""
from contextlib import closing, contextmanager
import base64
import json
from pathlib import Path
import sqlite3
import threading
import uuid

import graph_knowledge
import graph_table_core
import graph_values
import project_resources as res
import typed_tables as tables
import workflow_files
from graph_table_events import enqueue
from store import now, row, rows, transaction, uid

DEFAULT_PROJECT = str(uuid.uuid5(uuid.NAMESPACE_URL, "urn:penstock:default-project"))
SCHEMA = Path(__file__).with_name("resources.sql").read_text()


class ProjectData:
    def __init__(self, root):
        self.root = Path(root) / "projects"
        self.previews = Path(root) / "previews"
        self.root.mkdir(exist_ok=True)
        self.previews.mkdir(exist_ok=True)
        self.lock = threading.RLock()

    def file(self, project):
        return self.root / (res.ident(project) + ".sqlite3")

    def prepare(self, project):
        with self.lock, closing(sqlite3.connect(self.file(project))) as c:
            c.executescript(SCHEMA)

    def snapshot(self, project, test_id):
        target = self.previews / (res.ident(test_id) + ".sqlite3")
        with self.lock:
            if not target.exists():
                temp = target.with_suffix(".new")
                try:
                    with closing(sqlite3.connect(self.file(project))) as source, closing(sqlite3.connect(temp)) as dest:
                        source.backup(dest)
                        # Test writes exercise the same store without delivering external events.
                        dest.execute("UPDATE triggers SET enabled=0")
                        dest.commit()
                    temp.replace(target)
                finally:
                    temp.unlink(missing_ok=True)
        return target

    def release(self, test_id):
        with self.lock:
            (self.previews / (res.ident(test_id) + ".sqlite3")).unlink(missing_ok=True)

    @contextmanager
    def db(self, project, test_id=None, lineage=()):
        file = self.snapshot(project, test_id) if test_id else self.file(project)
        with transaction(file) as c:
            c.create_function("run_lineage",0,lambda:json.dumps(lineage))
            yield c

    def describe(self, project):
        with self.db(project) as c:
            return {"tables":rows(c,"SELECT t.id,t.name,s.revision FROM project_tables t JOIN typed_schemas s ON s.table_id=t.id ORDER BY t.name"),
                    "documents":res.documents(c,project), "files":rows(c,"SELECT id,name,version FROM project_files ORDER BY created DESC LIMIT 100"),
                    "callbacks":rows(c,"SELECT * FROM callback_messages ORDER BY rowid DESC LIMIT 50"),
                    "logs":rows(c,"SELECT source,title,detail,at FROM resource_log ORDER BY id DESC LIMIT 50")}

    def action(self, project, action, d):
        with self.db(project) as c:
            if action == "table-read":
                limit,offset=res.page(d)
                return tables.data(c,project,res.ident(d["id"]),limit,offset,d.get("view"),d.get("filterView"))
            if action == "table-export":
                name,raw=tables.export(c,project,res.ident(d["id"]),d.get("view"))
                return {"name":name,"csv":raw.decode()}
            if action == "file-read":
                meta,raw=res.file_data(c,project,res.ident(d["id"]))
                if res.media_type(meta["name"]):
                    return {**meta,"base64":base64.b64encode(raw).decode("ascii")}
                return {**meta,"text":raw.decode("utf-8")}
            if action == "document-read":
                return res.document(c,project,res.ident(d["id"]))
            writes = {**tables.CHANGES, "file-save":res.save_file,"document-save":res.save_document}
            if action not in writes:
                raise ValueError("Unknown project action.")
            key,fingerprint,prior=res.cached(c,project,d,action)
            if prior is not None:
                return prior
            result=writes[action](c,project,project,d)
            notify_workspace(c,action,result,[])
            return res.remember(c,project,key,action,fingerprint,result)

    def block(self, job, node, cfg, value):
        project=job["project"]
        with self.db(project, job.get("preview"), job.get("lineage",[])) as c:
            old=row(c,"SELECT result FROM block_effects WHERE run=? AND node=?",job["id"],node["id"])
            if old:
                return json.loads(old["result"])
            result=self.perform(c,job,node,cfg,value)
            graph_values.bound(result)
            c.execute("INSERT INTO block_effects VALUES(?,?,?)",(job["id"],node["id"],json.dumps(result)))
            return result

    def perform(self,c,job,node,cfg,value):
        kind=node["type"]
        project=job["project"]
        owner={"project":project,"chat":project,"run":job["id"]}
        op=cfg.get("operation")
        if kind in ("table","knowledge"):
            module=graph_table_core if kind=="table" else graph_knowledge
            params=dict(cfg)
            if kind=="knowledge":params.setdefault("knowledgeBaseId",project)
            allowed=module.active(op)
            required=module.REQUIRED[op] | ({"tableId"} if kind=="table" else {"knowledgeBaseId"})
            if set(params)-{"operation"}-allowed or not required<=params.keys():
                raise ValueError("Missing or unexpected operation fields.")
            validate=module.validate_value if kind=="table" else module.validate
            for k,v in params.items():
                if k!="operation":validate(k,v)
            return module.perform(c,owner,params)["values"]
        if kind=="pinned-table":
            meta=tables.editable(c,project,cfg["tableId"])
            tables.expected(cfg["revision"],meta["revision"],"Pinned table")
            return {"columns":meta["columns"],"schema":meta["schema"],"revision":meta["revision"],"rows":tables.data(c,project,meta["id"],250,0)["rows"],
                    "total":meta["rowCount"]}
        if kind=="file":
            return workflow_files.execute_file(c,project,cfg,value)
        if kind=="verify-artifact":
            return workflow_files.verify(c,project,cfg)
        if kind=="memory":
            thread=cfg.get("conversation","default")
            res.bounded(thread,"conversation",256)
            if op=="add":
                content=cfg.get("content",value)
                role=cfg.get("role","user")
                if role not in ("user","assistant","system"):raise ValueError("Invalid memory role.")
                res.bounded(content,"message",16000)
                c.execute("INSERT INTO graph_memory_messages(project,conversation,role,content,created) VALUES(?,?,?,?,?)",
                          (project,thread,role,content,now()))
            if op=="delete":
                count=c.execute("DELETE FROM graph_memory_messages WHERE project=? AND conversation=?",(project,thread)).rowcount
                return {"deleted":count}
            return rows(c,"SELECT conversation,role,content,created FROM graph_memory_messages WHERE project=? AND (? OR conversation=?) ORDER BY sequence",
                        project,op=="list",thread)
        if kind=="callback":
            ident=uid()
            c.execute("INSERT INTO callback_messages VALUES(?,?,?,?,?)",(ident,job["id"],node["id"],json.dumps(value),now()))
            return {"callback":ident,"value":value}
        if kind=="step-log":
            if op=="read":
                return rows(c,"SELECT at,title,detail FROM resource_log ORDER BY id DESC LIMIT 50")
            res.audit(c,project,project,"Workflow",cfg.get("label",node["id"]),json.dumps(value,ensure_ascii=False)[:4000])
            return value
        raise ValueError("Unknown project block.")


def notify_workspace(c, action, result, lineage):
    for trigger in rows(c,"SELECT * FROM triggers WHERE kind='workspace-event' AND enabled=1"):
        if action in json.loads(trigger["config"])["actions"]:
            enqueue(c,trigger,{"action":action,"result":result},lineage)
