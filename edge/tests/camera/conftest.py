"""Tests marked `camera` need the optional camera extra; skip them when it isn't installed."""

import importlib.util

import pytest


def pytest_collection_modifyitems(config, items):
    if importlib.util.find_spec("cv2") is not None:
        return
    skip = pytest.mark.skip(reason="needs the camera extra (uv sync --extra camera)")
    for item in items:
        if item.get_closest_marker("camera") is not None:
            item.add_marker(skip)
