"""Publish a redacted copy; original historical evidence remains untouched locally."""

import hashlib
import json
import os
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE = {
    ".git",
    ".venv",
    ".parity",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    "build",
    "dist",
}


def public_bytes(path):
    raw = path.read_bytes()
    if path.suffix in {".py", ".png", ".zip"}:
        return raw
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return raw
    text = text.replace(str(Path.home()), "<LOCAL_HOME>")
    text = re.sub(
        r'("(?:thread_id|account_id|user_id|hostname)"\s*:\s*")[^"]*(")', r"\1[REDACTED]\2", text
    )
    text = re.sub(r'hostname="[^"]*"', 'hostname="[REDACTED]"', text)
    text = re.sub(r"\b[A-Za-z0-9_-]+\.local\b", "[LOCAL_HOST]", text)
    return text.encode("utf-8")


def main():
    output = ROOT / "PARITY-release.zip"
    redactions = []
    secrets = re.compile(
        rb"sk-[A-Za-z0-9_-]{32,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    )
    count = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for directory, dirs, names in os.walk(ROOT):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDE and not d.endswith(".egg-info"))
            for name in sorted(names):
                path = Path(directory) / name
                relative = path.relative_to(ROOT)
                if (
                    path.suffix in {".zip", ".pyc", ".db"}
                    or name.endswith((".db-shm", ".db-wal"))
                    or name in {".env", ".DS_Store", "adapter.lock"}
                ):
                    continue
                if "worker" in relative.parts and name not in {
                    "http.jsonl",
                    "resources.json",
                    "termination.json",
                }:
                    continue
                raw = path.read_bytes()
                if secrets.search(raw):
                    raise RuntimeError(
                        f"credential pattern detected in {relative}; no value printed"
                    )
                public = public_bytes(path)
                if public != raw:
                    redactions.append(
                        {
                            "path": str(relative),
                            "private_sha256": hashlib.sha256(raw).hexdigest(),
                            "public_sha256": hashlib.sha256(public).hexdigest(),
                        }
                    )
                archive.writestr("PARITY/" + relative.as_posix(), public)
                count += 1
        archive.writestr(
            "PARITY/REDACTIONS.json",
            json.dumps(
                {
                    "policy": "Local home paths, hostnames and provider thread/account identifiers redacted in public copies only. Generated Python source and plans retained byte-for-byte. Raw historical evidence remains in the private workspace/archive; redacted transcript hashes intentionally differ.",
                    "files": redactions,
                },
                indent=2,
            ),
        )
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
    print(
        json.dumps(
            {
                "archive": output.name,
                "files": count,
                "redacted_files": len(redactions),
                "bytes": output.stat().st_size,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
