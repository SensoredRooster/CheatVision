from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

import cv2
import numpy as np


@dataclass
class CrosshairSample:
    point: tuple[int, int]
    velocity: float
    straightness: float
    tremor_variance: float
    zero_tremor_streak: int


class CrosshairKinematicsAnalyzer:
    """Score *aim motion*, not a moving reticle sprite.

    In an FPS the crosshair is fixed near screen center. Aimbots snap the
    camera, which shows up as a global translation of the scene under that
    reticle. We measure that with phase correlation on the center ROI and
    treat the reticle itself as geometric center (optionally refined).

    Thresholds were set against real 2560x1440@144 Warzone footage of legit
    play (see README "Aim scoring"): a human flick is *briefly* perfectly
    straight (straightness 0.99+ for a handful of frames, steps up to ~47px
    at 960x540) but never holds a tremor-free line for long. What separates
    assistance is *sustained* geometric motion, so the line rule needs a
    minimum straight duration in seconds, not a single-window snapshot.
    """

    def __init__(
        self,
        window_size: int = 18,
        roi_ratio: float = 0.22,
        velocity_threshold: float = 16.0,
        straightness_threshold: float = 0.985,
        snap_threshold: float = 36.0,
        zero_variance_epsilon: float = 0.45,
        min_phase_response: float = 0.12,
        lock_streak: int = 3,
        line_hold_seconds: float = 0.25,
        lock_hold_seconds: float = 0.12,
    ) -> None:
        self.window_size = window_size
        self.roi_ratio = roi_ratio
        self.velocity_threshold = velocity_threshold
        self.straightness_threshold = straightness_threshold
        self.snap_threshold = snap_threshold
        self.zero_variance_epsilon = zero_variance_epsilon
        self.min_phase_response = min_phase_response
        self.lock_streak = lock_streak
        # A geometric line only counts once it has persisted this long; on
        # legit footage straight runs last <0.15s (measured max 22 analysed
        # frames at 72fps ~ 0.3s across a whole clip, median far lower).
        self.line_hold_seconds = line_hold_seconds
        self.lock_hold_seconds = lock_hold_seconds
        self.delta_history: deque[tuple[float, float]] = deque(maxlen=window_size)
        self.previous_roi: Optional[np.ndarray] = None
        self.previous_gray: Optional[np.ndarray] = None
        self.zero_tremor_streak = 0
        self.last_metrics: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._line_since: float | None = None
        self._lock_since: float | None = None
        self._last_timestamp: float | None = None

    def reset(self) -> None:
        with self._lock:
            self.delta_history.clear()
            self.previous_roi = None
            self.previous_gray = None
            self.zero_tremor_streak = 0
            self.last_metrics = None
            self._line_since = None
            self._lock_since = None
            self._last_timestamp = None

    def _center_roi(self, gray: np.ndarray) -> np.ndarray:
        height, width = gray.shape[:2]
        roi_w = max(48, int(width * self.roi_ratio))
        roi_h = max(48, int(height * self.roi_ratio))
        x1 = max(0, (width - roi_w) // 2)
        y1 = max(0, (height - roi_h) // 2)
        return gray[y1 : y1 + roi_h, x1 : x1 + roi_w]

    def _scene_delta(self, previous: np.ndarray, current: np.ndarray) -> tuple[float, float, float]:
        """Global translation between two centre ROIs.

        Fine estimate: phase correlation on four edge bands (sub-pixel, but
        only valid for shifts small relative to the band). Coarse estimate:
        phase correlation on the whole ROI. A snap of 60px at 960x540 is
        invisible to the bands and used to be measured as ~7px, hiding the
        very motion the snap rules look for; when the two disagree by more
        than a band can resolve, the coarse estimate wins.
        """
        height, width = previous.shape[:2]
        band_h = max(16, height // 5)
        band_w = max(16, width // 5)
        bands = (
            (previous[:band_h, :], current[:band_h, :]),
            (previous[-band_h:, :], current[-band_h:, :]),
            (previous[:, :band_w], current[:, :band_w]),
            (previous[:, -band_w:], current[:, -band_w:]),
        )
        shifts_x: list[float] = []
        shifts_y: list[float] = []
        responses: list[float] = []
        for prev_band, curr_band in bands:
            if prev_band.shape[0] < 8 or prev_band.shape[1] < 8:
                continue
            window = cv2.createHanningWindow((prev_band.shape[1], prev_band.shape[0]), cv2.CV_32F)
            shift, response = cv2.phaseCorrelate(
                prev_band.astype(np.float32),
                curr_band.astype(np.float32),
                window,
            )
            shifts_x.append(float(shift[0]))
            shifts_y.append(float(shift[1]))
            responses.append(float(response))
        if not responses:
            return 0.0, 0.0, 0.0
        fine_x, fine_y, fine_resp = float(np.median(shifts_x)), float(np.median(shifts_y)), float(np.median(responses))

        window = cv2.createHanningWindow((width, height), cv2.CV_32F)
        coarse_shift, coarse_resp = cv2.phaseCorrelate(
            previous.astype(np.float32), current.astype(np.float32), window
        )
        coarse_x, coarse_y = float(coarse_shift[0]), float(coarse_shift[1])
        resolvable = min(band_h, band_w) * 0.5
        disagreement = ((coarse_x - fine_x) ** 2 + (coarse_y - fine_y) ** 2) ** 0.5
        if disagreement > resolvable and coarse_resp >= self.min_phase_response:
            return coarse_x, coarse_y, float(coarse_resp)
        return fine_x, fine_y, fine_resp

    def _refine_reticle(self, gray: np.ndarray) -> tuple[int, int]:
        """Prefer a small bright mark at center (dot/plus); else geometric center.

        The refinement must be conservative: on textured scenes the brightest
        speck near centre jumps around by +-20px frame to frame, which the
        kinematics would read as tremor. Only accept a mark that is both
        bright *and* far brighter than anything else in the patch.
        """
        height, width = gray.shape[:2]
        cx, cy = width // 2, height // 2
        radius = 12
        y1 = max(0, cy - radius)
        x1 = max(0, cx - radius)
        y2 = min(height, cy + radius)
        x2 = min(width, cx + radius)
        patch = gray[y1:y2, x1:x2]
        if patch.size == 0:
            return (cx, cy)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        tophat = cv2.morphologyEx(patch, cv2.MORPH_TOPHAT, kernel)
        _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(tophat)
        if max_val < 60 or max_val < float(np.percentile(tophat, 95)) * 1.8:
            return (cx, cy)
        return (x1 + int(max_loc[0]), y1 + int(max_loc[1]))

    def _line_residuals(self, coords: np.ndarray) -> np.ndarray:
        start = coords[0]
        end = coords[-1]
        path = end - start
        path_length = float(np.linalg.norm(path))
        if path_length <= 0.0:
            return np.zeros(len(coords), dtype=np.float32)

        unit = path / path_length
        offsets = coords - start
        projections = np.dot(offsets, unit)
        projected = np.outer(projections, unit) + start
        residuals = np.linalg.norm(coords - projected, axis=1)
        return residuals.astype(np.float32)

    def _idle_metrics(self, point: tuple[int, int], extra: dict[str, Any] | None = None) -> dict[str, Any]:
        metrics: dict[str, Any] = {
            "flagged": False,
            "point": point,
            "velocity": 0.0,
            "straightness": 0.0,
            "tremor_variance": 0.0,
            "zero_tremor_streak": self.zero_tremor_streak,
            "flow_response": 0.0,
            "path": [],
        }
        if extra:
            metrics.update(extra)
        return metrics

    def update(self, frame: np.ndarray, timestamp: float | None = None) -> Optional[dict[str, Any]]:
        with self._lock:
            frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            point = self._refine_reticle(frame_gray)
            roi = self._center_roi(frame_gray)

            if self.previous_roi is None or self.previous_roi.shape != roi.shape:
                self.previous_gray = frame_gray.copy()
                self.previous_roi = roi.copy()
                self.delta_history.clear()
                self.zero_tremor_streak = 0
                self.last_metrics = self._idle_metrics(point)
                return None

            scene_dx, scene_dy, response = self._scene_delta(self.previous_roi, roi)
            self.previous_gray = frame_gray.copy()
            self.previous_roi = roi.copy()

            if response < self.min_phase_response:
                # Unreliable measurement (flat texture / motion blur). Keep the
                # hold clocks alive briefly so one bad frame in the middle of
                # a mechanical pan cannot reset the evidence; they expire on
                # their own if the motion really stopped.
                now_unreliable = float(timestamp) if timestamp is not None else (
                    (self._last_timestamp or 0.0) + 1.0 / 60.0
                )
                self._last_timestamp = now_unreliable
                for attr in ("_line_since", "_lock_since"):
                    since = getattr(self, attr)
                    if since is not None and (now_unreliable - since) > self.line_hold_seconds * 2.0:
                        setattr(self, attr, None)
                self.last_metrics = self._idle_metrics(point, {"flow_response": response})
                return None

            # Scene translation under a fixed reticle => camera/aim moved the other way.
            dx = -scene_dx
            dy = -scene_dy
            self.delta_history.append((dx, dy))

            last_step = float((dx * dx + dy * dy) ** 0.5)
            if len(self.delta_history) < 4:
                self.last_metrics = self._idle_metrics(
                    point,
                    {
                        "velocity": last_step,
                        "last_dx": dx,
                        "last_dy": dy,
                        "last_step": last_step,
                        "max_step": last_step,
                        "flow_response": response,
                    },
                )
                return None

            deltas = np.array(self.delta_history, dtype=np.float32)
            step_distances = np.linalg.norm(deltas, axis=1)
            path = np.cumsum(deltas, axis=0)
            coords = np.vstack([np.zeros((1, 2), dtype=np.float32), path])
            displacement = float(np.linalg.norm(coords[-1] - coords[0]))
            total_path_length = float(np.sum(step_distances))
            straightness = float(displacement / total_path_length) if total_path_length > 0.0 else 1.0

            residuals = self._line_residuals(coords)
            residual_var = float(np.var(residuals))
            step_var = float(np.mean(np.var(deltas, axis=0))) if len(deltas) else 0.0
            # Deviation from a straight line is the wrong tremor measure for a
            # bot *tracking* a moving target: the path curves smoothly and
            # residual_var reads 10-16 while the hand is doing nothing at all.
            # Hand tremor is frame-to-frame *jerk*: the second difference of
            # the path. A human tracking a curve still jitters (measured >=1.5
            # on legit footage); a mechanical lock follows the curve exactly.
            if len(deltas) >= 3:
                accel = np.diff(deltas, axis=0)
                jerk_variance = float(np.mean(np.var(accel, axis=0)))
            else:
                jerk_variance = step_var
            tremor_variance = max(residual_var, step_var)
            mean_velocity = float(np.mean(step_distances))
            max_step = float(np.max(step_distances))

            smooth_lock = jerk_variance <= self.zero_variance_epsilon
            if mean_velocity > self.velocity_threshold * 0.65 and (tremor_variance <= self.zero_variance_epsilon or smooth_lock):
                self.zero_tremor_streak += 1
            else:
                self.zero_tremor_streak = 0

            now = float(timestamp) if timestamp is not None else (
                (self._last_timestamp or 0.0) + 1.0 / 60.0
            )
            self._last_timestamp = now

            # Straight-and-fast is the *raw* line condition. Humans satisfy it
            # for a few frames on every flick, so it only becomes an event
            # once it has held continuously for line_hold_seconds. A single
            # oversized step (old snap_threshold rule) is not evidence on its
            # own: legit flicks reach 47px at 960x540.
            line_now = mean_velocity >= self.velocity_threshold and straightness >= self.straightness_threshold
            if line_now:
                if self._line_since is None:
                    self._line_since = now
            else:
                self._line_since = None
            line_held = self._line_since is not None and (now - self._line_since) >= self.line_hold_seconds

            lock_now = self.zero_tremor_streak >= self.lock_streak and mean_velocity >= self.velocity_threshold * 0.65
            if lock_now:
                if self._lock_since is None:
                    self._lock_since = now
            else:
                self._lock_since = None
            lock_held = self._lock_since is not None and (now - self._lock_since) >= self.lock_hold_seconds

            event_type: Optional[str] = None
            if line_held:
                event_type = "UNNATURAL_GEOMETRIC_LINE"
            if lock_held:
                event_type = "MECHANICAL_LOCK_NO_TREMOR"

            base = {
                "point": point,
                "velocity": mean_velocity,
                "straightness": straightness,
                "tremor_variance": tremor_variance,
                "zero_tremor_streak": self.zero_tremor_streak,
                "flow_response": response,
                "max_step": max_step,
                "last_dx": dx,
                "last_dy": dy,
                "last_step": last_step,
                "jerk_variance": jerk_variance,
                "line_seconds": (now - self._line_since) if self._line_since is not None else 0.0,
                "path": coords.tolist(),
                "residuals": residuals.tolist(),
            }

            if event_type is None:
                self.last_metrics = {"flagged": False, **base}
                return None

            confidence = min(
                1.0,
                max(
                    straightness,
                    mean_velocity / max(self.velocity_threshold, 1e-4),
                ),
            )
            self.last_metrics = {
                "flagged": True,
                "event_type": event_type,
                "confidence": confidence,
                **base,
            }
            return self.last_metrics
