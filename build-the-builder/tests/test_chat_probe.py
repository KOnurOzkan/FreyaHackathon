from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import chat_probe
from tools.artifact_checks import canonical_digest


class ChatProbeTests(unittest.TestCase):
    def run_probe(self, fail_turn=None):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            root = Path(temp)
            definition = {"agent_config": {"llmConfig": {"provider": "experimental-5", "model": "experimental-5"}}}
            publication = {"workflow_id": "workflow", "agent_id": "agent", "version": 2,
                           "published_definition_sha256": canonical_digest(definition)}
            (root / "publication.json").write_text(json.dumps(publication))
            (root / "public-test-set.json").write_text(json.dumps([{"id": "synthetic", "variables": {
                "first_name": "Sample", "last_name": "Person", "title": "Ms", "now": "2030-01-08T11:00"}}]))
            (root / ".env").write_text("FREYA_API_KEY=fak_synthetic\nFREYA_BANK_TEAM_KEY=synthetic-team-key-12345\n")
            requests, turns = [], []
            def api(method, path, body=None, **params):
                requests.append((method, path))
                if path == "/api/v2/workflow/workflow":
                    return {"workflow": {"workflow": definition}}
                if path == "/api/v2/chat/message":
                    turns.append(body)
                    if len(turns) == fail_turn:
                        raise TimeoutError("Lost reply fak_synthetic")
                    return {"callId": "chat", "response": "synthetic reply"}
                if path == "/api/v2/chat/end":
                    return {"ended": True}
                raise AssertionError("Unexpected endpoint: " + path)
            freya = SimpleNamespace(api=api, AGENT_LLM=definition["agent_config"]["llmConfig"],
                workspace_id=lambda: "workspace", get_call=lambda call: {
                    "entityVersionNumber": 2, "assistantId": "agent", "assistantWorkflowId": "workflow", "messages": []})
            with patch.object(chat_probe, "ROOT", root), patch.dict(sys.modules, {"freya": freya}), redirect_stdout(io.StringIO()):
                report = chat_probe.probe(root / "publication.json", root / ".env", root / "out", "synthetic")
            for path in (root / "out").glob("*.json*"):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("fak_synthetic", text)
                self.assertNotIn("synthetic-team-key-12345", text)
            return report, requests, turns

    def test_two_turn_probe_ends_collects_and_verifies_actual_version(self):
        report, requests, turns = self.run_probe()
        self.assertEqual(report["turns_received"], 2)
        self.assertTrue(report["ended"])
        self.assertTrue(report["version_evidence"]["verified"])
        self.assertEqual(turns[1]["callId"], "chat")
        self.assertNotIn("customerVariables", turns[1])
        self.assertTrue(all("practice" not in path for _, path in requests))

    def test_second_turn_failure_still_ends_and_collects_known_chat(self):
        report, requests, _ = self.run_probe(fail_turn=2)
        self.assertEqual(len(report["errors"]), 1)
        self.assertTrue(report["ended"])
        self.assertTrue(report["version_evidence"]["verified"])
        self.assertEqual(requests.count(("POST", "/api/v2/chat/end")), 1)

    def test_uncertain_initial_creation_is_not_retried(self):
        report, requests, _ = self.run_probe(fail_turn=1)
        self.assertEqual(report["turns_received"], 0)
        self.assertEqual(requests.count(("POST", "/api/v2/chat/message")), 1)
        self.assertNotIn(("POST", "/api/v2/chat/end"), requests)
