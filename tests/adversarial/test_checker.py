from parity.adversarial import calibrate
from parity.replay import evaluate
from parity.resources import examples


def test_checker_rejects_real_defective_adapters(tmp_path):
    report = calibrate(examples() / "artifacts/reference/commerce/adapter.py", tmp_path)
    assert report["rejected"] == report["total"] == 5, report


def test_checker_rejects_boolean_in_place_of_integer(tmp_path):
    reference = examples() / "artifacts/reference/commerce/adapter.py"
    source = tmp_path / "boolean.py"
    source.write_text(
        reference.read_text()
        + """
original_handle = handle
def convert(value):
    if isinstance(value, dict):
        return {key: True if key == "quantity" and item == 1 else convert(item) for key, item in value.items()}
    if isinstance(value, list):
        return [convert(item) for item in value]
    return value
def handle(ctx, method, path, body, query, headers):
    status, data = original_handle(ctx, method, path, body, query, headers)
    return status, convert(data)
"""
    )
    report = evaluate(source, tmp_path / "trial", scenarios=("ordinary",))
    assert report["passed"] == 0
    assert "quantity" in report["scenarios"][0]["mismatch"]
