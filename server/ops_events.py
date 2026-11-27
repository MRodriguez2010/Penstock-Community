"""Durable project inbox and a transactionally recorded run-transition journal."""
import hashlib
import time


NOTIFIERS = frozenset({"email","teams","slack"})
TITLES = {"waiting":"Approval needed", "finished":"Run completed", "declined":"Request declined",
          "changes-requested":"Changes requested", "interrupted":"Run interrupted", "failed":"Run needs attention"}


def prepare(c):
    # Individual DDL statements preserve a caller's existing SQLite transaction.
    for sql in (
        """CREATE TABLE IF NOT EXISTS ops_connections(project TEXT, kind TEXT, checked INTEGER,
           enabled INTEGER NOT NULL, enabled_at REAL NOT NULL, binding TEXT, PRIMARY KEY(project,kind))""",
        """CREATE TABLE IF NOT EXISTS ops_events(project TEXT, id TEXT, reference TEXT, version INTEGER,
           state TEXT, title TEXT, created INTEGER, PRIMARY KEY(project,id))""",
        """CREATE TABLE IF NOT EXISTS ops_seen(project TEXT,event TEXT,actor TEXT,PRIMARY KEY(project,event,actor))""",
        """CREATE TABLE IF NOT EXISTS ops_outbox(project TEXT,event TEXT,kind TEXT,status TEXT,binding TEXT,
           PRIMARY KEY(project,event,kind))""",
        """CREATE INDEX IF NOT EXISTS ops_events_recent ON ops_events(project,created DESC)""",
    ):
        c.execute(sql)
    connection_columns = {column[1] for column in c.execute("PRAGMA table_info(ops_connections)")}
    if "enabled_at" not in connection_columns:
        c.execute("ALTER TABLE ops_connections ADD COLUMN enabled_at REAL NOT NULL DEFAULT 0")
    if "binding" not in connection_columns:
        c.execute("ALTER TABLE ops_connections ADD COLUMN binding TEXT")
    if "binding" not in {column[1] for column in c.execute("PRAGMA table_info(ops_outbox)")}:
        c.execute("ALTER TABLE ops_outbox ADD COLUMN binding TEXT")


def event_id(reference, version, state):
    return hashlib.sha256(f"{reference}/{version}/{state}".encode()).hexdigest()[:32]


def record_event(c, project, reference, version, state, title, *, deliver=False, occurred=None):
    if state not in TITLES:
        return
    prepare(c)
    occurred = time.time() if occurred is None else occurred
    ident = event_id(reference, version, state)
    inserted = c.execute("INSERT OR IGNORE INTO ops_events VALUES(?,?,?,?,?,?,?)",
                         (project,ident,reference,version,state,str(title)[:140],int(occurred))).rowcount
    if not inserted or not deliver:
        return
    choices = ",".join("?" for _ in NOTIFIERS)
    c.execute(f"""INSERT OR IGNORE INTO ops_outbox(project,event,kind,status,binding)
                  SELECT project,?,kind,'queued',binding FROM ops_connections
                  WHERE project=? AND kind IN ({choices}) AND enabled=1 AND binding IS NOT NULL AND enabled_at<=?""",
              (ident,project,*sorted(NOTIFIERS),occurred))


def initialize_journal(c):
    exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_event_journal'").fetchone()
    if exists:
        return
    c.execute("""CREATE TABLE run_event_journal(
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT NOT NULL, reference TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL,
        title TEXT NOT NULL, occurred REAL NOT NULL, deliver INTEGER NOT NULL,
        UNIQUE(project,reference,version,state))""")
    c.execute("CREATE INDEX run_event_journal_by_project ON run_event_journal(project,sequence)")
    # Preserve existing receipts in the inbox during migration. Historical snapshots
    # lack reliable transition times, so they don't enter the outbound queue.
    c.execute("""INSERT INTO run_event_journal(project,reference,version,state,title,occurred,deliver)
        SELECT d.project,'run:'||r.id||CASE WHEN r.state='waiting' AND r.cursor IS NOT NULL THEN '/'||r.cursor ELSE '' END,
               r.version,r.state,v.name,r.created,0
        FROM runs r JOIN run_details d ON d.id=r.id JOIN versions v ON v.workflow=r.workflow AND v.version=r.version
        WHERE r.state IN ('waiting','finished','declined','interrupted','failed') ORDER BY r.created,r.rowid""")


def journal_interrupted(c):
    c.execute("""INSERT OR IGNORE INTO run_event_journal(project,reference,version,state,title,occurred,deliver)
        SELECT d.project,'run:'||r.id,r.version,'interrupted',v.name,?,r.mode='live'
        FROM runs r JOIN run_details d ON d.id=r.id JOIN versions v ON v.workflow=r.workflow AND v.version=r.version
        WHERE r.state='running'""", (time.time(),))


def journal_transition(c, run, state, cursor):
    if state not in TITLES:
        return
    reference = "run:" + run + ("/" + cursor if state == "waiting" and cursor else "")
    c.execute("""INSERT OR IGNORE INTO run_event_journal(project,reference,version,state,title,occurred,deliver)
        SELECT d.project,?,r.version,?,v.name,?,r.mode='live'
        FROM runs r JOIN run_details d ON d.id=r.id JOIN versions v ON v.workflow=r.workflow AND v.version=r.version
        WHERE r.id=?""", (reference,state,time.time(),run))


def rows(c, query, args):
    return [dict(row) for row in c.execute(query, args)]


def inbox(c, project, actor):
    return rows(c, """SELECT e.*,s.event IS NOT NULL AS seen FROM ops_events e LEFT JOIN ops_seen s
        ON e.project=s.project AND e.id=s.event AND s.actor=? WHERE e.project=?
        ORDER BY e.created DESC,e.rowid DESC LIMIT 100""", (actor,project))
