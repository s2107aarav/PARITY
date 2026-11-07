from pathlib import Path

from parity.adversarial import calibrate

ROOT = Path(__file__).resolve().parents[2]


def test_checker_rejects_real_defective_adapters(tmp_path):
    report = calibrate(ROOT / "artifacts/reference/commerce/adapter.py", tmp_path)
    assert report["rejected"] == report["total"] == 5, report
