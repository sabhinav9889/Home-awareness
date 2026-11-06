from pathlib import Path

import app
from app.disclaimer import DISCLAIMER

TEXT = "Home-awareness tool. Not a medical device, security alarm, or emergency service."


def test_disclaimer_defined_once():
    assert DISCLAIMER == TEXT
    root = Path(app.__file__).parent
    holders = [p.name for p in root.rglob("*.py") if TEXT in p.read_text()]
    assert holders == ["disclaimer.py"]


def test_test_doubles_live_in_tests():
    import app.alerts.jobs as jobs
    import app.devices.models as device_models

    assert not hasattr(jobs, "RecordingJobRunner")
    assert not hasattr(device_models, "KINDS")  # dead: schemas own the allowed kinds
