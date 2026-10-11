"""Render already-collected, redacted calls as readable local text; no network."""
import argparse
import json
from pathlib import Path
import re
from urllib.parse import urlencode


def render(folder, output):
    folder, output = Path(folder), Path(output)
    result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    review_path = folder / "manual-review.json"
    findings = json.loads(review_path.read_text(encoding="utf-8")).get("findings", []) if review_path.exists() else []
    reviewed = {item.get("case") for item in findings}
    output.mkdir(parents=True, exist_ok=False)
    index = ["Collected Freya conversations", "Source: " + str(folder.resolve()),
             "Automated pass does not override a transcript-review finding.", ""]
    counts = {}
    for row in result["rows"]:
        case = row["id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", case):
            raise ValueError("Invalid case filename")
        call = json.loads((folder / ("call-" + case + ".json")).read_text(encoding="utf-8"))
        status = "PASS" if row.get("pass") is True else "FAIL"
        if row.get("official_outcome") == "inconclusive" and row.get("ledger_pass") is True and row.get("call_ended") is True:
            status = "INCONCLUSIVE"
        if case in reviewed:
            status += "-REVIEW"
        url = "https://staging-app.freyavoice.ai/calls?" + urlencode({"workspaceId": call["workspaceId"], "id": call["id"]})
        lines = [case + " | " + status, "Freya: " + url,
                 "Workflow version: " + str(result["expected_version"]),
                 "Call: " + call["id"], "Source: " + str((folder / ("call-" + case + ".json")).resolve()), ""]
        if "official_outcome" in row:
            lines += ["Official verdict: " + str(row["official_outcome"]),
                      "Bank ledger passed: " + str(row.get("ledger_pass")),
                      "Simulation status at collection: " + str(result.get("status")), ""]
        lines += ["Automated finding: " + str(item) for item in row.get("failed", [])]
        lines += ["Review finding: " + item["observed"] for item in findings if item.get("case") == case]
        lines.append("")
        spoken = [message for message in call["messages"] if message.get("role") in {"assistant", "user"}]
        for message in spoken:
            lines.append(str(message.get("secondsFromStart", "?")) + "s " +
                         ("ASSISTANT" if message["role"] == "assistant" else "CUSTOMER") + ": " + message.get("message", ""))
        name = case + "-" + status + ".txt"
        (output / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        counts[case] = len(spoken)
        index.append(name + " | " + url)
    (output / "INDEX.txt").write_text("\n".join(index) + "\n", encoding="utf-8")
    return {"conversations": len(counts), "spoken_messages": sum(counts.values()), "output": str(output)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    print(json.dumps(render(args.source, args.out)))
