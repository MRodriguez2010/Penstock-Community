from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from penstock import Connections, Workspace, initialize
from blocks import catalog
from workflows import plan_graph


class PortableTransforms(unittest.TestCase):
    def setUp(self):
        scratch = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(scratch)
        initialize(root)
        self.connections = Connections(root)
        self.workspace = Workspace(root, self.connections)

    def run_local(self, kind, config, routes, items, request):
        self.assertIn(kind, catalog())
        plan = {"name": "Local transformation", "steps": [
            {"id": "start", "type": "start", "next": "transform"},
            {"id": "transform", "type": kind, "config": config, "next": routes},
            *({"id": name, "type": "finish"} for name in dict.fromkeys(routes.values())),
        ]}
        version = self.workspace.save(plan)
        with patch("connections.post", side_effect=AssertionError("Local transform touched the network")) as post:
            result = self.workspace.start(version["workflow"], version["version"], "offline", items, request)
        post.assert_not_called()
        self.assertEqual(result["state"], "finished")
        return result

    def test_groups_are_case_sensitive_and_keep_first_seen_order(self):
        data = {"records": [{"region": "N"}, {"region": "S"}, {"region": "N"}, {"region": "n"}]}
        transform = {"field": "region", "rows": {"ref": {"node": "$input", "path": ["records"]}}}
        run = self.run_local("group-records", transform,
                             {"success": "finish", "error": "invalid"}, data, "group")
        self.assertEqual(run["context"]["result"], {
            "rowCount": 4, "distinctCount": 3,
            "groups": [{"value": "N", "count": 2}, {"value": "S", "count": 1}, {"value": "n", "count": 1}],
        })
        self.assertEqual(run["history"][1]["port"], "success")

    def test_malformed_group_rows_take_the_explicit_error_edge(self):
        data = [{"region": "north"}, {"region": 123}]
        run = self.run_local("group-records", {"field": "region"},
                             {"success": "finish", "error": "invalid"}, data, "bad-group")
        self.assertEqual(run["history"][-1]["node"], "invalid")
        self.assertIn("Record 2", run["context"]["result"]["message"])

    def test_first_matching_branch_uses_stable_port_and_original_payload(self):
        data = {"ticket": {"severity": 5, "urgent": True}}
        cfg = {
            "value": {"ref": {"node": "$input", "path": ["ticket"]}},
            "branches": [
                {"id": "priority", "path": ["severity"], "operator": "gte", "right": 4},
                {"id": "urgent", "path": ["urgent"], "operator": "equals", "right": True},
            ],
        }
        run = self.run_local("ordered-condition", cfg,
                             {"priority": "first", "urgent": "second", "default": "otherwise", "error": "invalid"},
                             data, "order")
        self.assertEqual(run["history"][1]["port"], "priority")
        self.assertEqual(run["history"][-1]["node"], "first")
        self.assertEqual(run["context"]["result"], data)

    def test_default_and_incompatible_input_have_different_ports(self):
        cfg = {"branches": [
            {"id": "hit", "path": ["name"], "operator": "starts-with", "right": "green"}
        ]}
        routes = {"hit": "match", "default": "otherwise", "error": "invalid"}
        ordinary = self.run_local("ordered-condition", cfg, routes, {"name": "blue"}, "otherwise")
        absent = self.run_local("ordered-condition", cfg, routes, {"status": True}, "missing")
        incompatible = self.run_local("ordered-condition", cfg, routes, {"name": True}, "type")
        self.assertEqual(ordinary["history"][1]["port"], "default")
        self.assertEqual(absent["history"][1]["port"], "error")
        self.assertEqual(incompatible["context"]["result"]["branch"], "hit")

    def test_invalid_literal_group_and_duplicate_branch_ids_refuse_to_save(self):
        def plan(kind, config):
            return {"name": "Validate local block", "steps": [
                {"id": "start", "type": "start", "next": "transform"},
                {"id": "transform", "type": kind, "config": config, "next": "finish"},
                {"id": "finish", "type": "finish"},
            ]}

        with self.assertRaisesRegex(ValueError, "UTF-16"):
            plan_graph(plan("group-records", {
                "field": "status", "rows": [{"status": "🚀" * 81}]
            }), self.connections)
        with self.assertRaisesRegex(ValueError, "distinct"):
            plan_graph(plan("ordered-condition", {"branches": [
                {"id": "same", "operator": "equals", "right": 1},
                {"id": "same", "operator": "equals", "right": 2},
            ]}), self.connections)
