"""Stable CLI for contract inspection, generation, replay and the reproducible demo."""

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import uvicorn

from apps.commerce_v1 import create_app as v1_app
from apps.commerce_v2 import create_app as v2_app
from apps.legacy_client import LegacyClient
from parity.adversarial import calibrate
from parity.agent import Agent
from parity.baselines import rules_adapter
from parity.contracts import analyze, load
from parity.fixtures import FIXTURES
from parity.oneshot import generate as single_shot_generate
from parity.providers import CodexProvider, OpenAIProvider
from parity.replay import SCENARIOS, evaluate
from parity.report import render
from parity.sandbox import digest
from parity.serving import Server
from parity.transfer_replay import evaluate as evaluate_transfer

PROJECT = Path(__file__).resolve().parent.parent


def artifact_path(value: str):
    path = Path(value)
    if not path.exists():
        path = PROJECT / "artifacts/generated" / value
    if path.is_file():
        path = path.parent
    if not (path / "adapter.py").is_file():
        raise ValueError("artifact must contain adapter.py")
    manifest = path / "manifest.json"
    if manifest.exists():
        expected = json.loads(manifest.read_text()).get("adapter_sha256")
        if expected and expected != digest(path / "adapter.py"):
            raise ValueError("artifact digest mismatch; refusing modified pinned source")
    return path.resolve()


