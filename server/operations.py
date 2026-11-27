"""Project-scoped capability checks, durable decisions inbox, and delivery receipts."""
import hashlib
import json
import time
from urllib.parse import urlencode, urlsplit

from office_connections import CAPABILITIES, notification_binding, notify, provisioned, read_items

OPS_ENDPOINTS = {"ops-catalog", "ops-configure", "ops-test", "ops-read", "ops-revoke",
                 "ops-notifications", "ops-notification-ack", "ops-improvements", "ops-test-change", "ops-profile-add", "ops-skill-add", "ops-oauth-start", "ops-oauth-finish", "ops-source-draft"}


from ops_events import NOTIFIERS, TITLES, inbox, prepare, record_event, rows


def dispatch(workspace, endpoint, d, *, admin=False, actor="owner", owner=False):
    project = workspace.require_project(d["project"])
    actor = str(actor or "owner")
    if endpoint in ("ops-profile-add","ops-skill-add"):
        if not owner:
            raise PermissionError("Only the workspace owner can install shared models and skills.")
        from setup_profiles import add_profile
        return add_profile(workspace.connections,"provider" if endpoint=="ops-profile-add" else "skill",d)
    config = provisioned(workspace.file.parent, project)
    if endpoint in ("ops-improvements", "ops-test-change"):
        from guided_improvements import improvements, test_change
        return (improvements(workspace, project) if endpoint == "ops-improvements" else
                test_change(workspace, project, d))
    kind = d.get("kind")
    if endpoint in ("ops-oauth-start","ops-oauth-finish"):
        if not admin or not isinstance(config.get(kind),dict):
            raise PermissionError("A project administrator must connect the registered account.")
        from office_oauth import begin, finish
        if endpoint == "ops-oauth-start":
            return begin(workspace,project,kind,actor,config[kind])
        result=finish(workspace,project,kind,actor,config[kind],d.get("code"),d["state"],browser=d.get("_browser"))
        if result.get("connected"):
            with workspace.data.db(project) as c:
                prepare(c)
                c.execute("""INSERT INTO ops_connections(project,kind,checked,enabled,enabled_at) VALUES(?,?,NULL,1,?) ON CONFLICT(project,kind)
                             DO UPDATE SET enabled=1,checked=NULL,enabled_at=excluded.enabled_at""",(project,kind,time.time()))
        return result
    signin={k:oauth_details(workspace,project,k,config) for k in CAPABILITIES} if endpoint=="ops-catalog" else {}
    destinations={k:current_destination(k,config.get(k)) for k in NOTIFIERS} if endpoint=="ops-catalog" else {}
    if endpoint=="ops-revoke" and admin and isinstance(config.get(kind),dict) and config[kind].get("oauth_client_id"):
        from office_oauth import revoke
        revoke(workspace,project,kind)
    with workspace.data.db(project) as c:
        prepare(c)
        if endpoint == "ops-notifications":
            return {"items":inbox(c,project,actor), "deliveries":rows(c,"""SELECT event,kind,status FROM ops_outbox
                    WHERE project=? ORDER BY rowid DESC LIMIT 100""",(project,)) if admin else []}
        if endpoint == "ops-notification-ack":
            if not c.execute("SELECT 1 FROM ops_events WHERE project=? AND id=?", (project,d["id"])).fetchone():
                raise ValueError("This notification is no longer available.")
            c.execute("INSERT OR IGNORE INTO ops_seen VALUES(?,?,?)",(project,d["id"],actor))
            return {"seen":True}
        saved = {r["kind"]:r for r in rows(c,"SELECT * FROM ops_connections WHERE project=?", (project,))}
        if endpoint == "ops-catalog":
            items=[]
            for k, (name, action, group) in CAPABILITIES.items():
                s=saved.get(k,{})
                enabled=k=="project-files" or bool(s.get("enabled"))
                reauthorize=False
                if k in NOTIFIERS:
                    approved=destinations[k] is not None and s.get("binding")==destinations[k]
                    reauthorize=enabled and not approved
                    enabled=enabled and approved
                if signin[k]:
                    enabled=enabled and signin[k]["signedIn"]
                items.append({"id":k,"name":name,"action":action,"group":group,
                              "available": k=="project-files" or isinstance(config.get(k),dict),
                              "enabled":enabled,"reauthorize":reauthorize,
                              **signin[k],
                              "checked":s.get("checked"),"delivery":k in NOTIFIERS})
            return {"items":items,"skills":workspace.connections.catalog().get("skills", []),
                    "providers":list(workspace.connections.providers),"canInstall":owner,
                    "canEnableNotifications":owner}
        if endpoint == "ops-source-draft":
            require_signed_in(workspace,project,kind,config)
            return source_draft(project,kind,config,saved)
        if not admin:
            raise PermissionError("Ask a project administrator to manage this connection.")
        if kind not in CAPABILITIES or (kind!="project-files" and not isinstance(config.get(kind),dict)):
            raise ValueError("Ask IT to provision this connection for this project.")
        if endpoint in ("ops-configure","ops-revoke"):
            if kind=="project-files":
                raise ValueError("This connection belongs to your local project.")
            enable = endpoint=="ops-configure"
            binding = None
            if enable and kind in NOTIFIERS:
                if not owner:
                    raise PermissionError("Only the workspace owner can approve a notification destination.")
                delivery_link(config[kind],project,"inbox")
                if d.get("confirmDelivery") is not True:
                    raise ValueError("Confirm delivery of future live notifications to this destination.")
                binding = notification_binding(kind, config[kind])
            if enable:
                require_signed_in(workspace,project,kind,config)
            c.execute("""INSERT INTO ops_connections(project,kind,checked,enabled,enabled_at,binding)
                       VALUES(?,?,NULL,?,?,?) ON CONFLICT(project,kind) DO UPDATE SET
                       enabled=excluded.enabled,checked=NULL,binding=excluded.binding,
                       enabled_at=CASE WHEN ops_connections.enabled=1 AND excluded.enabled=1
                                      AND ops_connections.binding IS excluded.binding
                                      THEN ops_connections.enabled_at ELSE excluded.enabled_at END""",
                      (project,kind,int(enable),time.time(),binding))
            if not enable:
                c.execute("UPDATE ops_outbox SET status='disabled' WHERE project=? AND kind=? AND status='queued'", (project,kind))
            elif binding:
                c.execute("""UPDATE ops_outbox SET status='destination changed'
                          WHERE project=? AND kind=? AND status='queued' AND binding IS NOT ?""", (project,kind,binding))
            return {"enabled":enable}
        if endpoint not in ("ops-test","ops-read") or kind in NOTIFIERS:
            raise ValueError("Choose a readable connection.")
        if d.get("consent") is not True:
            raise ValueError("Confirm this read before Penstock contacts the source.")
        if kind!="project-files" and not saved.get(kind,{}).get("enabled"):
            raise ValueError("Connect this service first.")
    local = workspace.data.describe(project) if kind=="project-files" else {}
    items = authorized_items(workspace,project,kind,config.get(kind,{}),local)
    stamp = int(time.time())
    with workspace.data.db(project) as c:
        c.execute("""INSERT INTO ops_connections(project,kind,checked,enabled,enabled_at) VALUES(?,?,?,1,0) ON CONFLICT(project,kind)
                    DO UPDATE SET checked=excluded.checked""", (project,kind,stamp))
    return {"checked":stamp, "count":len(items), "items":items if endpoint=="ops-read" else []}


