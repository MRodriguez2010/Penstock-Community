"""Add workspace-owned model profiles and instruction skills from the setup form."""
from contextlib import closing
from pathlib import Path
import ipaddress
import json
import re
import sqlite3
import threading
from urllib.parse import urlsplit

LOCK=threading.RLock()


def store(root):
    path=Path(root)/"studio-profiles.sqlite3"
    conn=sqlite3.connect(path,timeout=10)
    try:
        path.chmod(0o600)
        conn.execute("""CREATE TABLE IF NOT EXISTS setup_profiles(
            kind TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(kind,name))""")
    except Exception:
        conn.close()
        raise
    return conn


def load_profiles(connections):
    if not (Path(connections.root)/"studio-profiles.sqlite3").exists():
        return
    with LOCK, closing(store(connections.root)) as c:
        for kind,name,value in c.execute("SELECT kind,name,value FROM setup_profiles"):
            target=connections.providers if kind=="provider" else connections.skills
            if name in target:
                raise ValueError(f"Choose distinct names for the workspace configuration and the saved {kind} {name}.")
            target[name]=json.loads(value)


def add_profile(connections,kind,body):
    name=body.get("name")
    if not isinstance(name,str) or not re.fullmatch("[A-Za-z0-9_-]{1,64}",name):
        raise ValueError("Choose a short name using letters, digits, dashes or underscores.")
    if kind=="provider":
        entry={key:body.get(key) for key in ("protocol","url","model")}
        secret=body.get("keyEnv")
        if secret:
            if not isinstance(secret,str) or not re.fullmatch("PENSTOCK_PROFILE_[A-Z0-9_]{1,60}",secret):
                raise ValueError("Use an administrator-supplied variable starting with PENSTOCK_PROFILE_.")
            entry["key_env"]=secret
        if not isinstance(entry["url"],str) or len(entry["url"])>2000:
            raise ValueError("Enter the model’s full request URL.")
        connections.check_endpoint(name,entry,{"model","protocol"})
        url=urlsplit(entry["url"])
        if url.scheme!="https":
            try:
                loopback=ipaddress.ip_address(url.hostname).is_loopback
            except ValueError:
                loopback=False
            if not loopback:
                raise ValueError("Use HTTPS, or a numeric loopback address for a local model.")
        if entry["protocol"] not in ("ollama","chat-completions","anthropic-messages"):
            raise ValueError("Choose a supported model protocol.")
        if not isinstance(entry["model"],str) or not 1<=len(entry["model"].strip())<=180:
            raise ValueError("Enter the installed or hosted model ID.")
        value=entry
    else:
        value=body.get("instructions")
        if not isinstance(value,str) or not value.strip() or len(value.encode())>16384:
            raise ValueError("Write instructions of up to 16 KiB.")
    with LOCK, closing(store(connections.root)) as c, c:
        target=connections.providers if kind=="provider" else connections.skills
        if name in target or len(target)>=128:
            raise ValueError("Use a new profile name within the workspace limit of 128.")
        try:
            c.execute("INSERT INTO setup_profiles VALUES(?,?,?)",(kind,name,json.dumps(value)))
        except sqlite3.IntegrityError:
            raise ValueError("This profile name is already installed.") from None
        c.commit()
        updated={**target,name:value}
        if kind=="provider":
            connections.providers=updated
        else:
            connections.skills=updated
    return {"name":name,"kind":kind}
