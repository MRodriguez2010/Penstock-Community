"""Verify exact receipt context, draft revision behavior, and model disclosure."""
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
from urllib import request

from test_workspace import Connections, Workspace, initialize, make_handler, serving
from guided_diagnosis import diagnose, provider_disclosures
from resources import DEFAULT_PROJECT


class GuidedDiagnosis(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        initialize(self.root)
        connections = Connections(self.root)
        connections.providers.update({
            "local-test": {"protocol": "ollama", "model": "reviewer-local", "url": "http://127.0.0.1:11434/api/chat"},
            "remote-test": {"protocol": "chat-completions", "model": "summer", "url": "https://model.example.test/v1/chat",
                            "key_env": "TEST_REMOTE_TOKEN"},
        })
        self.workspace = Workspace(self.root, connections)

    def numeric_failure(self):
        plan = {"name": "Review a reference", "steps": [
            {"id": "start", "type": "start", "label": "Check reference", "next": "finish",
             "config": {"inputFormat": [
                 {"id": "ref", "name": "reference", "type": "number", "description": "Reference"},
             ], "runMetadata": False}},
            {"id": "finish", "type": "finish", "label": "Finish"},
        ]}
        version = self.workspace.save(plan)
        run = self.workspace.start(version["workflow"], 1, "offline", {"reference": "private-reference"}, str(uuid.uuid4()))
        self.assertEqual(run["state"], "failed")
        return version, run

    def test_exact_failed_step_repair_is_editable_and_retestable(self):
        version, failure = self.numeric_failure()
        body = {"workflow": version["workflow"], "version": 1, "run": failure["id"], "node": "start"}
        viewer = diagnose(self.workspace, DEFAULT_PROJECT, body)
        self.assertEqual(viewer["code"], "invalid-number")
        self.assertNotIn("repair", viewer)
        edit = diagnose(self.workspace, DEFAULT_PROJECT, body, can_edit=True)
        self.assertEqual(edit["repair"]["node"], "start")
        fixed = self.workspace.save(edit["repair"]["plan"], version["workflow"], 1)
        self.assertEqual(fixed["version"], 2)
        test = self.workspace.start(fixed["workflow"], 2, "offline", failure["input"], str(uuid.uuid4()))
        self.assertEqual(test["state"], "finished")
        self.assertEqual(test["context"]["result"]["reference"], "private-reference")
        self.assertEqual(self.workspace.version(version["workflow"], 1)["digest"], version["digest"])
        with self.assertRaises(PermissionError):
            diagnose(self.workspace, DEFAULT_PROJECT, {**body, "version": 2})
        with self.assertRaises(ValueError):
            diagnose(self.workspace, DEFAULT_PROJECT, {**body, "node": "some-other-step"})
        other = self.workspace.create_project("Other project")["id"]
        with self.assertRaises(PermissionError):
            diagnose(self.workspace, other, body)

    def test_model_scope_and_provider_overrides(self):
        version, failure = self.numeric_failure()
        providers = provider_disclosures(self.workspace.connections)
        self.assertEqual(providers["remote-test"]["host"], "model.example.test")
        self.assertFalse(providers["remote-test"]["local"])
        self.assertTrue(providers["local-test"]["local"])
        self.assertNotIn("TEST_REMOTE_TOKEN", json.dumps(providers))
        self.assertNotIn("/v1/chat", json.dumps(providers))
        arguments = {"workflow": version["workflow"], "version": 1, "run": failure["id"],
                     "provider": "remote-test", "providerBinding": providers["remote-test"]["binding"]}
        with patch.object(self.workspace.connections, "complete", return_value="Test the input type.") as completion:
            with self.assertRaises(PermissionError):
                diagnose(self.workspace, DEFAULT_PROJECT, arguments, can_edit=True)
            with self.assertRaises(ValueError):
                diagnose(self.workspace, DEFAULT_PROJECT, {**arguments, "providerBinding": "stale", "modelConsent": True},
                         can_edit=True)
            completion.assert_not_called()
            report = diagnose(self.workspace, DEFAULT_PROJECT, {**arguments, "modelConsent": True}, can_edit=True)
        provider, _, model_input = completion.call_args.args
        self.assertEqual(provider, "remote-test")
        self.assertEqual(json.loads(model_input), {"finding": "invalid-number", "stepType": "start", "runState": "failed"})
        self.assertIn("Test the input type.", report["assistant"]["text"])

    def test_arbitrary_job_proposal_to_saved_workflow_to_model_result(self):
        plan = {"name": "Summarize any project meeting", "steps": [
            {"id": "start", "type": "start", "next": "write"},
            {"id": "write", "type": "agent", "next": "finish",
             "config": {"provider": "local-test", "instructions": "Summarize only the provided content."}},
            {"id": "finish", "type": "finish"},
        ]}
        formatted = "## Decisions\n\n- Pilot the process.\n\n## Evidence\n\n- Notes from the review."
        key = "test-" + str(uuid.uuid4())
        with patch.object(self.workspace.connections, "complete", side_effect=[json.dumps(plan), formatted]) as completion:
            with serving(make_handler(self.workspace, key)) as origin:
                def post(path, body):
                    req = request.Request(origin + "/api/" + path, data=json.dumps(body).encode(),
                        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
                    with request.urlopen(req, timeout=4) as response:
                        return json.load(response)

                model = post("drafting-providers", {"project": DEFAULT_PROJECT})["providers"]["local-test"]
                proposed = post("propose", {"project": DEFAULT_PROJECT, "provider": "local-test", "modelConsent": True,
                                           "providerBinding": model["binding"], "brief": "Summarize my next project meeting."})
                self.assertEqual(proposed["plan"]["name"], plan["name"])
                self.assertEqual(self.workspace.catalog()["versions"], [])
                edited = {**proposed["plan"], "name": "Executive meeting brief"}
                saved = post("save", {"project": DEFAULT_PROJECT, "plan": edited})
                run = post("start", {"workflow": saved["workflow"], "version": saved["version"], "mode": "test",
                                      "request": str(uuid.uuid4()), "input": "Pilot the process.", "modelConsent": True})
                self.assertEqual(run["state"], "finished")
                self.assertEqual(run["context"]["result"], formatted)
                self.assertFalse(saved["published"])
        self.assertEqual(completion.call_count, 2)
