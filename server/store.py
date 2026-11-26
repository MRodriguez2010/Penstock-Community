"""SQLite primitives shared by the resource and workflow stores."""
import contextlib
import sqlite3
import time
import uuid


def uid():
    return str(uuid.uuid4())


def now():
    return int(time.time())


def row(c, sql, *args):
    value = c.execute(sql, args).fetchone()
    return dict(value) if value is not None else None


def rows(c, sql, *args):
    return [dict(value) for value in c.execute(sql, args)]


@contextlib.contextmanager
def transaction(path):
    with contextlib.closing(sqlite3.connect(path, timeout=15)) as c:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.commit()
