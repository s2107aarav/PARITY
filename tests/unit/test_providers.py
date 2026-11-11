import json

import httpx
import pytest

from parity.agent import Agent
from parity.providers import OpenAIProvider
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
