import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from outrigger import events, reviewer, settings
from outrigger.registry import REGISTRY


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    settings.SETTINGS.clear()
    settings.SETTINGS.update(settings.DEFAULTS)
    REGISTRY.close_all()
    REGISTRY.turns.clear()
    REGISTRY.call_counts.clear()
    from outrigger import ws as _ws
    _ws._RATE.clear()
    REGISTRY.subagents.clear()
    REGISTRY.intent.clear()
    REGISTRY.actions.clear()
    reviewer._llm = None
    events._emit = None
    yield
    REGISTRY.close_all()
