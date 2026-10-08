"""Fixtures shared across the test suite.

The fakes themselves live in `fakes.py` (the test files construct them with
arguments); everything buildable with defaults is provided here so no test
module defines its own wrapper.
"""

from __future__ import annotations

import pytest
from fakes import _FakeClient, _FakeScreensaver, _FakeSettings


@pytest.fixture()
def fake_client(qapp) -> _FakeClient:
    return _FakeClient()


@pytest.fixture()
def fake_screensaver() -> _FakeScreensaver:
    return _FakeScreensaver()


@pytest.fixture()
def fake_settings() -> _FakeSettings:
    return _FakeSettings()
