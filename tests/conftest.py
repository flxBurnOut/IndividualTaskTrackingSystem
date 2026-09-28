"""Background tests must not populate the user's Windows notification area."""
import pytest


@pytest.fixture(autouse=True)
def no_test_tray(monkeypatch):
    monkeypatch.setenv('PERSONAL_MANAGEMENT_NO_TRAY', '1')
