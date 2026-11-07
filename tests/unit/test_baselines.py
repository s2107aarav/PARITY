import json
import runpy
from unittest.mock import MagicMock

from parity.baselines import rules_adapter


def test_rules_mapper_does_not_match_absent_ids(tmp_path):
    old = {"paths": {"/old": {"get": {}}}}
    new = {"paths": {"/new": {"get": {}}}}
    rules_adapter(old, new, tmp_path)
    assert json.loads((tmp_path / "manifest.json").read_text())["mappings"] == []


def test_rules_mapper_treats_path_punctuation_literally(tmp_path):
    document = {"paths": {"/v1.0/items/{id}": {"get": {"operationId": "item"}}}}
    source = rules_adapter(document, document, tmp_path)
    handle = runpy.run_path(str(source))["handle"]
    ctx = MagicMock()
    ctx.call.return_value = {"found": True}
    assert handle(ctx, "GET", "/v1.0/items/7", None, {}, {}) == (200, {"found": True})
    ctx.call.assert_called_once_with("GET", "/v1.0/items/7", body=None, headers={}, query={})
    assert handle(ctx, "GET", "/v1x0/items/7", None, {}, {})[0] == 501