def fresh_output(prefix):
    return (
        PROJECT
        / "artifacts/reports"
        / (prefix + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    )


def direct_break(root):
    old, new = Server(v1_app(str(root / "v1.db"))), Server(v2_app(str(root / "v2.db")))
    a, b = LegacyClient(old.url), LegacyClient(new.url)
    try:
        left, right = a.checkout(), b.checkout()
        return {
            "v1_status": left.status_code,
            "v2_status": right.status_code,
            "v1_body": left.json(),
            "v2_body": right.json(),
        }
    finally:
        a.close()
        b.close()
        old.close()
        new.close()


def demo(artifact, output, transfer_artifact=None, single_shot_artifact=None):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    evidence = []
    with tempfile.TemporaryDirectory(prefix="parity-demo-") as temp:
        broken = direct_break(Path(temp))
    print(
        f"Legacy client → V1: HTTP {broken['v1_status']}; unchanged client → V2: HTTP {broken['v2_status']}",
        flush=True,
    )
    for name, role, source in (
        ("PARITY / authenticated generated artifact", "parity", artifact / "adapter.py"),
        (
            "Manual reference / correctness calibration",
            "reference",
            PROJECT / "artifacts/reference/commerce/adapter.py",
        ),
        (
            "Rules-only operationId mapping",
            "rules",
            rules_adapter(
                load(PROJECT / "contracts/commerce/v1.json"),
                load(PROJECT / "contracts/commerce/v2.json"),
                output / "rules-artifact",
            ),
        ),
    ):
        print(f"Replaying {name}…", flush=True)
        report = evaluate(source, output / role)
        evidence.append({"name": name, "role": role, "report": report})
        print(f"  {report['passed']}/{report['total']} semantic workflows passed", flush=True)
    print("Calibrating checker against five deliberately defective adapters…", flush=True)
    calibration = calibrate(
        PROJECT / "artifacts/reference/commerce/adapter.py", output / "calibration"
    )
    print(f"  {calibration['rejected']}/{calibration['total']} rejected", flush=True)
    transfer_artifact = transfer_artifact or artifact_path("transfer-live-001")
    transfer_manifest = transfer_artifact / "manifest.json"
    transfer_provenance = (
        json.loads(transfer_manifest.read_text()).get("provenance", "unknown")
        if transfer_manifest.exists()
        else "manually-authored-reference"
    )
    print(f"Replaying transfer / {transfer_provenance}…", flush=True)
    transfer = evaluate_transfer(transfer_artifact / "adapter.py", output / "transfer")
    evidence.append(
        {
            "name": "Ticketing transfer / " + transfer_provenance,
            "role": "transfer",
            "report": transfer,
        }
    )
    print(f"  {transfer['passed']}/{transfer['total']} semantic workflows passed", flush=True)
    if single_shot_artifact:
        print("Replaying single-shot authenticated LLM baseline…", flush=True)
        baseline_manifest = json.loads((single_shot_artifact / "manifest.json").read_text())
        if baseline_manifest.get("status") == "generation_rejected":
            report = {
                "suite": "commerce",
                "passed": 0,
                "total": 0,
                "scenarios": [],
                "generation_failure": baseline_manifest["validation_failure"],
                "not_run": len(SCENARIOS),
                "artifact_sha256": None,
                "checker_sha256": None,
            }
            (output / "single-shot").mkdir()
            (output / "single-shot/evaluation.json").write_text(json.dumps(report, indent=2))
        else:
            report = evaluate(single_shot_artifact / "adapter.py", output / "single-shot")
        evidence.append(
            {
                "name": "Single-shot LLM / no generation feedback",
                "role": "single-shot",
                "report": report,
            }
        )
        if report.get("generation_failure"):
            print(
                "  Generation rejected; workflows not run: " + report["generation_failure"],
                flush=True,
            )
        else:
            print(f"  {report['passed']}/{report['total']} semantic workflows passed", flush=True)
    data = render(
        output,
        evidence,
        artifact,
        broken,
        calibration,
        transfer_provenance=transfer_provenance,
        related_artifacts=[
            transfer_artifact,
            *([single_shot_artifact] if single_shot_artifact else []),
        ],
    )
    latest = str(output.relative_to(PROJECT)) if output.is_relative_to(PROJECT) else str(output)
    (PROJECT / "artifacts/reports/LATEST").write_text(latest + "\n")
    print(f"Report: {output / 'index.html'}", flush=True)
    print(data["summary"]["status"], flush=True)
    healthy = [x["report"] for x in evidence if x["role"] in {"parity", "reference", "transfer"}]
    return (
        all(x["passed"] == x["total"] for x in healthy)
        and calibration["rejected"] == calibration["total"]
    )


def parser():
    result = argparse.ArgumentParser(
        prog="parity", description="Contract → plan → adapter → independent evidence"
    )
    sub = result.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="Print conservative OpenAPI delta JSON")
    inspect.add_argument("--old", type=Path, required=True)
    inspect.add_argument("--new", type=Path, required=True)
    generate = sub.add_parser(
        "generate", help="Make real authenticated model calls; never replays a transcript"
    )
    generate.add_argument("--old", type=Path, required=True)
    generate.add_argument("--new", type=Path, required=True)
    generate.add_argument("--notes", type=Path)
    generate.add_argument("--provider", choices=["codex", "openai"], default="codex")
    generate.add_argument("--model", default=os.getenv("PARITY_MODEL"))
    generate.add_argument("--out", type=Path)
    generate.add_argument("--max-calls", type=int, choices=range(1, 15), default=10)
    generate.add_argument("--strategy", choices=["agent", "single-shot"], default="agent")
    generate.add_argument("--resume-artifact")
    validate = sub.add_parser("validate", help="Independent differential workflow checker")
    validate.add_argument("--adapter", required=True)
    validate.add_argument("--suite", choices=["commerce", "transfer"], default="commerce")
    validate.add_argument("--out", type=Path)
    play = sub.add_parser(
        "demo", help="Offline replay with provenance, controls, baselines and HTML report"
    )
    play.add_argument("--artifact", default="commerce-live-002")
    play.add_argument("--transfer-artifact")
    play.add_argument(
        "--single-shot-artifact",
        type=Path,
        default=PROJECT / "artifacts/generated/commerce-single-shot-001",
    )
    play.add_argument("--out", type=Path)
    serve = sub.add_parser("serve", help="Run an independently inspectable fixture API")
    serve.add_argument("--suite", choices=list(FIXTURES), default="commerce")
    serve.add_argument("--version", choices=["old", "new"], required=True)
    serve.add_argument("--db", required=True)
    serve.add_argument("--port", type=int, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    return result


def main():
    args = parser().parse_args()
    try:
        if args.command == "inspect":
            print(json.dumps(analyze(load(args.old), load(args.new)), indent=2))
        elif args.command == "generate":
            if args.provider == "openai" and not args.model:
                raise ValueError("--model is required for the OpenAI provider")
            provider = (
                CodexProvider(args.model)
                if args.provider == "codex"
                else OpenAIProvider(args.model)
            )
            out = args.out or PROJECT / "artifacts/generated" / (
                "live-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            )
            if args.strategy == "single-shot":
                manifest = single_shot_generate(
                    args.old, args.new, args.notes or args.old.with_name("notes.md"), provider, out
                )
            else:
                agent = Agent(
                    args.old,
                    args.new,
                    args.notes or args.old.with_name("notes.md"),
                    provider,
                    out,
                    max_calls=args.max_calls,
                )
                if args.resume_artifact:
                    agent.resume(artifact_path(args.resume_artifact))
                manifest = agent.run()
            print(json.dumps(manifest, indent=2))
            if manifest.get("status") == "generation_rejected":
                raise SystemExit(2)
            if args.strategy == "agent" and manifest["status"] != "public_validation_passed":
                raise SystemExit(2)
        elif args.command == "validate":
            artifact = artifact_path(args.adapter)
            fn = evaluate if args.suite == "commerce" else evaluate_transfer
            report = fn(artifact / "adapter.py", args.out or fresh_output(args.suite))
            print(
                f"{report['passed']}/{report['total']} passed; source SHA256 {report['artifact_sha256']}"
            )
            for scenario in report["scenarios"]:
                if scenario["status"] != "pass":
                    print(f"  FAIL {scenario['scenario']}: {scenario.get('mismatch')}")
            if report["passed"] != report["total"]:
                raise SystemExit(1)
        elif args.command == "demo":
            success = demo(
                artifact_path(args.artifact),
                args.out or fresh_output("demo"),
                artifact_path(args.transfer_artifact) if args.transfer_artifact else None,
                args.single_shot_artifact.resolve() if args.single_shot_artifact else None,
            )
            if not success:
                raise SystemExit(1)
        elif args.command == "serve":
            factory = getattr(FIXTURES[args.suite], args.version)
            uvicorn.run(factory(args.db), host=args.host, port=args.port, log_level="info")
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"PARITY: {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
