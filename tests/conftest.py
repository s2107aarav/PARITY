from pathlib import Path

import pytest

from parity.replay import evaluate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def reference_report(tmp_path_factory):
    return evaluate(
        ROOT / "artifacts/reference/commerce/adapter.py", tmp_path_factory.mktemp("reference")
    )
