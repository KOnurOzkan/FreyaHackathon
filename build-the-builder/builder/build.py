"""Final-round entry point: python3 builder/build.py {case_folder} {mock_url}

One unattended run: read every document in the case folder (or zip), find or derive
the bank tool contract pointed at the team's mock, build the phone agent with the
source-backed pipeline, publish it, export it to submission/final.workflow.json,
and print a trace (start/end, every builder model call, what was created, tests).
Secrets are masked in everything printed. Nothing here knows any case content.
"""
import argparse
from datetime import datetime, timezone
import glob
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from builder import __version__  # noqa: E402
from builder.codex_provider import CodexProvider, ProviderError  # noqa: E402
from builder.inputs import InvalidInput  # noqa: E402
from builder.service_contract import derive_openapi, find_openapi  # noqa: E402
from builder.sources import convert_case  # noqa: E402

SECRETS = []


def mask(text):
    text = re.sub(r"fak_[A-Za-z0-9_\-]+", "fak_[redacted]", str(text))
    for secret in SECRETS:
        text = text.replace(secret, "[redacted]")
    return text


def log(message):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] {mask(message)}", flush=True)


def load_secrets(env_path):
    if env_path and env_path.is_file():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                value = line.split("=", 1)[1].strip().strip("'\"")
                if len(value) >= 8:
                    SECRETS.append(value)


def find_env(explicit):
    for candidate in ([Path(explicit)] if explicit else []) + [ROOT.parent / ".env", ROOT / ".env"]:
        if candidate.is_file():
            return candidate.resolve()
    raise SystemExit("No .env with FREYA_API_KEY found; pass --env")


def find_codex(explicit):
    if explicit or os.environ.get("CODEX_EXE"):
        return explicit or os.environ["CODEX_EXE"]
    found = shutil.which("codex.exe") or shutil.which("codex")
    if found and not found.lower().endswith((".cmd", ".bat", ".ps1")):
        return found
    home = Path.home()
    # The extension ships binaries for several platforms; take this platform's.
    pattern = ("bin/windows-*/codex.exe" if os.name == "nt" else
               "bin/darwin-*/codex" if sys.platform == "darwin" else "bin/linux-*/codex")
    candidates = glob.glob(str(home / ".vscode/extensions/openai.chatgpt-*" / Path(pattern)))
    if candidates:
        return max(candidates, key=os.path.getmtime)
    raise SystemExit("Codex CLI not found; pass --codex-exe or set CODEX_EXE")


