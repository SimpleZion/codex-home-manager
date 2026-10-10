import json
from pathlib import Path

import pytest

from backend.codex_data import timeline_event_from_item


fixtures = json.loads((Path(__file__).parent / "fixtures" / "timeline_classification_contract.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture", fixtures, ids=[fixture["name"] for fixture in fixtures])
def test_timeline_classification_contract(fixture):
    event = timeline_event_from_item(fixture["record"], 0)
    for key, value in fixture["expected"].items():
        assert event[key] == value
