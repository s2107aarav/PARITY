"""Optional real PostgreSQL integration in an isolated, disposable local cluster."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy import create_engine

from parity.replay import Replay
from parity.resources import examples, workspace
from parity.sandbox import free_port


def main():
    if not shutil.which("initdb") or not shutil.which("pg_ctl"):
        raise SystemExit("PostgreSQL server tools unavailable; no verification claim")
    result = {
        "backend": subprocess.check_output(["postgres", "--version"], text=True).strip(),
        "adapter_backend": "SQLite; single-host OS lock",
        "scenarios": [],
    }
    with tempfile.TemporaryDirectory(prefix="parity-postgres-") as temp:
        root = Path(temp)
        pgdata, log = root / "pgdata", root / "postgres.log"
        port = free_port()
        subprocess.run(
            [
                "initdb",
                "-D",
                str(pgdata),
                "-A",
                "trust",
                "--no-locale",
                "--encoding=UTF8",
                "-U",
                "parity",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        base = ["pg_ctl", "-D", str(pgdata), "-l", str(log), "-w"]

        def start():
            subprocess.run(
                [*base, "-o", f"-h 127.0.0.1 -p {port} -k {root}", "start"],
                check=True,
                capture_output=True,
            )

        start()
        replay = None
        try:
            url = f"postgresql+psycopg://parity@127.0.0.1:{port}/"
            engine = create_engine(url + "postgres", isolation_level="AUTOCOMMIT")
            with engine.connect() as conn:
                conn.exec_driver_sql("CREATE DATABASE parity_v1")
                conn.exec_driver_sql("CREATE DATABASE parity_v2")
            engine.dispose()
            replay = Replay(
                examples() / "artifacts/generated/commerce-live-001/adapter.py",
                root / "replay",
                old_database=url + "parity_v1",
                new_database=url + "parity_v2",
            )
            for scenario in (
                "ordinary",
                "payment_loss_restart",
                "concurrent_retry",
                "cancellation",
                "pagination",
            ):
                state = replay.run_scenario(scenario)
                result["scenarios"].append(
                    {"scenario": scenario, "status": "pass", "effects": state}
                )
            before = replay.verify_state()
            subprocess.run([*base, "stop", "-m", "fast"], check=True, capture_output=True)
            start()
            after = replay.verify_state()
            assert before == after, "business state did not survive PostgreSQL restart"
            result["scenarios"].append({"scenario": "postgres_restart", "status": "pass"})
            result["status"] = "verified"
        finally:
            if replay:
                replay.close()
            subprocess.run([*base, "stop", "-m", "fast"], check=False, capture_output=True)
    output = workspace() / "reports/postgres-verification.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
