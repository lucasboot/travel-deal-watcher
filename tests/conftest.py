from __future__ import annotations

from pathlib import Path

import pytest

from travel_deal_watcher.config import load_config


@pytest.fixture
def config():
    project_root = Path(__file__).resolve().parents[1]
    return load_config(project_root / "config.yaml", environ={})
