"""Bounded Codex CLI adapter using existing ChatGPT authentication only.

No credential file is read, copied, or written by this adapter. No API-key fallback.
CLI configuration overrides apply only to the child invocation, never user settings.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from .inputs import InvalidInput, read_json


class ProviderError(RuntimeError):
    pass


def child_environment(codex_home=None):
    # An allowlist prevents bank/provider secrets in the parent environment reaching
    # the model process. Existing CLI auth remains in its own normal credential store.
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP",
               "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "LOCALAPPDATA", "APPDATA",
               "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    home = codex_home or os.environ.get("CODEX_HOME")
    if home is None:
        home = str(Path.home() / ".codex")
    path = Path(home).resolve()
    if not path.is_dir():
        raise ProviderError("Codex home does not exist; sign in through the normal CLI first")
    env["CODEX_HOME"] = str(path)
    return env


def find_codex(explicit=None):
    if explicit:
        path = Path(explicit).resolve()
    else:
        found = shutil.which("codex.exe") or shutil.which("codex")
        if not found:
            raise ProviderError("Codex CLI was not found; supply --codex-exe explicitly")
        path = Path(found).resolve()
    if not path.is_file() or path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise ProviderError("Supply a native Codex executable, not a shell wrapper")
    return str(path)


class CodexProvider:
    def __init__(self, model, executable=None, codex_home=None):
        if not model or any(c.isspace() for c in model) or model.startswith("-"):
            raise ProviderError("An explicit builder model name is required")
        self.model = model
        self.executable = find_codex(executable)
        self.env = child_environment(codex_home)
        self.metadata = None

    def _run(self, args, *, timeout, prompt=None, cwd=None):
        # shell=False and an argument list preserve paths/spaces without shell quoting.
        try:
            return subprocess.run([self.executable, *args], input=prompt, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=timeout,
                                  env=self.env, cwd=cwd, shell=False,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            # subprocess.run kills and collects the CLI process before returning.
            raise ProviderError("Codex exceeded the extraction deadline; no automatic retry") from exc
        except OSError as exc:
            raise ProviderError("Could not launch the Codex CLI") from exc

    def check_access(self, timeout=20):
        started = time.monotonic()
        result = self._run(["login", "status"], timeout=timeout)
        message = (result.stdout + result.stderr).lower()
        # Never echo auth status verbatim: API-key mode may include a key fragment.
        if result.returncode != 0 or "chatgpt" not in message or "api key" in message or "api_key" in message:
            raise ProviderError("Existing ChatGPT CLI authentication was not confirmed; API-key fallback is disabled")
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise ProviderError("Codex access check exceeded its deadline")
        version = self._run(["--version"], timeout=remaining)
        first = version.stdout.strip().splitlines()
        self.metadata = {"provider": "codex_cli", "authentication": "chatgpt", "model": self.model,
                         "cli_version": first[0] if version.returncode == 0 and first and
                         first[0].startswith("codex-cli ") and len(first[0]) < 100 else "unknown"}
        return dict(self.metadata)

    def extract(self, prompt, schema, *, timeout, event,
                purpose="Extract effective source-backed bank policy and API bindings"):
        if self.metadata is None:
            raise ProviderError("Check existing ChatGPT access before extraction")
        with tempfile.TemporaryDirectory(prefix="freya-extract-") as temp:
            folder = Path(temp)
            schema_path, response_path = folder / "response-schema.json", folder / "response.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            args = ["exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                    "--sandbox", "read-only", "--cd", str(folder), "--model", self.model,
                    "--json", "--color", "never", "--output-schema", str(schema_path),
                    "--output-last-message", str(response_path)]
            # No inherited MCP/app/plugin configuration or automatic local hooks.
            # Turn off command tools and discovery; the prompt already contains all inputs.
            overrides = {"model_provider": '"openai"', "forced_login_method": '"chatgpt"',
                         "approval_policy": '"never"', "web_search": '"disabled"',
                         "features.shell_tool": "false", "features.unified_exec": "false",
                         "features.apps": "false", "features.plugins": "false", "features.hooks": "false",
                         "features.multi_agent": "false", "features.memories": "false",
                         "project_doc_max_bytes": "0", "model_reasoning_effort": '"medium"'}
            for key, value in overrides.items():
                args += ["-c", key + "=" + value]
            args.append("-")
            event("model_call_start", purpose=purpose,
                  **self.metadata, prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                  timeout_seconds=round(timeout, 2))
            started = time.monotonic()
            try:
                result = self._run(args, timeout=timeout, prompt=prompt, cwd=temp)
                # Do not persist arbitrary CLI stderr, reasoning, or tool payloads.
                usage = None
                for line in result.stdout.splitlines():
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if item.get("type") == "turn.completed":
                        usage = {key: value for key, value in item.get("usage", {}).items()
                                 if key in {"input_tokens", "output_tokens", "cached_input_tokens"} and type(value) is int}
                    detail = item.get("item", {})
                    if item.get("type", "").startswith("item.") and detail.get("type") in {
                            "command_execution", "mcp_tool_call", "web_search", "file_change"}:
                        raise ProviderError("Extraction attempted a tool action; result rejected")
                if result.returncode != 0 or not response_path.is_file():
                    diagnostic = (result.stdout + result.stderr).lower()
                    category = next((label for terms, label in (
                        (("usage limit", "rate limit", "quota"), "usage_limit"),
                        (("invalid_json_schema", "invalid schema", "response_format"), "output_schema"),
                        (("model is not supported", "model_not_found", "does not exist"), "model_unavailable"),
                        (("unauthorized", "token expired", "authentication"), "authentication"),
                        (("unexpected argument", "unknown field", "config error"), "cli_configuration"),
                        (("connection", "timed out", "network"), "connection"),
                    ) if any(term in diagnostic for term in terms)), "unclassified")
                    raise ProviderError(f"Codex extraction failed ({category}); raw CLI output withheld")
                if response_path.stat().st_size > 4_000_000:
                    raise ProviderError("Codex response exceeds the extraction size limit")
                response = read_json(response_path)
                event("model_call_end", status="completed", seconds=round(time.monotonic() - started, 3), usage=usage)
                return response
            except (ProviderError, InvalidInput, OSError) as exc:
                event("model_call_end", status="failed", seconds=round(time.monotonic() - started, 3),
                      error_type=type(exc).__name__)
                raise