def delivery_link(cfg, project, reference):
    base = str(cfg.get("studio_url","")).rstrip("/")
    url = urlsplit(base)
    if url.scheme!="https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Set the public HTTPS studio URL for this notification channel.")
    return base + "/?" + urlencode({"project":project,"notice":reference})


def current_destination(kind, cfg):
    if not isinstance(cfg,dict):
        return None
    try:
        return notification_binding(kind,cfg)
    except (KeyError,TypeError,ValueError):
        return None


def collect(workspace, project, batch_size=100):
    # Acknowledging happens after the durable inbox/outbox commit. Replaying a
    # delivered journal batch after a crash is safe because event IDs are stable.
    with workspace.db() as source:
        records=rows(source,"SELECT * FROM run_event_journal WHERE project=? ORDER BY sequence LIMIT ?",(project,batch_size))
    if not records:
        return 0
    with workspace.data.db(project) as c:
        prepare(c)
        for rec in records:
            record_event(c,project,rec["reference"],rec["version"],rec["state"],rec["title"],
                         deliver=bool(rec["deliver"]),occurred=rec["occurred"])
    with workspace.db() as source:
        source.execute("DELETE FROM run_event_journal WHERE project=? AND sequence<=?",(project,records[-1]["sequence"]))
    return len(records)


def recover_outbox(workspace, project):
    # Process interruption leaves the remote outcome unknown. Never retry that delivery automatically.
    with workspace.data.db(project) as c:
        prepare(c)
        c.execute("UPDATE ops_outbox SET status='delivery unconfirmed' WHERE project=? AND status='sending'",(project,))
        c.execute("UPDATE ops_outbox SET status='destination changed' WHERE project=? AND status='queued' AND binding IS NULL",(project,))


