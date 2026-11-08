"""Model protocols and the real MediaPipe wrappers (mediapipe is imported lazily)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ha_edge.camera.pose_rules import Landmarks

PERSON_LABEL = "person"
weapon_labels = {"knife", "baseball bat"}

# BlazePose indices
_L_SHOULDER, _R_SHOULDER, _L_HIP, _R_HIP = 11, 12, 23, 24


@dataclass(frozen=True)
class Detection:
    label: str
    score: float


class PoseModel(Protocol):
    def torso(self, frame: Any) -> Landmarks | None: ...


class ObjectModel(Protocol):
    def detect(self, frame: Any) -> list[Detection]: ...


def _mp_image(mp: Any, frame: Any) -> Any:
    """BGR uint8 frame -> mediapipe RGB image."""
    import numpy as np

    rgb = np.ascontiguousarray(frame[:, :, ::-1], dtype=np.uint8)
    return mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)


class _Closable:
    _model: Any = None

    def close(self) -> None:
        model, self._model = self._model, None
        if model is not None:
            model.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001, S110 - interpreter shutdown: nothing useful to do
            pass


class MediapipePose(_Closable):
    """Pose landmarker (lite); first person only."""

    def __init__(self, model_path: Path | str) -> None:
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        self._mp = mp
        opts = vision.PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=vision.RunningMode.IMAGE,
            num_poses=1,
        )
        self._model = vision.PoseLandmarker.create_from_options(opts)

    def torso(self, frame: Any) -> Landmarks | None:
        if self._model is None:
            raise RuntimeError("MediapipePose is closed")
        res = self._model.detect(_mp_image(self._mp, frame))
        if not res.pose_landmarks:
            return None
        lms = res.pose_landmarks[0]

        def pt(i: int) -> tuple[float, float, float]:
            p = lms[i]
            return (float(p.x), float(p.y), float(p.visibility or 0.0))

        return Landmarks(pt(_L_SHOULDER), pt(_R_SHOULDER), pt(_L_HIP), pt(_R_HIP))


class MediapipeObjects(_Closable):
    """EfficientDet-Lite0 (COCO). Labels are reported as-is: "person" only ever means person."""

    def __init__(self, model_path: Path | str, score_threshold: float = 0.4) -> None:
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        self._mp = mp
        opts = vision.ObjectDetectorOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=vision.RunningMode.IMAGE,
            score_threshold=score_threshold,
            max_results=10,
        )
        self._model = vision.ObjectDetector.create_from_options(opts)

    def detect(self, frame: Any) -> list[Detection]:
        if self._model is None:
            raise RuntimeError("MediapipeObjects is closed")
        res = self._model.detect(_mp_image(self._mp, frame))
        out = []
        for d in res.detections:
            if d.categories:
                c = d.categories[0]
                out.append(Detection(c.category_name or "", float(c.score)))
        return out
