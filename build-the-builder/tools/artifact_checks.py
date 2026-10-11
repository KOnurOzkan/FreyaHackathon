"""Pure checks linking generated, published, tested and exported workflow versions.

These establish consistency of supplied records, not authenticity of an API response.
They perform no network calls and never need credentials.
"""
import hashlib
import json


class ArtifactError(ValueError):
    pass


def canonical_digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def version_number(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    raise ArtifactError("Version is missing or is not a nonnegative integer")


def test_version_evidence(publication, iteration, calls, simulation=None):
    """Every run needs reported version evidence; an expected version is not evidence.

Use agentVersionNumber on a run, or entityVersionNumber on its fetched call
(observed on the live chat call endpoint). Check both if both are present. A
simulation-level value is checked for contradictions but does not replace missing
per-call evidence. Missing fields fail explicitly until live response shape is known.
"""
    expected = version_number(publication["version"])
    runs = iteration.get("runs", [])
    failures, evidence = [], []
    if not isinstance(runs, list) or not runs:
        return {"verified": False, "failures": ["No iteration runs to verify"], "runs": []}
    total = iteration.get("runsTotal", len(runs))
    if type(total) is not int or total != len(runs):
        failures.append("Iteration run count is incomplete")
    seen = set()
    for run in runs:
        call_id = run.get("callId")
        if not isinstance(call_id, str) or not call_id or call_id in seen:
            failures.append("Missing or duplicate call ID")
            continue
        seen.add(call_id)
        call = calls.get(call_id)
        if not isinstance(call, dict):
            failures.append("Full call was not collected")
            continue
        versions = []
        for label, item in (("run", run), ("call", call)):
            for field in ("agentVersionNumber", "entityVersionNumber"):
                if field in item:
                    try:
                        versions.append({"source": label + "." + field, "version": version_number(item[field])})
                    except ArtifactError:
                        failures.append("Invalid reported call version")
            if any(item.get(field) not in (None, publication["agent_id"]) for field in ("agentId", "assistantId")):
                failures.append("Call belongs to a different agent")
            if "assistantWorkflowId" in item and item["assistantWorkflowId"] != publication.get("workflow_id"):
                failures.append("Call belongs to a different workflow")
        if not versions:
            failures.append("Actual call version was not reported")
        if any(v["version"] != expected for v in versions):
            failures.append("Call ran a different workflow version")
        evidence.append({"call_id": call_id, "reported_versions": versions})
    if simulation:
        if simulation.get("agentId") not in (None, publication["agent_id"]):
            failures.append("Simulation belongs to a different agent")
        if "agentVersionNumber" in simulation:
            try:
                if version_number(simulation["agentVersionNumber"]) != expected:
                    failures.append("Simulation reports a different workflow version")
            except ArtifactError:
                failures.append("Invalid reported simulation version")
    return {"verified": not failures, "failures": failures, "runs": evidence}


def verify_export(publication, test_result, saved_definition, exported, review=None):
    if review is not None:
        if review.get("submission_ready") is not True or review.get("findings"):
            raise ArtifactError("Known transcript-review findings block export even when automated tests pass")
        if (review.get("workflow_version", publication["version"]) != publication["version"] or
                review.get("workflow_sha256", publication["workflow_sha256"]) != publication["workflow_sha256"]):
            raise ArtifactError("Transcript review belongs to a different candidate")
    if publication.get("status") != "readback_verified" or publication.get("differences"):
        raise ArtifactError("Publication was not verified")
    if test_result.get("status") != "completed" or test_result.get("version_verified") is not True:
        raise ArtifactError("Completed tests with verified actual call versions are required")
    total = test_result.get("total")
    rows = test_result.get("rows")
    if type(total) is not int or total < 1 or not isinstance(rows, list) or len(rows) != total:
        raise ArtifactError("Test results are empty or incomplete")
    if test_result.get("passed") != total or any(row.get("pass") is not True for row in rows):
        raise ArtifactError("Every selected test must pass before this export can be accepted")
    if test_result.get("agent_id") != publication["agent_id"]:
        raise ArtifactError("Tested agent does not match publication")
    if version_number(test_result.get("expected_version")) != version_number(publication["version"]):
        raise ArtifactError("Tested version does not match publication")
    if not publication.get("workflow_sha256") or test_result.get("workflow_sha256") != publication["workflow_sha256"]:
        raise ArtifactError("Tested source artifact does not match publication")
    return verify_published_export(publication, saved_definition, exported)


def verify_published_export(publication, saved_definition, exported):
    """The exported file is exactly the verified publication of this build.

    Final runs use this directly: their practice tests cannot match an unseen
    brief, so test evidence is recorded beside the export instead of gating it.
    """
    if publication.get("status") != "readback_verified" or publication.get("differences"):
        raise ArtifactError("Publication was not verified")
    if not publication.get("workflow_sha256"):
        raise ArtifactError("Publication lacks the built workflow hash")
    if exported.get("workflow_id") != publication["workflow_id"] or exported.get("agent_id") != publication["agent_id"]:
        raise ArtifactError("Export identifiers do not match the published agent")
    definition = exported.get("workflow")
    if not isinstance(definition, dict) or canonical_digest(definition) != canonical_digest(saved_definition):
        raise ArtifactError("Export differs from the selected saved definition")
    llm = definition.get("agent_config", {}).get("llmConfig", {})
    if (llm.get("provider"), llm.get("model")) != ("experimental-5", "experimental-5"):
        raise ArtifactError("Export changed the required event runtime model")
    published_hash = publication.get("published_definition_sha256")
    if published_hash and published_hash != canonical_digest(saved_definition):
        raise ArtifactError("Saved definition differs from the publication fingerprint")
    return {"status": "verified_export", "workflow_id": publication["workflow_id"],
            "agent_id": publication["agent_id"], "version": publication["version"],
            "workflow_sha256": publication["workflow_sha256"],
            "published_definition_sha256": canonical_digest(saved_definition)}