def deliver_next(workspace, project):
    with workspace.data.db(project) as c:
        prepare(c)
        item = c.execute("""SELECT o.event,o.kind,o.binding,e.state,e.reference,k.enabled,k.binding AS approved_binding
            FROM ops_outbox o JOIN ops_events e ON e.id=o.event AND e.project=o.project
            LEFT JOIN ops_connections k ON k.project=o.project AND k.kind=o.kind
            WHERE o.project=? AND o.status='queued' ORDER BY o.rowid LIMIT 1""", (project,)).fetchone()
        if not item:
            return
        key=(project,item["event"],item["kind"])
        if not item["enabled"] or not item["binding"] or item["binding"]!=item["approved_binding"]:
            status="disabled" if not item["enabled"] else "destination changed"
            c.execute("UPDATE ops_outbox SET status=? WHERE project=? AND event=? AND kind=?",(status,*key))
            return
        c.execute("UPDATE ops_outbox SET status='sending' WHERE project=? AND event=? AND kind=?",
                  key)
    try:
        cfg=provisioned(workspace.file.parent,project).get(item["kind"])
    except ValueError:
        cfg=None
    if current_destination(item["kind"],cfg)!=item["binding"]:
        status="destination changed"
    else:
        try:
            link=delivery_link(cfg,project,item["reference"])
            notify(item["kind"], cfg, TITLES[item["state"]], link)
            status="delivered"
        except Exception:
            # An external service may have accepted a message before disconnecting.
            status="delivery unconfirmed"
    with workspace.data.db(project) as c:
        c.execute("UPDATE ops_outbox SET status=? WHERE project=? AND event=? AND kind=? AND status='sending'",
                  (status,*key))


def serve(workspace, stop):
    import logging
    for project in workspace.projects():
        try:
            recover_outbox(workspace,project["id"])
        except Exception:
            logging.exception("Project notification recovery needs attention")
    while not stop.wait(10):
        for project in workspace.projects():
            if stop.is_set():
                return
            try:
                collect(workspace,project["id"])
                deliver_next(workspace,project["id"])
            except Exception:
                logging.exception("Project notification processing needs attention")


def oauth_details(workspace,project,kind,config):
    cfg=config.get(kind,{})
    if not isinstance(cfg,dict) or not cfg.get("oauth_client_id"):
        return {}
    from office_oauth import configured,status
    return {"signIn":configured(kind,cfg),"signedIn":bool(status(workspace,project,kind,cfg))}


def require_signed_in(workspace,project,kind,config):
    details=oauth_details(workspace,project,kind,config)
    if details and not details["signedIn"]:
        raise ValueError("Sign in to this project account before enabling or drafting a read.")


def authorized_items(workspace,project,kind,cfg,local):
    token=None
    if cfg.get("oauth_client_id"):
        from office_oauth import access_token
        token=access_token(workspace,project,kind,cfg)
        if not token:
            raise ValueError("Connect this account before reading from it.")
    return read_items(kind,cfg,local,token=token)


def source_binding(project,kind,cfg,enabled_at):
    data=json.dumps([project,kind,cfg,enabled_at],sort_keys=True,separators=(",",":"))
    return hashlib.sha256(data.encode()).hexdigest()


def source_draft(project,kind,config,saved):
    if kind not in CAPABILITIES or kind in NOTIFIERS:
        raise ValueError("Select a readable source.")
    if kind!="project-files" and (not config.get(kind) or not saved.get(kind,{}).get("enabled")):
        raise ValueError("An administrator needs to enable this source.")
    title,action,_=CAPABILITIES[kind]
    stamp=saved.get(kind,{}).get("enabled_at",0)
    binding=source_binding(project,kind,config.get(kind,{}),stamp)
    return {"plan":{"name":"Review "+title,"steps":[
        {"id":"start","type":"start","label":"Receive request","next":"read"},
        {"id":"read","type":"source-read","label":action,"config":{"source":kind,"binding":binding,
         "sample":["Example item for the preview"]},"next":"finish"},
        {"id":"finish","type":"finish","label":"Show the result"}]}}


def execute_source(workspace,project,cfg):
    kind=cfg["source"]
    config=provisioned(workspace.file.parent,project)
    require_signed_in(workspace,project,kind,config)
    with workspace.data.db(project) as c:
        prepare(c)
        saved={r["kind"]:r for r in rows(c,"SELECT * FROM ops_connections WHERE project=? AND kind=?",(project,kind))}
    current=source_draft(project,kind,config,saved)["plan"]["steps"][1]["config"]
    if cfg["binding"]!=current["binding"]:
        raise ValueError("This source changed. Add the current connection to a new draft and test it.")
    local=workspace.data.describe(project) if kind=="project-files" else {}
    items=authorized_items(workspace,project,kind,config.get(kind,{}),local)
    return {"source":CAPABILITIES[kind][0],"items":items}
