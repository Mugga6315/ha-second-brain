from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def hass():
    mock = MagicMock()
    mock.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw))
    mock.config.path.return_value = "/tmp/test_second_brain"
    mock.config_entries.async_update_entry = MagicMock(side_effect=lambda entry, **kw: None)
    return mock


@pytest.fixture
def store(hass, tmp_path):
    from custom_components.second_brain.store import Store

    s = Store(hass, str(tmp_path))
    return s
