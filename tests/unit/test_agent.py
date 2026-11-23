import json

import pytest

from parity.agent import Agent, ToolCall
from parity.providers import ProviderUnavailable
from parity.resources import examples
from parity.sandbox import digest


class Unavailable:
    def complete(self, prompt, schema, directory):
        raise ProviderUnavailable("test-only unavailable endpoint")


class Malformed:
    def complete(self, prompt, schema, directory):
        return {"tool": "unrestricted_shell", "arguments": "{}", "reason": "synthetic parser test"}


def agent(tmp_path, provider):
    base = examples() / "contracts/transfer"
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


def test_resume_uses_committed_versions_instead_of_newer_aliases(tmp_path):
    first = agent(tmp_path, Malformed())
    resumed = agent(tmp_path / "next", Malformed())
    source = "def handle(ctx, method, path, body, query, headers):\n    return 200, {}\n"
    bundle = examples() / "artifacts/generated/transfer-live-001"
    try:
        first.tool(
            ToolCall(
                tool="submit_plan",
                arguments=json.dumps({"plan": json.loads((bundle / "plan.json").read_text())}),
                reason="test",
            )
        )
        first.tool(
            ToolCall(tool="submit_code", arguments=json.dumps({"source": source}), reason="test")
        )
        first.persist("running")
        (first.output / "adapter.py").write_text("partially saved newer source")
        (first.output / "plan.json").write_text("partially saved newer plan")
        resumed.resume(first.output)
        assert resumed.attempts == 1
        assert resumed.source.read_text() == source
        assert resumed.plan == first.plan
        assert resumed.feedback is None
        first.source.write_text("modified checkpoint")
        with pytest.raises(ValueError, match="checkpoint digest mismatch"):
            resumed.resume(first.output)
    finally:
        first.temp.cleanup()
        resumed.temp.cleanup()


def test_finish_refuses_replaced_adapter_after_public_pass(tmp_path, monkeypatch):
    trial = agent(tmp_path, Malformed())
    bundle = examples() / "artifacts/generated/transfer-live-001"
    try:
        trial.source = trial.output / "adapter-v1.py"
        trial.source.write_bytes((bundle / "adapter.py").read_bytes())
        alias = trial.output / "adapter.py"
        alias.write_bytes(trial.source.read_bytes())
        (trial.output / "plan.json").write_bytes((bundle / "plan.json").read_bytes())

        def replace_alias(source, domain, output):
            assert source.content == trial.source.read_bytes()
            alias.write_text("edited during public validation")
            return {"passed": True}

        monkeypatch.setattr("parity.agent.validate", replace_alias)
        trial.feedback = trial.validate(trial.source, trial.output / "public-v1")
        assert trial.feedback["adapter_sha256"] == digest(trial.source) != digest(alias)
        with pytest.raises(ValueError, match="source or plan changed"):
            trial.tool(ToolCall(tool="finish", arguments="{}", reason="test"))
    finally:
        trial.temp.cleanup()


def test_single_shot_cannot_silently_ignore_resume(monkeypatch, capsys):
    from parity.cli import main

    base = examples() / "contracts/transfer"
    monkeypatch.setattr(
        "sys.argv",
        [
            "parity",
            "generate",
            "--old",
            str(base / "v1.json"),
            "--new",
            str(base / "v2.json"),
            "--strategy",
            "single-shot",
            "--resume-artifact",
            "saved-run",
        ],
    )

    def unexpected_provider(*args):
        raise AssertionError("invalid options must be rejected before provider initialization")

    monkeypatch.setattr("parity.cli.CodexProvider", unexpected_provider)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "--resume-artifact requires --strategy agent" in capsys.readouterr().out
