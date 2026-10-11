"""Keep practice-only customer fixtures separate from unseen builder inputs.

These are evaluator checks, never extraction prompts or runtime policy rules.
An altered brief or API can be evaluated locally, but the organizer's existing
practice sessions do not thereby acquire the altered contract or expectations.
"""
import hashlib
from pathlib import Path

from builder.inputs import InvalidInput, read_json

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def expected_practice_inputs(cases, root=ROOT):
    selected = cases.split(",") if isinstance(cases, str) else list(cases)
    if not selected or not all(isinstance(item, str) and item for item in selected) or len(set(selected)) != len(selected):
        raise InvalidInput("Select distinct, nonempty practice case IDs")
    available = {row["id"]: row for row in read_json(root / "public-test-set.json")}
    if any(case not in available for case in selected):
        raise InvalidInput("Selected cases are not in the supplied practice test set")
    families = {available[case]["brief"] for case in selected}
    if len(families) != 1 or not families.issubset({"A", "B"}):
        raise InvalidInput("One practice evaluation must use cases from one compatible brief")
    brief = next(iter(families))
    names = ["brief-a.md"] + (["brief-b.md"] if brief == "B" else [])
    return {"brief": brief, "cases": selected,
            "documents": [digest(root / "briefs" / name) for name in names],
            "api": digest(root / "tools/openapi.json")}


def check_practice_inputs(briefs, api, cases, root=ROOT):
    expected = expected_practice_inputs(cases, root)
    if [digest(path) for path in briefs] != expected["documents"] or digest(api) != expected["api"]:
        raise InvalidInput("Practice cases require their unchanged matching briefs and API; unseen or altered inputs need separate tests")
    return expected


def check_practice_manifest(manifest, cases, root=ROOT):
    """The compiled manifest carries the exact input hashes from generation."""
    expected = expected_practice_inputs(cases, root)
    if ([row.get("sha256") for row in manifest.get("documents", [])] != expected["documents"]
            or manifest.get("api", {}).get("sha256") != expected["api"]):
        raise InvalidInput("Compiled candidate inputs do not match the selected practice cases")
    return expected
