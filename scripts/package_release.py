"""Package a reviewed working tree without including untracked run data."""

import hashlib
import json
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS = re.compile(rb"sk-[A-Za-z0-9_-]{32,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")


def public_bytes(path, raw):
    if path.suffix == ".py" or path.name == "plan.json" or path.name.startswith("plan-v"):
        return raw
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    text = text.replace(str(Path.home()), "<LOCAL_HOME>")
    text = re.sub(
        r'("(?:thread_id|account_id|user_id|hostname)"\s*:\s*")[^"]*(")', r"\1[REDACTED]\2", text
    )
    text = re.sub(r'hostname="[^"]*"', 'hostname="[REDACTED]"', text)
    text = re.sub(r"\b[A-Za-z0-9_-]+\.local\b", "[LOCAL_HOST]", text)
    return text.encode("utf-8")


def build_release(root: Path, output: Path):
    root = root.resolve()
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    redactions = []
    count = 0
    with tempfile.TemporaryDirectory(prefix="parity-release-", dir=output.parent) as temp:
        draft = Path(temp) / "release.zip"
        with zipfile.ZipFile(draft, "w", zipfile.ZIP_DEFLATED) as archive:
            for relative in sorted({Path(name) for name in tracked if name}):
                path = root / relative
                name = relative.name
                if (
                    path.suffix in {".zip", ".pyc", ".db", ".sqlite", ".sqlite3"}
                    or name.endswith((".db-shm", ".db-wal"))
                    or (name.startswith(".env") and name != ".env.example")
                    or name in {".DS_Store", "adapter.lock"}
                    or {".parity", "challenge-private", "worker"}.intersection(relative.parts)
                ):
                    continue
                if path.resolve() != path or not path.is_file():
                    raise ValueError(f"release input must be a regular file: {relative}")
                raw = path.read_bytes()
                if SECRETS.search(raw):
                    raise RuntimeError(
                        f"credential pattern detected in {relative}; no value printed"
                    )
                public = public_bytes(path, raw)
                if public != raw:
                    redactions.append(
                        {
                            "path": relative.as_posix(),
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
                        "policy": "Tracked release files only. Local paths and provider identifiers may be redacted in the public copy. Python source and accepted plans retain their original bytes.",
                        "files": redactions,
                    },
                    indent=2,
                ),
            )
        with zipfile.ZipFile(draft) as archive:
            corrupt = archive.testzip()
            if corrupt:
                raise RuntimeError(f"release archive failed integrity check: {corrupt}")
        draft.replace(output)
    return {
        "archive": output.name,
        "files": count,
        "redacted_files": len(redactions),
        "bytes": output.stat().st_size,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }


def main():
    print(json.dumps(build_release(ROOT, ROOT / "PARITY-release.zip"), indent=2))


if __name__ == "__main__":
    main()
