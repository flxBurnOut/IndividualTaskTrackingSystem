"""Background tests must not populate the user's Windows notification area."""
import pytest


@pytest.fixture(autouse=True)
def no_test_tray(monkeypatch):
    monkeypatch.setenv('PERSONAL_MANAGEMENT_NO_TRAY', '1')
    # Ordinary UI tests exercise business controls without first-launch tours.
    # Onboarding-specific tests explicitly enable automatic introductions.
    monkeypatch.setenv('PERSONAL_MANAGEMENT_NO_ONBOARDING', '1')
