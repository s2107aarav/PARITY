import json
from pathlib import Path

import pytest

from parity.agent import Agent, ToolCall
from parity.providers import ProviderUnavailable

ROOT = Path(__file__).resolve().parents[2]


class Unavailable:
    def complete(self, prompt, schema, directory):
        raise ProviderUnavailable("test-only unavailable endpoint")


class Malformed:
    def complete(self, prompt, schema, directory):
        return {"tool": "unrestricted_shell", "arguments": "{}", "reason": "synthetic parser test"}


def agent(tmp_path, provider):
    base = ROOT / "contracts/transfer"
    return Agent(
        base / "v1.json",
        base / "v2.json",
        base / "notes.md",
        provider,
        tmp_path / "run",
        max_calls=2,
    )


def test_provider_failure_stops_without_burning_budget(tmp_path):
    trial = agent(tmp_path, Unavailable())
    result = trial.run()
    assert result["status"] == "provider_unavailable"
    assert result["model_calls"] == 1
    assert result["provenance"] == "no-successful-model-response"
    assert result["adapter_sha256"] is None


def test_malformed_output_is_bounded_and_persisted(tmp_path):
    trial = agent(tmp_path, Malformed())
    result = trial.run()
    assert result["status"] == "budget_exhausted" and result["model_calls"] == 2
    assert result["provenance"] == "test-provider"
    assert len(json.loads((trial.output / "state.json").read_text())["history"]) == 2


def test_source_cannot_precede_valid_plan(tmp_path):
    trial = agent(tmp_path, Malformed())
    try:
        with pytest.raises(ValueError, match="plan before source"):
            trial.tool(
                ToolCall(
                    tool="submit_code",
                    arguments=json.dumps(
                        {
                            "source": "def handle(ctx, method, path, body, query, headers):\n    return 200, {}\n"
                        }
                    ),
                    reason="synthetic ordering test",
                )
            )
    finally:
        trial.temp.cleanup()
