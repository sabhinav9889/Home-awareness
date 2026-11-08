import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from ha_edge.camera import detectors
from ha_edge.camera.detectors import Detection, weapon_labels


def test_weapon_labels_and_detection():
    assert weapon_labels == {"knife", "baseball bat"}
    assert "cat" not in weapon_labels and "person" not in weapon_labels
    assert Detection("person", 0.9).label == "person"


def test_import_does_not_pull_mediapipe():
    code = "import sys, ha_edge.camera.detectors; assert 'mediapipe' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)


def _model_dir() -> Path:
    from ha_edge.camera.models import default_model_dir

    env = os.environ.get("HA_CAMERA_TEST_MODELS")
    return Path(env) if env else default_model_dir()


@pytest.mark.camera
def test_real_models_blank_frame_smoke():
    pytest.importorskip("mediapipe", reason="needs the camera extra")
    from ha_edge.camera import models as m

    d = _model_dir()
    env = bool(os.environ.get("HA_CAMERA_TEST_MODELS"))
    pose_p = d / ("pose.task" if env else m.POSE_FILE)
    obj_p = d / ("det.tflite" if env else m.OBJECT_FILE)
    if not (pose_p.exists() and obj_p.exists()):
        pytest.skip(f"models not in {d} (run `ha-camera fetch-model`)")
    pose = detectors.MediapipePose(pose_p)
    objs = detectors.MediapipeObjects(obj_p, score_threshold=0.4)
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    try:
        assert pose.torso(frame) is None
        assert [d for d in objs.detect(frame) if d.label == "person"] == []
    finally:
        pose.close()
        objs.close()
    pose.close()  # idempotent
    objs.close()
