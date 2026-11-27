"""Durable project schedules and event subscriptions pinned to published workflows."""
import hashlib
import hmac
import json
import secrets
import workers
import time
import uuid

import calendar_schedule
import graph_values
import typed_tables
import workflow_lifecycle
from graph_table_events import enqueue
from store import row, rows, uid


def occurrences(config, after, count=5):
    if set(config) == {"seconds"}:
        seconds = config["seconds"]
        if type(seconds) is not int or not 1 <= seconds <= 31536000:
            raise ValueError("Interval must be 1 second through 365 days.")
        return [after + seconds * index for index in range(1, count + 1)]
    return calendar_schedule.preview(config, after, count)["occurrences"]


class Triggers:
    def __init__(self, workspace):
        self.workspace = workspace

    def create(self, project, workflow, version, kind, config, payload):
        v = self.workspace.version(workflow, version)
        self.workspace.check_binding(v)
        if not v["published"] or v["project"] != project:
            raise ValueError("Select a published workflow version in this project.")
        graph_values.bound(payload)
        token = None
        due = None
        resource = None
        if not isinstance(config, dict):
            raise ValueError("Trigger configuration must be an object.")
        if kind == "schedule":
            due = occurrences(config, time.time(), 1)[0]
            config = {"timing": config, "input": payload}
        elif kind in ("rss","app-trigger"):
            fields = {"service","seconds"} | ({"idField","itemsPath"} if kind=="app-trigger" else set())
            if set(config)!=fields:
                raise ValueError("Supply the registered service, polling interval and item mapping.")
            service = self.workspace.connections.services.get(config["service"],{})
            if type(config["seconds"]) is not int or not 30<=config["seconds"]<=86400:
                raise ValueError("Poll every 30 seconds through 24 hours.")
            if kind=="rss" and service.get("protocol")!="rss":
                raise ValueError("Select a registered RSS service.")
            if kind=="app-trigger":
                if service.get("protocol","http")!="http" or service.get("method")!="GET":
                    raise ValueError("Select a registered HTTP GET service.")
                if not isinstance(config["idField"],str) or not config["idField"] or not isinstance(config["itemsPath"],list) or len(config["itemsPath"])>8 or not all(isinstance(v,str) for v in config["itemsPath"]):
                    raise ValueError("Supply an item ID field and an array of object keys leading to the item list.")
            config = {**config,"binding":self.service_binding(service)}
            due = time.time()
        elif kind == "table-event":
            if set(config) != {"tableId", "event"} or config["event"] not in ("insert","update","delete"):
                raise ValueError("Choose a project table and an insert, update or delete event.")
            resource = config["tableId"]
        elif kind == "workspace-event":
            actions = config.get("actions")
            if set(config) != {"actions"} or not isinstance(actions,list) or not actions or not all(
                isinstance(a,str) and a in set(typed_tables.CHANGES) | {"file-save","document-save"} for a in actions
            ):
                raise ValueError("Choose project resource actions to observe.")
        elif kind == "webhook" and not config:
            token = secrets.token_urlsafe(32)
        else:
            raise ValueError("Unknown trigger type or configuration.")
        key = uid()
        with self.workspace.db() as workflows, self.workspace.data.db(project) as c:
            workflow_lifecycle.require_available(workflows,workflow,live=True)
            if not workflows.execute(
                "SELECT 1 FROM versions WHERE workflow=? AND version=? AND published=1",(workflow,version)
            ).fetchone():
                raise ValueError("Select a published workflow version in this project.")
            if resource is not None:
                typed_tables.editable(c,project,resource)
            c.execute("INSERT INTO triggers VALUES(?,?,?,?,?,?,1,?,?,NULL)", (
                key,workflow,version,kind,json.dumps(config),resource,due,
                hashlib.sha256(token.encode()).hexdigest() if token else None))
            if kind in ("rss","app-trigger"):
                c.execute("INSERT INTO poll_state(trigger_id) VALUES(?)",(key,))
        result = {"id":key, "nextDue":due}
        if token:
            result.update(token=token,path=f"/hooks/{project}/{key}")
        return result

    def list(self, project):
        with self.workspace.data.db(project) as c:
            return {"triggers":rows(c,"SELECT id,workflow,version,kind,enabled,next_due,last_error,config FROM triggers ORDER BY rowid DESC"),
                    "deliveries":rows(c,"SELECT id,trigger_id,state,run,detail FROM deliveries ORDER BY rowid DESC LIMIT 50")}

    def enable(self, project, key, enabled):
        if type(enabled) is not bool:
            raise ValueError("Enabled must be true or false.")
        with self.workspace.db() as workflows, self.workspace.data.db(project) as c:
            t = row(c,"SELECT * FROM triggers WHERE id=?",key)
            if not t:
                raise ValueError("Unknown trigger.")
            if enabled:
                workflow_lifecycle.require_available(workflows,t["workflow"],live=True)
                if not workflows.execute(
                    "SELECT 1 FROM versions WHERE workflow=? AND version=? AND published=1",(t["workflow"],t["version"])
                ).fetchone():
                    raise ValueError("Select a published workflow version before enabling this trigger.")
            due = t["next_due"]
            if enabled and not t["enabled"] and t["kind"] == "schedule":
                due = occurrences(json.loads(t["config"])["timing"], time.time(), 1)[0]
            c.execute("UPDATE triggers SET enabled=?,next_due=?,last_error=NULL WHERE id=?",(enabled,due,key))
        return {"id":key,"enabled":enabled,"nextDue":due}

    def webhook(self, project, key, token, event_id, value):
        if not isinstance(event_id,str) or not 1 <= len(event_id) <= 128:
            raise ValueError("Supply an Idempotency-Key of 1 to 128 characters.")
        graph_values.bound(value)
        with self.workspace.db() as workflows, self.workspace.data.db(project) as c:
            t = row(c,"SELECT * FROM triggers WHERE id=? AND enabled=1 AND kind='webhook'",key)
            if not t or not hmac.compare_digest(t["token_hash"],hashlib.sha256(token.encode()).hexdigest()):
                raise PermissionError("Invalid webhook credentials.")
            workflow_lifecycle.require_available(workflows,t["workflow"],live=True)
            delivery = str(uuid.uuid5(uuid.UUID(key), event_id))
            prior = row(c,"SELECT payload FROM deliveries WHERE id=?",delivery)
            if prior and json.loads(prior["payload"]) != value:
                raise ValueError("Event ID already belongs to another payload.")
            enqueue(c,t,value,[],delivery)
            return {"delivery":delivery}

    @staticmethod
    def service_binding(service):
        return hashlib.sha256(json.dumps(service,sort_keys=True).encode()).hexdigest()

    def poll(self,project,at):
        with self.workspace.data.db(project) as c:
            due=rows(c,"SELECT * FROM triggers WHERE enabled=1 AND kind IN ('rss','app-trigger') AND next_due<=? ORDER BY next_due LIMIT 8",at)
        for t in due:
            cfg=json.loads(t["config"])
            service=self.workspace.connections.services.get(cfg["service"],{})
            if cfg["binding"]!=self.service_binding(service):
                with self.workspace.data.db(project) as c:
                    c.execute("UPDATE triggers SET enabled=0,last_error=? WHERE id=?",("Service registration changed. Create a new trigger.",t["id"]))
                continue
            try:
                if t["kind"]=="rss":
                    feed=workers.feed(self.workspace.connections,cfg["service"])
                    items=feed["items"]
                else:
                    items=workers.endpoint_json(self.workspace.connections,cfg["service"],None)
                    for field in cfg["itemsPath"]:
                        items=items[field]
                if not isinstance(items,list) or len(items)>10000:
                    raise ValueError("Expected a bounded list of feed items.")
                events=[]
                for item in items:
                    if not isinstance(item,dict):
                        raise ValueError("Expected feed item objects.")
                    if t["kind"]=="rss":
                        identity=item.get("guid") or item.get("link") or item.get("title","")+"|"+item.get("pubDate","")
                        value={"item":item,"feed":feed["feed"]}
                    else:
                        identity=item.get(cfg["idField"])
                        value=item
                    if type(identity) not in (str,int) or identity in ("","|"):
                        raise ValueError("Each feed item needs a stable ID.")
                    events.append((hashlib.sha256(json.dumps(identity).encode()).hexdigest(),value))
                with self.workspace.data.db(project) as c:
                    latest=row(c,"SELECT enabled,next_due FROM triggers WHERE id=?",t["id"])
                    if not latest["enabled"] or latest["next_due"]!=t["next_due"]:
                        continue
                    state=row(c,"SELECT initialized FROM poll_state WHERE trigger_id=?",t["id"])
                    for identity,value in events:
                        new=c.execute("INSERT OR IGNORE INTO poll_seen VALUES(?,?)",(t["id"],identity)).rowcount
                        if new and state["initialized"]:
                            enqueue(c,t,value,[],str(uuid.uuid5(uuid.UUID(t["id"]),identity)))
                    c.execute("UPDATE poll_state SET initialized=1,failures=0 WHERE trigger_id=?",(t["id"],))
                    c.execute("UPDATE triggers SET next_due=?,last_error=NULL WHERE id=?",(at+cfg["seconds"],t["id"]))
            except (ValueError,KeyError,TypeError) as exc:
                with self.workspace.data.db(project) as c:
                    failures=row(c,"SELECT failures FROM poll_state WHERE trigger_id=?",t["id"])["failures"]+1
                    c.execute("UPDATE poll_state SET failures=? WHERE trigger_id=?",(failures,t["id"]))
                    c.execute("UPDATE triggers SET next_due=?,last_error=? WHERE id=? AND enabled=1",
                              (at+min(3600,cfg["seconds"]*2**min(failures,8)),str(exc)[:500],t["id"]))

    def require_ready(self, project, delivery, workflow, version):
        with self.workspace.data.db(project) as c:
            if not c.execute("""SELECT 1 FROM deliveries WHERE id=? AND workflow=? AND version=? AND state='ready'""",
                             (delivery,workflow,version)).fetchone():
                raise workflow_lifecycle.LifecycleConflict("This trigger delivery is no longer ready.")

    def tick(self, project, at):
        self.poll(project,at)
        with self.workspace.data.db(project) as c:
            for t in rows(c,"SELECT * FROM triggers WHERE enabled=1 AND kind='schedule' AND next_due<=?",at):
                cfg = json.loads(t["config"])
                due = t["next_due"]
                try:
                    timing = cfg["timing"]
                    if "seconds" in timing:
                        seconds = timing["seconds"]
                        next_due = due + (int((at-due)//seconds)+1)*seconds
                    else:
                        next_due = occurrences(timing,at,1)[0]
                except ValueError as exc:
                    c.execute("UPDATE triggers SET enabled=0,last_error=? WHERE id=?",(str(exc),t["id"]))
                    continue
                delivery = str(uuid.uuid5(uuid.UUID(t["id"]), str(due)))
                enqueue(c,t,cfg["input"],[],delivery)
                c.execute("UPDATE triggers SET next_due=? WHERE id=?",(next_due,t["id"]))
            ready = rows(c,"SELECT * FROM deliveries WHERE state='ready' ORDER BY rowid LIMIT 16")
        for item in ready:
            try:
                run = self.workspace.start(item["workflow"],item["version"],"live",json.loads(item["payload"]),
                                           item["id"],lineage=json.loads(item["lineage"]),trigger_delivery=True)
                state, detail, rid = "dispatched", None, run["id"]
            except (TypeError, ValueError) as exc:
                state, detail, rid = "failed", str(exc), None
            with self.workspace.data.db(project) as c:
                c.execute("UPDATE deliveries SET state=?,detail=?,run=? WHERE id=? AND state='ready'",
                          (state,detail,rid,item["id"]))

    def serve(self, stop):
        import logging
        while not stop.wait(1):
            try:
                for project in self.workspace.projects():
                    self.tick(project["id"], time.time())
                self.workspace.wake()
            except Exception:
                logging.exception("The scheduler could not complete a tick")
