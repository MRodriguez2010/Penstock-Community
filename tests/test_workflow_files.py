from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from penstock import Connections, Workspace, initialize
from resources import DEFAULT_PROJECT


class ProjectWorkflowFiles(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        initialize(self.root)
        self.workspace = Workspace(self.root, Connections(self.root))
        self.first_body = "Original exact report content"
        self.first = self.save("Résumé.txt", self.first_body)
        self.second = self.save("Résumé.txt", "Newer confidential content")
        self.other_project = self.workspace.create_project("Separate")["id"]
        self.other = self.save("Other.txt", "Unrelated project secret", self.other_project)

    def save(self, filename, text, project=DEFAULT_PROJECT):
        return self.workspace.data.action(project, "file-save", {
            "key": str(uuid4()), "name": filename, "text": text,
        })

    def plan(self, kind, config):
        next_steps = "finish" if kind == "file" else {"success": "finish", "error": "repair"}
        return {"name": "Inspect saved files", "steps": [
            {"id": "start", "type": "start", "next": "file"},
            {"id": "file", "type": kind, "config": config, "next": next_steps},
            {"id": "finish", "type": "finish"},
            *([{"id": "repair", "type": "finish"}] if kind != "file" else []),
        ]}

    def run_offline(self, kind, config, value=None, project=DEFAULT_PROJECT):
        saved = self.workspace.save(self.plan(kind, config), project=project)
        with patch("connections.post", side_effect=AssertionError("Offline file work touched the network")) as remote:
            run = self.workspace.start(saved["workflow"], saved["version"], "offline", value, str(uuid4()))
        remote.assert_not_called()
        self.assertEqual(run["state"], "finished")
        return run

    def expectation(self):
        raw = self.first_body.encode()
        return {"expectedSha256": sha256(raw).hexdigest(), "expectedBytes": len(raw)}

    def test_list_search_is_project_scoped_casefolded_and_can_include_history(self):
        all_files = self.run_offline("file", {"operation": "list"})["context"]["result"]
        self.assertEqual([file["version"] for file in all_files], [2, 1])
        self.assertNotIn(self.other["id"], {file["id"] for file in all_files})
        results = self.run_offline("file", {
            "operation": "list", "search": {"ref": {"node": "$input"}}, "latestOnly": True, "limit": 1,
        }, "RÉSUMÉ")["context"]["result"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], self.second["id"])
        self.assertRegex(results[0]["digest"], r"^[a-f0-9]{64}$")
        self.assertGreater(results[0]["bytes"], 0)
        self.assertNotIn("text", results[0])

    def test_legacy_id_and_named_exact_version_read_same_bytes(self):
        by_id = self.run_offline("file", {"operation": "read", "id": self.first["id"]})["context"]["result"]
        named = self.run_offline("file", {"operation": "read", "name": "Résumé.txt", "version": 1})["context"]["result"]
        latest = self.run_offline("file", {"operation": "read", "name": "Résumé.txt"})["context"]["result"]
        self.assertEqual(by_id, named)
        self.assertEqual(named["text"], self.first_body)
        self.assertEqual(named["bytes"], self.expectation()["expectedBytes"])
        self.assertEqual(named["digest"], self.expectation()["expectedSha256"])
        self.assertEqual(latest["id"], self.second["id"])

    def test_exact_named_verification_keeps_historical_receipts_and_never_emits_body(self):
        cfg = {"name": "Résumé.txt", "version": 1, **self.expectation()}
        run = self.run_offline("verify-artifact", cfg)
        self.assertEqual(run["history"][1]["verification"], "passed")
        self.assertEqual(run["history"][-1]["node"], "finish")
        result = run["context"]["result"]
        self.assertEqual((result["id"], result["version"], result["verified"]), (self.first["id"], 1, True))
        self.assertNotIn(self.first_body, json.dumps(run))
        self.save("Résumé.txt", "A third private version")
        reopened = Workspace(self.root, Connections(self.root))
        self.assertEqual(reopened.run(run["id"])["context"]["result"], result)

    def test_wrong_version_hash_size_and_cross_project_ids_route_for_repair(self):
        scenarios = (
            ({"name": "Résumé.txt", "version": 5}, {}, "file-unavailable"),
            ({"id": self.other["id"]}, {}, "file-unavailable"),
            ({"id": self.first["id"]}, {"expectedSha256": "0" * 64}, "mismatch"),
            ({"id": self.first["id"]}, {"expectedBytes": 0}, "mismatch"),
        )
        for target, override, reason in scenarios:
            with self.subTest(target=target, override=override):
                run = self.run_offline("verify-artifact", {**target, **self.expectation(), **override})
                self.assertEqual(run["history"][1]["verification"], "failed")
                self.assertEqual(run["history"][-1]["node"], "repair")
                proof = run["context"]["result"]
                self.assertEqual(proof["reason"], reason)
                self.assertFalse(proof["verified"])
                if reason == "file-unavailable":
                    self.assertNotIn("name", proof)
                raw = json.dumps(run)
                self.assertNotIn(self.first_body, raw)
                self.assertNotIn("Unrelated project secret", raw)

    def test_integrity_check_rehashes_the_file_instead_of_trusting_metadata(self):
        with self.workspace.data.db(DEFAULT_PROJECT) as c:
            c.execute("UPDATE project_files SET data=? WHERE id=?",
                      (b"X" * self.expectation()["expectedBytes"], self.first["id"]))
        run = self.run_offline("verify-artifact", {"id": self.first["id"], **self.expectation()})
        self.assertEqual(run["context"]["result"]["reason"], "stored-checksum")
        self.assertNotIn("sha256", run["context"]["result"])

    def test_offline_file_save_has_metadata_and_keeps_preview_isolated(self):
        run = self.run_offline("file", {"operation": "save", "name": "Preview.txt"}, "A test write only")
        metadata = run["context"]["result"]
        self.assertEqual(metadata["bytes"], len("A test write only"))
        self.assertFalse(any(item["id"] == metadata["id"] for item in self.workspace.data.describe(DEFAULT_PROJECT)["files"]))

    def test_requires_literal_or_reference_to_exact_file_and_valid_expectation(self):
        for fields in (
            {"name": "Résumé.txt"}, {"name": "../Résumé.txt", "version": 1},
            {"id": self.first["id"], "expectedSha256": "invalid"},
            {"id": self.first["id"], "expectedBytes": -1},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.workspace.save(self.plan("verify-artifact", {**self.expectation(), **fields}))
