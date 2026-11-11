"""Render a portable report from actual artifacts; no synthetic metric defaults."""

import difflib
import html
import json
from datetime import datetime, timezone
from pathlib import Path

from parity.contracts import analyze, load
from parity.resources import examples
from parity.sandbox import digest


def render_project(output: Path, result):
    rows = []
    for scenario in result["scenarios"]:
        actions = "".join(
            f"<li>{html.escape(action['method'])} {html.escape(action['path'])}: expected {action['expected_status']}, received {action['actual_status']}</li>"
            for action in scenario["actions"]
        )
        rows.append(
            f"<article><h2>{html.escape(scenario['scenario'])} — {scenario['status']}</h2><p>{html.escape(scenario.get('mismatch', 'Declared requests matched.'))}</p><ul>{actions}</ul><p>Business effects: {scenario['business_effects']}</p></article>"
        )
    status = "Passed" if result["passed"] == result["total"] else "Needs attention"
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(result["project"])} — PARITY</title><style>body{{font:16px/1.6 system-ui;max-width:850px;margin:40px auto;padding:0 20px;color:#172c24}}article{{border-top:1px solid #ccd4cf;padding:16px 0}}h1{{line-height:1.2}}h2{{font-size:20px}}p,li{{overflow-wrap:anywhere}}code{{font-size:14px}}</style><h1>{html.escape(result["project"])}: {status}</h1><p>{result["passed"]} / {result["total"]} declared scenarios passed.</p><p>Business effects: {result["business_effects"]}. Coverage is limited to the declared scenarios and invariants.</p>{"".join(rows)}<footer>Inspect <code>evaluation.json</code> for source and contract fingerprints. Fix the first failed request, then rerun <code>parity validate --project parity.json</code>.</footer></html>"""
    (output / "index.html").write_text(document)


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
        "These replays use SQLite. PostgreSQL has a separate optional integration check.",
        "Single adapter host; global OS lock intentionally serializes requests. No multi-host distributed locking.",
        "V1 temporal atomic visibility cannot be preserved while a V2 saga is pending. 503 PENDING requires same-key retry.",
        "No real money, auth migration, streaming, shipping or external writers. Listings and HTTP calls are bounded.",
        "Historical Codex transcripts include quota failures. Saved call records distinguish completed responses from failed requests.",
        "CLI default model identity and monetary cost were not disclosed; token counts and CLI version are recorded where available.",
        "Rejected generation has no workflow compatibility score. Public diagnostics and independent replay measure separate stages; these trials do not establish comparative superiority.",
        f"Transfer artifact provenance: {transfer_provenance}.",
        "Small deterministic fixture suite, seed 0; these results do not establish correctness for arbitrary APIs.",
        "macOS workers use Seatbelt. Linux uses bubblewrap network namespaces and Unix-socket relays. Windows untrusted execution fails closed.",
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
    project = examples()
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
