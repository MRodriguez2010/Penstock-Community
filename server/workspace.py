"""Immutable workflow editions, review gates, and durable serial run cursors."""
import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

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
        with self.db() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS versions(
                    workflow TEXT, version INTEGER, name TEXT, plan TEXT, graph TEXT, digest TEXT, binding TEXT,
                    published INTEGER DEFAULT 0, PRIMARY KEY(workflow,version));
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, workflow TEXT, version INTEGER, mode TEXT,
                    request TEXT UNIQUE, input TEXT, state TEXT, cursor TEXT, context TEXT,
                    history TEXT, created REAL, FOREIGN KEY(workflow,version) REFERENCES versions);
            """)
            c.execute("UPDATE runs SET state='interrupted' WHERE state='running'")

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

    def save(self, plan, workflow=None, base_version=None):
        graph = plan_graph(plan, self.connections)
        with self.db() as c:
            if workflow:
                head = c.execute("SELECT max(version) FROM versions WHERE workflow=?", (workflow,)).fetchone()[0]
                if head is None or head != base_version:
                    raise Conflict("This draft changed. Reopen the latest version before saving.")
            else:
                workflow, head = str(uuid.uuid4()), 0
            version = head + 1
            c.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?,0)",
                      (workflow, version, plan["name"], encode(plan), encode(graph), digest(graph), self.connections.binding(graph)))
        return self.version(workflow, version)

    def version(self, workflow, version):
        with self.db() as c:
            row = c.execute("SELECT * FROM versions WHERE workflow=? AND version=?", (workflow,version)).fetchone()
        if row is None:
            raise ValueError("Unknown workflow version.")
        return {**dict(row), "graph": json.loads(row["graph"]), "plan": json.loads(row["plan"])}

    def catalog(self):
        with self.db() as c:
            versions = c.execute("SELECT workflow,version,name,published FROM versions ORDER BY rowid DESC").fetchall()
            runs = c.execute("SELECT id,workflow,version,mode,state FROM runs ORDER BY created DESC LIMIT 60").fetchall()
        return {"versions": [dict(x) for x in versions], "runs": [dict(x) for x in runs],
                **self.connections.catalog()}

    def check_binding(self, version):
        if self.connections.binding(version["graph"]) != version["binding"]:
            raise Conflict("A selected connection or skill changed. Save and test a new version.")

    def publish(self, workflow, version):
        self.check_binding(self.version(workflow, version))
        with self.db() as c:
            passed = c.execute("SELECT 1 FROM runs WHERE workflow=? AND version=? AND mode='test' AND state='finished'",
                               (workflow,version)).fetchone()
            if not passed:
                raise Conflict("Finish a test of this exact version before publishing it.")
            c.execute("UPDATE versions SET published=1 WHERE workflow=? AND version=?", (workflow,version))
        return self.version(workflow,version)

    def start(self, workflow, version, mode, value, request_id):
        if mode not in ("test", "live"):
            raise ValueError("Choose test or live.")
        bounded(request_id, "Request ID", 80)
        input_json = encode(value)
        if len(input_json.encode()) > 32768:
            raise ValueError("Input exceeds 32 KiB.")
        v = self.version(workflow, version)
        self.check_binding(v)
        if mode == "live" and not v["published"]:
            raise Conflict("Publish this version before starting a live run.")
        plan_graph(v["plan"], self.connections)
        with self.db() as c:
            prior = c.execute("SELECT * FROM runs WHERE request=?", (request_id,)).fetchone()
            if prior:
                if (workflow,version,mode,input_json) != tuple(prior[k] for k in ("workflow","version","mode","input")):
                    raise Conflict("This request ID belongs to a different run.")
                rid = prior["id"]
            else:
                rid = str(uuid.uuid4())
                start = next(n["id"] for n in v["graph"]["nodes"] if n["type"] == "start")
                c.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,'running',?,'{}','[]',?)",
                          (rid,workflow,version,mode,request_id,input_json,start,time.time()))
        if not prior:
            self.drive(rid)
        return self.run(rid)

    def run(self, rid):
        with self.db() as c:
            row = c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise ValueError("Unknown run.")
        result = dict(row)
        for field in ("input", "context", "history"):
            result[field] = json.loads(result[field])
        result.pop("request")
        result["graph"] = self.version(row["workflow"], row["version"])["graph"]
        return result

    def record(self, rid, state, cursor, ctx, history):
        with self.db() as c:
            c.execute("UPDATE runs SET state=?,cursor=?,context=?,history=? WHERE id=?",
                      (state,cursor,encode(ctx),encode(history),rid))

    def decide(self, rid, node_id, approve):
        record = self.run(rid)
        if approve:
            self.check_binding(self.version(record["workflow"], record["version"]))
        if type(approve) is not bool:
            raise ValueError("A decision must be true or false.")
        with self.db() as c:
            changed = c.execute("UPDATE runs SET state='running' WHERE id=? AND state='waiting' AND cursor=?",
                               (rid,node_id)).rowcount
            if not changed:
                raise Conflict("This step is no longer waiting for a decision.")
        self.drive(rid, approve)
        return self.run(rid)

    def drive(self, rid, decision=None):
        record = self.run(rid)
        version = self.version(record["workflow"], record["version"])
        graph, ctx, history = version["graph"], record["context"], record["history"]
        nodes = {node["id"]: node for node in graph["nodes"]}
        values = {entry["node"]: entry.get("output") for entry in history}
        cursor = record["cursor"]
        while cursor:
            node = nodes[cursor]
            kind, cfg = node["type"], node["config"]
            value = values.get(ctx.get("previousNode"), record["input"])
            entry = {"node": cursor, "type": kind, "at": time.time()}
            if kind == "agent":
                route = self.connections.providers.get(cfg["provider"], {})
                entry.update(provider=cfg["provider"], model=route.get("model"), skill=cfg.get("skill"))
            if kind == "service":
                entry.update(service=cfg["service"], preview=record["mode"] == "test")
            try:
                if kind in ("review", "service"):
                    if decision is None:
                        ctx["pending"] = {"node":cursor, "value":value, "question":
                            cfg.get("question") or ("Preview delivery to " if record["mode"] == "test" else "Send this input to ") + cfg["service"] + "?"}
                        self.record(rid, "waiting", cursor, ctx, history)
                        return
                    ctx.pop("pending", None)
                    entry["decision"] = "approved" if decision else "declined"
                    if kind == "review":
                        port, result = entry["decision"], value
                    elif not decision:
                        entry.update(port="declined", output=value)
                        self.record(rid, "declined", None, ctx, history + [entry])
                        return
                    else:
                        result = value if record["mode"] == "test" else self.connections.deliver(cfg["service"], value)
                        port = "success"
                    decision = None
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
