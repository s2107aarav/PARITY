import json

import httpx
import pytest

from parity.agent import Agent
from parity.providers import CodexProvider, OpenAIProvider
from parity.resources import examples


@pytest.mark.parametrize("status", [401, 429, 503])
def test_http_provider_failure_stops_agent_and_preserves_response(tmp_path, monkeypatch, status):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(status, text="upstream failure", request=request)
    calls = []

    def post(*args, **kwargs):
        calls.append(args)
        return response

    monkeypatch.setattr(httpx, "post", post)
    base = examples() / "contracts/transfer"
    agent = Agent(
        base / "v1.json",
        base / "v2.json",
        base / "notes.md",
        OpenAIProvider("test-model"),
        tmp_path / "run",
    )
    manifest = agent.run()
    assert manifest["status"] == "provider_unavailable"
    assert manifest["model_calls"] == len(calls) == 1
    assert manifest["adapter_sha256"] is None
    assert (agent.output / "calls/01/raw-response.json").read_text() == response.text
    assert json.loads((agent.output / "state.json").read_text())["status"] == "provider_unavailable"


def test_transport_failure_uses_provider_unavailable(tmp_path, monkeypatch):
    from parity.providers import ProviderUnavailable

    monkeypatch.setenv("OPENAI_API_KEY", "test-only")

    def post(*args, **kwargs):
        raise httpx.ConnectError("test offline")

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(ProviderUnavailable, match="transport"):
        OpenAIProvider("test-model").complete("prompt", {}, tmp_path)


def fake_codex(monkeypatch, lines):
    from pathlib import Path
    from types import SimpleNamespace

    captured = {}

    def spawn(command, **kwargs):
        captured["command"] = command
        cwd = Path(command[command.index("-C") + 1])
        assert cwd.is_dir() and not list(cwd.iterdir())
        Path(command[command.index("-o") + 1]).write_text('{"answer": true}')
        kwargs["stdout"].write("\n".join(lines) + "\n")
        return SimpleNamespace(returncode=0, communicate=lambda *args, **kwargs: None)

    monkeypatch.setattr("parity.providers.shutil.which", lambda _: "/test/codex")
    monkeypatch.setattr("parity.providers.subprocess.Popen", spawn)
    monkeypatch.setattr(
        "parity.providers.subprocess.check_output", lambda *args, **kwargs: "codex-cli test"
    )
    return captured


def test_codex_accepts_text_output_without_native_tools(tmp_path, monkeypatch):
    captured = fake_codex(
        monkeypatch,
        [
            json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": "Plan"}}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": '{"answer": true}'},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {"output_tokens": 1}}),
        ],
    )
    assert CodexProvider().complete("prompt", {}, tmp_path) == {"answer": True}
    command = captured["command"]
    disabled = [command[index + 1] for index, value in enumerate(command) if value == "--disable"]
    enabled = [command[index + 1] for index, value in enumerate(command) if value == "--enable"]
    assert "view_image" in disabled and "skip_host_skill_discovery" in enabled
    assert "--ignore-user-config" in command and "--ignore-rules" in command
    assert json.loads((tmp_path / "usage.json").read_text())["usage"] == {"output_tokens": 1}


@pytest.mark.parametrize(
    "kind",
    [
        "command_execution",
        "mcp_tool_call",
        "web_search",
        "file_change",
        "image_view",
        "plan_update",
        "future_native_tool",
    ],
)
def test_codex_rejects_any_native_tool_item(tmp_path, monkeypatch, kind):
    fake_codex(monkeypatch, [json.dumps({"type": "item.completed", "item": {"type": kind}})])
    with pytest.raises(RuntimeError, match="native tool"):
        CodexProvider().complete("prompt", {}, tmp_path)


@pytest.mark.parametrize("line", ["invalid JSON", "[]"])
def test_codex_cannot_accept_an_unauditable_event_stream(tmp_path, monkeypatch, line):
    fake_codex(monkeypatch, [line])
    with pytest.raises(RuntimeError, match="invalid JSON event"):
        CodexProvider().complete("prompt", {}, tmp_path)
