import pytest

from parity.replay import evaluate
from parity.resources import examples


@pytest.fixture(scope="session")
def reference_report(tmp_path_factory):
    return evaluate(
        examples() / "artifacts/reference/commerce/adapter.py", tmp_path_factory.mktemp("reference")
    )
