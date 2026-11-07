"""Export real service schemas; enrich error docs with the actual service error envelope."""

import json
import tempfile
from pathlib import Path

from apps.commerce_v1 import create_app as commerce_v1
from apps.commerce_v2 import create_app as commerce_v2
from apps.transfer import create_v1, create_v2


def main():
    for domain, factories in {
        "commerce": (commerce_v1, commerce_v2),
        "transfer": (create_v1, create_v2),
    }.items():
        with tempfile.TemporaryDirectory() as temp:
            for version, factory in enumerate(factories, 1):
                app = factory(str(Path(temp) / f"v{version}.db"))
                doc = app.openapi()
                doc["x-parity-domain"] = domain
                doc["x-behavioral-notes"] = "notes.md"
                error_schema = (
                    {
                        "type": "object",
                        "properties": {"error": {"type": "string"}},
                        "required": ["error"],
                    }
                    if version == 1
                    else {
                        "type": "object",
                        "properties": {
                            "problem": {
                                "type": "object",
                                "properties": {"kind": {"type": "string"}},
                                "required": ["kind"],
                            }
                        },
                        "required": ["problem"],
                    }
                )
                for entry in doc["paths"].values():
                    for operation in entry.values():
                        if not isinstance(operation, dict) or "responses" not in operation:
                            continue
                        for status in ("402", "404", "409", "422", "503"):
                            operation["responses"][status] = {
                                "description": "Business error; see notes.md for operation-specific codes",
                                "content": {"application/json": {"schema": error_schema}},
                            }
                target = Path("contracts") / domain / f"v{version}.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(doc, indent=2) + "\n")
                app.state.store.close()


if __name__ == "__main__":
    main()
