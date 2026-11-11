"""Real model providers. Authentication is never forwarded to generated workers."""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Protocol

import httpx


class Provider(Protocol):
    def complete(self, prompt: str, schema: dict, directory: Path) -> dict: ...


class ProviderUnavailable(RuntimeError):
    """Authentication/quota/transport failure; stop the trial instead of burning attempts."""


class CodexProvider:
    provenance = "real-authenticated-model-run"

    def __init__(self, model: str | None = None, timeout: int = 240):
        self.model, self.timeout = model, timeout
        if not shutil.which("codex"):
            raise RuntimeError(
                "codex CLI unavailable; install and run codex login, or use OpenAI provider"
            )

    def complete(self, prompt, schema, directory):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "prompt.txt").write_text(prompt)
        (directory / "schema.json").write_text(json.dumps(schema))
        # Only an empty temporary cwd. Host CLI tools, MCP/apps and skills are disabled;
        # model-selected actions execute through the typed engine tools, not native shell.
        with tempfile.TemporaryDirectory(prefix="parity-model-") as temp:
            cmd = [
                "codex",
                "exec",
                "--ignore-user-config",
                "--ignore-rules",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--json",
                "-C",
                temp,
                "--output-schema",
                str((directory / "schema.json").resolve()),
                "-o",
                str((directory / "response.json").resolve()),
            ]
            for feature in (
                "shell_tool",
                "unified_exec",
                "apps",
                "plugins",
                "remote_plugin",
                "browser_use",
                "browser_use_external",
                "computer_use",
                "code_mode_host",
                "multi_agent",
                "multi_agent_v2",
                "image_generation",
                "hooks",
                "skill_search",
            ):
                cmd += ["--disable", feature]
            cmd += [
                "-c",
                'web_search="disabled"',
                "-c",
                'model_reasoning_effort="medium"',
                "-c",
                'developer_instructions="Return only the requested structured response. Do not access local files or invoke native tools. All allowed actions are JSON tool requests handled by PARITY."',
            ]
            if self.model:
                cmd += ["--model", self.model]
            cmd.append("-")
            started = time.perf_counter()
            with (
                (directory / "events.jsonl").open("w") as events,
                (directory / "stderr.txt").open("w") as errors,
            ):
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=events,
                    stderr=errors,
                    text=True,
                    start_new_session=True,
                )
                try:
                    proc.communicate(prompt, timeout=self.timeout)
                except subprocess.TimeoutExpired as exc:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    raise ProviderUnavailable(
                        "model request timed out; trajectory retained"
                    ) from exc
                except BaseException:
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                    raise
            usage = None
            for line in (directory / "events.jsonl").read_text().splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("usage"):
                    usage = event["usage"]
                item = event.get("item", {})
                if item.get("type") in {"command_execution", "mcp_tool_call", "web_search"}:
                    raise RuntimeError("provider attempted a native tool; refused contaminated run")
            (directory / "usage.json").write_text(
                json.dumps(
                    {
                        "provider": "codex-cli",
                        "model_requested": self.model,
                        "model_observed": None,
                        "cli_version": subprocess.check_output(
                            ["codex", "--version"], text=True
                        ).strip(),
                        "usage": usage,
                        "elapsed_s": round(time.perf_counter() - started, 3),
                        "cost_usd": None,
                        "exit_code": proc.returncode,
                    },
                    indent=2,
                )
            )
            if proc.returncode or not (directory / "response.json").exists():
                raise ProviderUnavailable(
                    "Codex request failed (authentication, quota or transport); inspect "
                    + str(directory / "stderr.txt")
                )
            return json.loads((directory / "response.json").read_text())


class OpenAIProvider:
    provenance = "real-authenticated-model-run"

    def __init__(self, model: str):
        self.model = model
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError(
                "OPENAI_API_KEY is not configured; a Codex subscription is not an API key"
            )

    def complete(self, prompt, schema, directory):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "prompt.txt").write_text(prompt)
        started = time.perf_counter()
        try:
            response = httpx.post(
                "https://api.openai.com/v1/responses",
                timeout=180,
                headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
                json={
                    "model": self.model,
                    "input": prompt,
                    "max_output_tokens": 16000,
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "parity_response",
                            "strict": True,
                            "schema": schema,
                        }
                    },
                },
            )
            (directory / "raw-response.json").write_text(response.text)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                "OpenAI request failed (authentication, quota or transport); trajectory retained"
            ) from exc
        data = response.json()
        text = "".join(
            part.get("text", "")
            for item in data.get("output", [])
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        )
        parsed = json.loads(text)
        (directory / "response.json").write_text(json.dumps(parsed, indent=2))
        (directory / "usage.json").write_text(
            json.dumps(
                {
                    "provider": "openai-responses",
                    "model_requested": self.model,
                    "model_observed": data.get("model"),
                    "usage": data.get("usage"),
                    "cost_usd": None,
                    "elapsed_s": round(time.perf_counter() - started, 3),
                },
                indent=2,
            )
        )
        return parsed
