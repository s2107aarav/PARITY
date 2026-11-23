"""Check archived code/manifest consistency without making provider calls."""

import hashlib
import json

from parity.resources import examples
from parity.sandbox import digest


def main():
    root = examples() / "artifacts/generated"
    outputs = set()
    for response in root.glob("*/calls/*/response.json"):
        data = json.loads(response.read_text())
        if data.get("tool") == "submit_code":
            source = json.loads(data["arguments"])["source"]
            outputs.add(hashlib.sha256(source.encode()).hexdigest())
    rows = []
    for name in (
        "commerce-live-001",
        "commerce-live-002",
        "transfer-live-001",
        "moderated-release-001",
    ):
        artifact = root / name
        manifest = json.loads((artifact / "manifest.json").read_text())
        source = digest(artifact / "adapter.py")
        if source != manifest["adapter_sha256"] or source not in outputs:
            raise ValueError(f"{name}: source does not match manifest and recorded model output")
        if digest(artifact / "plan.json") != manifest["plan_sha256"]:
            raise ValueError(f"{name}: plan does not match manifest")
        for filename, expected in manifest["inputs"].items():
            if digest(artifact / "inputs" / filename) != expected:
                raise ValueError(f"{name}: {filename} does not match manifest")
        rows.append(
            {
                "artifact": name,
                "source_matches_recorded_model_output": True,
                "manifest_and_inputs_valid": True,
            }
        )
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
