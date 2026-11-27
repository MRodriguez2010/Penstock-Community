import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admin.backup import backup, restore
from penstock import Connections, initialize, single_writer, Workspace
from resources import DEFAULT_PROJECT
from starter_api import dispatch


class WorkspaceRecovery(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.original = self.base / "original"
        initialize(self.original)
        self.archive = self.base / "workspace.zip"
        self.target = self.base / "restored"

    def test_pending_decision_survives_restore(self):
        workspace = Workspace(self.original, Connections(self.original))
        pending = dispatch(workspace, "starter-run", {
            "project": DEFAULT_PROJECT, "kind": "reviewed-intake", "source": "sample",
            "version": 1, "request": str(uuid4()), "input": {},
        })
        with single_writer(self.original), self.assertRaises(OSError):
            backup(self.original, self.archive)
        count = backup(self.original, self.archive)
        self.assertEqual(restore(self.archive, self.target), count)
        for key in ("owner.key", "connections.toml"):
            self.assertEqual((self.original / key).read_bytes(), (self.target / key).read_bytes())
        if os.name != "nt":
            self.assertEqual(self.archive.stat().st_mode & 0o077, 0)
        with self.assertRaises(ValueError):
            restore(self.archive, self.target)
        with self.assertRaises(ValueError):
            backup(self.original, self.archive)
        resumed = Workspace(self.target, Connections(self.target))
        decision = {"project": DEFAULT_PROJECT, "id": pending["id"], "approve": True}
        first = dispatch(resumed, "starter-decide", decision)
        second = dispatch(resumed, "starter-decide", decision)
        self.assertEqual(first["effect"], second["effect"])
        table = resumed.data.action(DEFAULT_PROJECT, "table-read", {"id": first["effect"]["id"]})
        self.assertEqual(len(table["rows"]), 1)
        self.assertFalse(workspace.data.describe(DEFAULT_PROJECT)["tables"])

    def test_corrupt_archive_creates_no_destination(self):
        with ZipFile(self.archive, "w") as zipped:
            zipped.writestr("workspace/owner.key", "damaged")
            zipped.writestr("manifest.json", json.dumps({
                "format": 1, "sha256": {"owner.key": hashlib.sha256(b"intact").hexdigest()}
            }))
        with self.assertRaisesRegex(ValueError, "checksum"):
            restore(self.archive, self.target)
        self.assertFalse(self.target.exists())
