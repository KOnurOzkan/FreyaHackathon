from copy import deepcopy
import unittest

from tools.artifact_checks import ArtifactError, canonical_digest, test_version_evidence, verify_export, verify_published_export


class ArtifactChecksTests(unittest.TestCase):
    def setUp(self):
        self.definition = {"nodes": {"start": {"prompt": "synthetic"}},
                           "agent_config": {"llmConfig": {"provider": "experimental-5", "model": "experimental-5"}}}
        self.publication = {"status": "readback_verified", "differences": [], "workflow_id": "workflow",
            "agent_id": "agent", "version": 7, "workflow_sha256": "generated-hash",
            "published_definition_sha256": canonical_digest(self.definition)}
        self.iteration = {"runsTotal": 2, "runs": [
            {"callId": "call-a", "agentVersionNumber": 7}, {"callId": "call-b", "agentVersionNumber": "7"}]}
        self.calls = {"call-a": {"agentId": "agent"}, "call-b": {"agentId": "agent"}}
        self.result = {"status": "completed", "version_verified": True, "total": 2, "passed": 2,
            "rows": [{"id": "case-a", "pass": True}, {"id": "case-b", "pass": True}],
            "agent_id": "agent", "expected_version": 7, "workflow_sha256": "generated-hash"}
        self.exported = {"workflow_id": "workflow", "agent_id": "agent", "workflow": deepcopy(self.definition)}

    def test_actual_version_per_call_is_required_and_all_reports_must_agree(self):
        self.assertTrue(test_version_evidence(self.publication, self.iteration, self.calls)["verified"])
        for change in ({"agentVersionNumber": 6}, {"agentId": "other-agent"}, {"agentVersionNumber": True}):
            with self.subTest(change=change):
                calls = deepcopy(self.calls)
                calls["call-b"].update(change)
                self.assertFalse(test_version_evidence(self.publication, self.iteration, calls)["verified"])

    def test_expected_version_or_simulation_version_cannot_replace_missing_call_evidence(self):
        for run in self.iteration["runs"]:
            run.pop("agentVersionNumber")
            run["expected_version"] = 7
        result = test_version_evidence(self.publication, self.iteration, self.calls, {"agentVersionNumber": 7})
        self.assertFalse(result["verified"])
        self.assertIn("Actual call version was not reported", result["failures"])

    def test_live_call_entity_version_is_evidence_and_conflicting_aliases_fail(self):
        for run in self.iteration["runs"]:
            run.pop("agentVersionNumber")
            self.calls[run["callId"]] = {"entityVersionNumber": 7, "assistantId": "agent", "assistantWorkflowId": "workflow"}
        self.assertTrue(test_version_evidence(self.publication, self.iteration, self.calls)["verified"])
        for change in ({"agentVersionNumber": 8}, {"entityVersionNumber": 6},
                       {"assistantId": "another-agent"}, {"assistantWorkflowId": "another-workflow"}):
            calls = deepcopy(self.calls)
            calls["call-a"].update(change)
            self.assertFalse(test_version_evidence(self.publication, self.iteration, calls)["verified"])

    def test_missing_duplicate_and_uncollected_calls_are_rejected(self):
        for kind in ("count", "duplicate", "missing"):
            with self.subTest(kind=kind):
                iteration, calls = deepcopy(self.iteration), deepcopy(self.calls)
                if kind == "count":
                    iteration["runsTotal"] = 3
                elif kind == "duplicate":
                    iteration["runs"][1]["callId"] = "call-a"
                else:
                    calls.pop("call-b")
                self.assertFalse(test_version_evidence(self.publication, iteration, calls)["verified"])

    def test_exact_tested_export_is_accepted(self):
        result = verify_export(self.publication, self.result, self.definition, self.exported)
        self.assertEqual(result["status"], "verified_export")
        self.assertEqual(result["version"], 7)

    def test_known_review_findings_block_an_automated_pass(self):
        for review in ({"submission_ready": False}, {"submission_ready": True, "findings": [{"observed": "unsupported success claim"}]},
                       {"submission_ready": True, "workflow_version": 6}):
            with self.subTest(review=review), self.assertRaises(ArtifactError):
                verify_export(self.publication, self.result, self.definition, self.exported, review)

    def test_empty_failed_unverified_or_wrong_artifact_results_do_not_authorize_export(self):
        for change in ({"total": 0, "passed": 0, "rows": []}, {"passed": 1},
                       {"version_verified": False}, {"status": "failed"}, {"expected_version": 6},
                       {"workflow_sha256": "other-hash"}, {"agent_id": "different-agent"},
                       {"rows": [{"pass": True}]}):
            with self.subTest(change=change), self.assertRaises(ArtifactError):
                verify_export(self.publication, {**self.result, **change}, self.definition, self.exported)

    def test_changed_export_or_runtime_model_is_rejected(self):
        for target in ("workflow_id", "agent_id", "prompt", "model"):
            with self.subTest(target=target):
                exported = deepcopy(self.exported)
                if target in {"workflow_id", "agent_id"}:
                    exported[target] = "different"
                elif target == "prompt":
                    exported["workflow"]["nodes"]["start"]["prompt"] = "edited after testing"
                else:
                    exported["workflow"]["agent_config"]["llmConfig"]["model"] = "other-model"
                with self.assertRaises(ArtifactError):
                    verify_export(self.publication, self.result, self.definition, exported)

    def test_reordering_json_keys_does_not_create_a_false_content_mismatch(self):
        self.exported["workflow"] = dict(reversed(list(self.definition.items())))
        self.assertEqual(verify_export(self.publication, self.result, self.definition, self.exported)["status"], "verified_export")

    def test_final_export_needs_no_tests_but_keeps_every_publication_guard(self):
        # A final run's unseen brief has no matching practice customers.
        self.assertEqual(verify_published_export(self.publication, self.definition, self.exported)["status"], "verified_export")
        for change in ({"status": "saved"}, {"differences": ["nodes.start"]}, {"workflow_sha256": ""}):
            with self.subTest(change=change), self.assertRaises(ArtifactError):
                verify_published_export({**self.publication, **change}, self.definition, self.exported)
        for target in ("agent_id", "prompt", "model"):
            with self.subTest(target=target), self.assertRaises(ArtifactError):
                exported = deepcopy(self.exported)
                if target == "agent_id":
                    exported["agent_id"] = "different"
                elif target == "prompt":
                    exported["workflow"]["nodes"]["start"]["prompt"] = "edited after publication"
                else:
                    exported["workflow"]["agent_config"]["llmConfig"]["model"] = "other-model"
                verify_published_export(self.publication, self.definition, exported)
        # The strict development gate is unchanged: a failed test still blocks it.
        with self.assertRaises(ArtifactError):
            verify_export(self.publication, {**self.result, "passed": 1}, self.definition, self.exported)
