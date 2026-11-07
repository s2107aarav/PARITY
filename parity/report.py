"""Render a portable report from actual artifacts; no synthetic metric defaults."""

import difflib
import json
from datetime import datetime, timezone
from pathlib import Path

from parity.contracts import analyze, load
from parity.sandbox import digest


def render(
    output: Path,
    evaluations: list,
    artifact: Path,
    direct_break: dict,
    calibration: dict,
    *,
    transfer_provenance: str,
    related_artifacts=(),
):
    manifest_path = artifact / "manifest.json"
    plan_path = artifact / "plan.json"
    manifest: dict = (
        json.loads(manifest_path.read_text())
        if manifest_path.exists()
        else {"provenance": "unknown"}
    )
    roots = {artifact, *related_artifacts}
    parent = manifest.get("parent")
    if parent:
        roots.add(artifact.parent / parent["artifact"])
    usages = [
        {"run": root.name, "call": p.parent.name, **json.loads(p.read_text())}
        for root in sorted(roots)
        for p in sorted(root.glob("calls/*/usage.json"))
    ]
    versions = sorted(artifact.glob("adapter-v*.py"))
    diffs = []
    for a, b in zip(versions, versions[1:], strict=False):
        diffs.append(
            {
                "name": f"{a.name} → {b.name}",
                "diff": "".join(
                    difflib.unified_diff(
                        a.read_text().splitlines(True),
                        b.read_text().splitlines(True),
                        a.name,
                        b.name,
                    )
                ),
            }
        )
    full = next(x["report"] for x in evaluations if x["role"] == "parity")
    transfer = next(x["report"] for x in evaluations if x["role"] == "transfer")
    limits = [
        "SQLite replay and a disposable PostgreSQL 18.3 integration/restart check were exercised. Docker Compose was not exercised (daemon unavailable).",
        "Single adapter host; global OS lock intentionally serializes requests. No multi-host distributed locking.",
        "V1 temporal atomic visibility cannot be preserved while a V2 saga is pending. 503 PENDING requires same-key retry.",
        "No real money, auth migration, streaming, shipping or external writers. Listings and HTTP calls are bounded.",
        "Historical Codex transcripts include quota failures. Saved call records distinguish completed responses from failed requests.",
        "CLI default model identity and monetary cost were not disclosed; token counts and CLI version are recorded where available.",
        "Rejected generation has no workflow compatibility score. Public diagnostics and independent replay measure separate stages; these trials do not establish comparative superiority.",
        f"Transfer artifact provenance: {transfer_provenance}.",
        "Small deterministic fixture suite, seed 0; these results do not establish correctness for arbitrary APIs.",
        "macOS workers use Seatbelt. Linux bubblewrap fallback shares host networking; Windows untrusted execution fails closed.",
    ]
    verified = (
        all(
            value["report"]["total"] > 0 and value["report"]["passed"] == value["report"]["total"]
            for value in evaluations
            if value["role"] in {"parity", "reference", "transfer"}
        )
        and calibration["total"] > 0
        and calibration["rejected"] == calibration["total"]
    )
    project = Path(__file__).resolve().parent.parent
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "direct_break": direct_break,
        "evaluations": evaluations,
        "artifact": {
            "manifest": manifest,
            "plan": json.loads(plan_path.read_text()) if plan_path.exists() else None,
            "source": (artifact / "adapter.py").read_text(),
            "revisions": diffs,
            "public_trials": [
                {"revision": p.parent.name, **json.loads(p.read_text())}
                for p in sorted(artifact.glob("public-v*/public-result.json"))
            ],
            "delta": analyze(
                load(project / "contracts/commerce/v1.json"),
                load(project / "contracts/commerce/v2.json"),
            ),
        },
        "usage": usages,
        "limitations": limits,
        "integrity": {
            "source_sha256": digest(artifact / "adapter.py"),
            "checker_sha256": full["checker_sha256"],
            "legacy_client_sha256": full["legacy_client_sha256"],
        },
        "summary": {
            "status": "VERIFIED fixture replay — see scope and limitations"
            if verified
            else "INCOMPLETE — one or more fixture controls failed",
            "commerce_passed": full["passed"],
            "commerce_total": full["total"],
            "transfer_passed": transfer["passed"],
            "transfer_total": transfer["total"],
            "mutants_rejected": calibration["rejected"],
            "mutants_total": calibration["total"],
        },
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "report-data.json").write_text(json.dumps(data, indent=2))
    template_path = Path(__file__).with_name("report_template.html")
    template = template_path.read_text()
    safe_json = (
        json.dumps(data).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )
    (output / "index.html").write_text(template.replace("__PARITY_DATA__", safe_json))
    return data
