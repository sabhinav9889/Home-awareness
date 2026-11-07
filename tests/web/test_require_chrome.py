"""REQUIRE_CHROME=1 (set in CI) turns a missing Chrome into a failure instead of a skip."""

import pytest

from tests.web import conftest


def _no_chrome(monkeypatch):
    monkeypatch.setattr(conftest.shutil, "which", lambda _name: None)


def test_missing_chrome_skips_by_default(monkeypatch):
    _no_chrome(monkeypatch)
    monkeypatch.delenv("REQUIRE_CHROME", raising=False)
    with pytest.raises(pytest.skip.Exception):
        conftest._require_chrome()


def test_missing_chrome_fails_when_required(monkeypatch):
    _no_chrome(monkeypatch)
    monkeypatch.setenv("REQUIRE_CHROME", "1")
    with pytest.raises(pytest.fail.Exception, match="REQUIRE_CHROME"):
        conftest._require_chrome()


def test_present_chrome_passes(monkeypatch):
    monkeypatch.setattr(conftest.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("REQUIRE_CHROME", "1")
    conftest._require_chrome()
