import json

import pytest

from parity.contracts import analyze, load
from parity.plans import MigrationPlan, validate_plan
from parity.project import Project, initialize
from parity.resources import examples


def test_init_preserves_existing_work(tmp_path):
    initialize(tmp_path)
    original = (tmp_path / "parity.json").read_bytes()
    with pytest.raises(ValueError, match="overwrite"):
        initialize(tmp_path)
    assert (tmp_path / "parity.json").read_bytes() == original


def test_new_authentication_requirement_is_reported():
    old = {"info": {}, "paths": {}}
    new = {**old, "security": [{"api_key": []}]}
    assert analyze(old, new)["changes"][0]["kind"] == "security_changed"


def test_field_mapping_must_exist():
    root = examples()
    plan = MigrationPlan.model_validate_json(
        (root / "artifacts/generated/commerce-live-002/plan.json").read_text()
    )
    plan.mappings[0].field_mappings = {"not_a_field": "total_minor"}
    with pytest.raises(ValueError, match="not_a_field"):
        validate_plan(
            plan,
            load(root / "contracts/commerce/v1.json"),
            load(root / "contracts/commerce/v2.json"),
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:invalid",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:0",
        "http://@127.0.0.1",
        "http://:@127.0.0.1",
    ],
)
def test_invalid_endpoint_authority_is_rejected_before_network_calls(tmp_path, url):
    config_path = initialize(tmp_path, example="tickets")
    config = json.loads(config_path.read_text())
    config["old"]["base_url"] = url
    config_path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        Project(config_path)
