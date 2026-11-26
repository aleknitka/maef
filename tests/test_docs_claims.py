"""Doc claims that can be checked mechanically: every event type the docs name is emitted by the code."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_documented_event_types_exist_in_code():
    text = (ROOT / "docs" / "BUDGETS_OBSERVABILITY.md").read_text()
    section = text.split("## Events per run", 1)[1].split("\n\n")[0]
    documented = set(re.findall(r"`([a-z]+\.[a-z_]+)`", section))
    assert len(documented) > 15
    source = "\n".join(p.read_text() for p in (ROOT / "apps" / "api" / "maef_api").rglob("*.py"))
    missing = sorted(t for t in documented if f'"{t}"' not in source)
    assert not missing, f"documented but never emitted: {missing}"