def summarize(run_dir, export_path):
    """The trace the rules ask for, from the run's own records."""
    calls = []
    for trace in sorted(Path(run_dir).rglob("trace.jsonl")):
        for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "model_call_start":
                calls.append((event.get("at", ""), event.get("model") or event.get("provider", ""), event.get("purpose", "")))
    calls = sorted(set(calls))  # nested run traces repeat the same event
    log(f"builder model calls in the build pipeline: {len(calls)}")
    for at, model, purpose in calls:
        log(f"  model call {at}: {model} - {purpose}")
    if export_path and Path(export_path).is_file():
        exported = json.loads(Path(export_path).read_text(encoding="utf-8"))
        workflow = exported.get("workflow", exported)
        nodes = workflow.get("nodes") or workflow.get("definition", {}).get("nodes") or {}
        api_nodes = [n for n in nodes.values() if isinstance(n, dict) and n.get("node_type") == "api_request"]
        tools = {t.get("function", {}).get("name") for n in nodes.values() if isinstance(n, dict)
                 for t in (n.get("pre_actions") or []) + (n.get("tools") or []) if isinstance(t, dict)}
        log(f"exported workflow: {export_path} ({len(nodes)} steps, {len(api_nodes)} bank tool steps, "
            f"{len([t for t in tools if t])} distinct tools)")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the phone agent for one case folder, unattended")
    parser.add_argument("case_folder")
    parser.add_argument("mock_url")
    parser.add_argument("--env", help="File with FREYA_API_KEY (default: ../.env or .env)")
    parser.add_argument("--model", default=os.environ.get("BUILDER_MODEL", "gpt-6-astra"), help="Builder model")
    parser.add_argument("--codex-exe")
    parser.add_argument("--slot", default="final", help="Workflow slot, updated in place")
    parser.add_argument("--out", help="New run directory (default: out/final-<timestamp>)")
    parser.add_argument("--export", default="submission/final.workflow.json")
    parser.add_argument("--timeout", type=int, default=1800, help="Total budget in seconds (rules: 30 minutes)")
    args = parser.parse_args(argv)
    started, start_wall = time.monotonic(), datetime.now(timezone.utc)
    env_path = find_env(args.env)
    load_secrets(env_path)
    if not re.fullmatch(r"https://[A-Za-z0-9.\-]+(:\d+)?(/[A-Za-z0-9._~\-/]*)?", args.mock_url):
        raise SystemExit("mock_url must be a plain https URL")
    run_dir = (ROOT / (args.out or "out/final-" + start_wall.strftime("%Y%m%d-%H%M%S"))).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    log(f"run start {start_wall.isoformat()} | builder {__version__} | case {args.case_folder} | mock {args.mock_url}")
    try:
        sources, skipped, structured = convert_case(args.case_folder, run_dir / "sources", log=log)
        log(f"sources: {len(sources)} readable documents, {len(skipped)} skipped")
        spec = find_openapi(structured, args.mock_url, log=log)
        provider = CodexProvider(args.model, find_codex(args.codex_exe))
        if spec is None:
            provider.check_access(timeout=60)
            spec = derive_openapi(sources, args.mock_url, provider, timeout=600, log=log, out_dir=run_dir)
        api_path = run_dir / "openapi.json"
        api_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(f"contract: {api_path.name} with {len(spec['paths'])} tools at {spec['servers'][0]['url']}")
        remaining = int(args.timeout - (time.monotonic() - started))
        if remaining < 600:
            raise InvalidInput("Not enough time left for the build")
        command = [sys.executable, "-B", str(ROOT / "tools/complete_run.py")]
        for source in sources:
            command += ["--brief", str(source)]
        command += ["--api", str(api_path), "--out", str(run_dir / "build"), "--model", args.model,
                    "--codex-exe", find_codex(args.codex_exe), "--live", "--final", "--env", str(env_path),
                    "--slot", args.slot, "--timeout", str(remaining)]
        log("build: extract policy -> dialogue plan -> compile -> publish -> export (tools/complete_run.py --final)")
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=remaining + 60)
        for line in (result.stdout + result.stderr).splitlines():
            log("  " + line)
        manifest = json.loads((run_dir / "build/manifest.json").read_text(encoding="utf-8"))
        export_path = None
        if manifest.get("status") == "verified_export":
            target = ROOT / args.export
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(run_dir / "build" / manifest["export"], target)
            export_path = target
            log(f"created on Freya: workflow {manifest.get('workflow_id')} | agent {manifest.get('agent_id')} | version {manifest.get('version')}")
        for gap in manifest.get("best_effort_gaps", []):
            log(f"best-effort gap: {gap}")
        log(f"tests on the phone agent: local generated-workflow checks during compile; live self-test "
            f"{manifest.get('self_test', 'not_run')} (final mode builds, publishes and exports only)")
        summarize(run_dir, export_path)
        status = manifest.get("status")
    except (InvalidInput, ProviderError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        log(f"run failed: {type(exc).__name__}: {exc}")
        status = "failed"
    end_wall = datetime.now(timezone.utc)
    log(f"run end {end_wall.isoformat()} | status {status} | {round(time.monotonic() - started)} s")
    return 0 if status == "verified_export" else 2


if __name__ == "__main__":
    raise SystemExit(main())
