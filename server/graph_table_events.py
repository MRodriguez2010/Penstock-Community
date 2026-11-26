"""Capture table events in the same transaction as the row change."""
import json
from store import rows, uid, now


def enqueue(c, trigger, value, lineage, key=None):
    if trigger["id"] in lineage or len(lineage)>=8:
        return
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode())>32768:
        state, message, encoded = "failed", "Trigger input exceeds 32 KiB.", "null"
    else:
        state, message = "ready", None
    c.execute("""INSERT INTO deliveries(id,trigger_id,workflow,version,payload,lineage,state,detail,created)
                 VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING""",
              (key or uid(),trigger["id"],trigger["workflow"],trigger["version"],encoded,
               json.dumps([*lineage,trigger["id"]]),state,message,now()))


def capture(c, project, table, op, rid, current, previous=None, changed=None):
    triggers = rows(c,"SELECT * FROM triggers WHERE kind='table-event' AND enabled=1 AND resource=?",table["id"])
    if not triggers:
        return
    lineage = json.loads(c.execute("SELECT run_lineage()").fetchone()[0])
    cols = table["schema"]
    def named(values):
        if values is None:
            return None
        from graph_table_core import typed
        return {col["name"]:typed(col,x) for col,x in zip(cols,values)}
    names = [col["name"] for col in cols if col["id"] in (changed or [])]
    for trigger in triggers:
        cfg = json.loads(trigger["config"])
        if cfg["event"] == op and (not cfg.get("watch") or set(cfg["watch"]).intersection(changed or [])):
            enqueue(c,trigger,{"eventType":op,"tableId":table["id"],"tableName":table["name"],
                    "rowId":rid,"row":named(current),"previousRow":named(previous),"changedColumns":names},lineage)
