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
from parity.moderated_replay import evaluate as evaluate_moderated
from parity.oneshot import generate as single_shot_generate
from parity.project import Project, initialize
from parity.providers import CodexProvider, OpenAIProvider
from parity.replay import SCENARIOS, evaluate
from parity.report import render
from parity.resources import examples, workspace
from parity.sandbox import digest
from parity.serving import Server
from parity.transfer_replay import evaluate as evaluate_transfer

PROJECT = examples()


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
        workspace()
        / "reports"
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
    (workspace() / "reports").mkdir(parents=True, exist_ok=True)
    (workspace() / "reports/LATEST").write_text(latest + "\n")
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
    init = sub.add_parser("init", help="Create a staging project configuration and scenario")
    init.add_argument("directory", type=Path, nargs="?", default=Path.cwd())
    init.add_argument("--example", choices=["tickets"])
    doctor = sub.add_parser(
        "doctor", help="Check project inputs, credentials and scenarios without network calls"
    )
    doctor.add_argument("--project", type=Path, default=Path("parity.json"))
    inspect = sub.add_parser("inspect", help="Print conservative OpenAPI delta JSON")
    inspect.add_argument("--old", type=Path)
    inspect.add_argument("--new", type=Path)
    inspect.add_argument("--project", type=Path)
    inspect.add_argument("--format", choices=["text", "json"], default="text")
    generate = sub.add_parser(
        "generate", help="Make real authenticated model calls; never replays a transcript"
    )
    generate.add_argument("--old", type=Path)
    generate.add_argument("--new", type=Path)
    generate.add_argument("--project", type=Path)
    generate.add_argument("--notes", type=Path)
    generate.add_argument("--provider", choices=["codex", "openai"], default="codex")
    generate.add_argument("--model", default=os.getenv("PARITY_MODEL"))
    generate.add_argument("--out", type=Path)
    generate.add_argument("--max-calls", type=int, choices=range(1, 15), default=10)
    generate.add_argument("--strategy", choices=["agent", "single-shot"], default="agent")
    generate.add_argument("--resume-artifact")
    validate = sub.add_parser("validate", help="Independent differential workflow checker")
    validate.add_argument("--adapter")
    validate.add_argument("--project", type=Path)
    validate.add_argument(
        "--suite", choices=["commerce", "transfer", "moderated"], default="commerce"
    )
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
    for name in ("status", "retry"):
        command = sub.add_parser(name, help="Inspect or retry a recorded adapter workflow")
        command.add_argument("--project", type=Path, default=Path("parity.json"))
        command.add_argument("--adapter", required=True)
        if name == "retry":
            command.add_argument("--request", required=True)
    return result


def main():
    args = parser().parse_args()
    try:
        project = Project(args.project) if getattr(args, "project", None) else None
        if args.command == "init":
            print(
                f"Created {initialize(args.directory, example=args.example)}. Run parity doctor --project {args.directory / 'parity.json'} to check the project."
            )
        elif args.command == "doctor":
            assert project is not None
            print(json.dumps(project.doctor(), indent=2))
        elif args.command == "inspect":
            if not project and (not args.old or not args.new):
                raise ValueError("use --project or both --old and --new")
            delta = (
                analyze(project.old, project.new)
                if project
                else analyze(load(args.old), load(args.new))
            )
            if args.format == "json":
                print(json.dumps(delta, indent=2))
            else:
                print(
                    f"{len(delta['changes'])} changes; {len(delta['added_operations'])} added operations"
                )
                for change in delta["changes"]:
                    print(f"  {change['operation']}: {change['kind']} {change.get('field', '')}")
                print("Behavioral compatibility requires scenario validation.")
        elif args.command == "generate":
            if project:
                project.doctor()
                args.old = project.root / project.settings.old.schema_path
                args.new = project.root / project.settings.new.schema_path
                args.notes = project.root / project.settings.notes
            elif not args.old or not args.new:
                raise ValueError("use --project or both --old and --new")
            if args.strategy == "single-shot" and args.resume_artifact:
                raise ValueError("--resume-artifact requires --strategy agent")
            if args.provider == "openai" and not args.model:
                raise ValueError("--model is required for the OpenAI provider")
            provider = (
                CodexProvider(args.model)
                if args.provider == "codex"
                else OpenAIProvider(args.model)
            )
            out = args.out or (project.workspace if project else workspace()) / "generated" / (
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
                    project=project,
                )
                if args.resume_artifact:
                    parent = Path(args.resume_artifact)
                    agent.resume(parent if parent.exists() else artifact_path(args.resume_artifact))
                manifest = agent.run()
            print(json.dumps(manifest, indent=2))
            if manifest.get("status") == "generation_rejected":
                raise SystemExit(2)
            if args.strategy == "agent" and manifest["status"] != "public_validation_passed":
                raise SystemExit(2)
        elif args.command == "validate":
            if project:
                source = artifact_path(args.adapter) / "adapter.py" if args.adapter else None
                report = project.validate(source, args.out)
                print(
                    f"{report['passed']}/{report['total']} scenarios passed; business effects: {report['business_effects']}"
                )
                for row in report["scenarios"]:
                    if row["status"] != "pass":
                        print(f"  FAIL {row['scenario']}: {row.get('mismatch')}")
                print(f"Report: {report['report']}")
                if report["passed"] != report["total"]:
                    raise SystemExit(1)
                return
            if not args.adapter:
                raise ValueError("--adapter is required without --project")
            artifact = artifact_path(args.adapter)
            fn = {
                "commerce": evaluate,
                "transfer": evaluate_transfer,
                "moderated": evaluate_moderated,
            }[args.suite]
            report = fn(artifact / "adapter.py", args.out or fresh_output(args.suite))
            print(
                f"{report['passed']}/{report['total']} passed; source SHA256 {report.get('artifact_sha256', report.get('source_sha256'))}"
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
        elif args.command == "status":
            assert project is not None
            source = artifact_path(args.adapter) / "adapter.py"
            for row in project.workflows(source):
                print(
                    f"{row['id']} {row['status']} {row['stage']} HTTP {row.get('http_status', '?')}"
                )
        elif args.command == "retry":
            assert project is not None
            result = project.retry(artifact_path(args.adapter) / "adapter.py", args.request)
            print(json.dumps(result, indent=2))
            if result["status"] >= 400:
                raise SystemExit(1)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"PARITY: {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
