from __future__ import annotations

import os
from typing import Any

import cv2
import numpy as np
import onnxruntime as ort

# Inference shares the machine with the capture reader, renderer, analysis
# worker, ffmpeg and the UI thread. ORT's default (one intra-op thread per
# physical core) saturates every core during each detection pass, which shows
# up as stutter in the live picture. The CPU pool therefore takes about half
# the logical cores minus a margin, and never more than 8 (measured: 960
# input 79 ms/pass at 4 threads, 63 ms at 8 on a 20-thread desktop).
_MIN_THREADS = 2
_MAX_THREADS = 8
# With onnxruntime-directml installed the model runs on any DirectX 12 GPU:
# measured 3.8 ms/pass at 640 and 8.1 ms at 960 on the development machine,
# against 29 / 67 ms on its CPU. "auto" uses it when present, else the CPU.
_DML_PROVIDER = "DmlExecutionProvider"
_CPU_PROVIDER = "CPUExecutionProvider"


def auto_threads(cpu_count: int | None) -> int:
    cores = int(cpu_count or 4)
    return max(_MIN_THREADS, min(_MAX_THREADS, cores // 2 - 2))


class PixelVisionObjectDetector:
    def __init__(
        self,
        model_path: str = "data/models/yolov8n.onnx",
        conf_threshold: float = 0.35,
        nms_threshold: float = 0.45,
        player_class_ids: list[int] | None = None,
        input_size: int | None = None,
        provider: str = "auto",
        threads: int | None = None,
    ):
        self.model_path = self._resolve_model_path(model_path)
        # "auto": GPU (DirectML) when onnxruntime-directml is installed, else
        # CPU. "cpu" / "directml" force one; a forced GPU that fails to open
        # falls back to the CPU with a warning rather than losing detection.
        self.provider_pref = str(provider or "auto").strip().lower()
        self.threads = int(threads) if threads else auto_threads(os.cpu_count())
        self.provider_name = ""
        # Letterbox size. A model exported with a fixed input (the normal
        # case) dictates it; `input_size` only matters for a dynamic-shape
        # export. Exporting at 960 instead of 640 (tools/export_player_model.py
        # --imgsz 960) keeps distant players above the detector's size floor
        # at ~2.3x the CPU cost per pass.
        self.input_w = self.input_h = int(input_size or 640)
        self.conf_threshold = conf_threshold
        self.nms_threshold = nms_threshold
        self.player_class_ids = player_class_ids if player_class_ids is not None else []
        if not self.player_class_ids:
            print(
                "[DETECTOR] [WARN] player_class_ids is empty -- detection is filter-closed "
                "and will match zero classes until configured (e.g. [0] for 'person')."
            )
        self.ort_session = None
        self.active_tracks: dict[int, tuple[int, int, int, int, int]] = {}
        self.track_miss_streak: dict[int, int] = {}
        self.track_counter = 0
        self._model_warning_emitted = False
        self._output_layout: str | None = None
        self._load_onnx_model()

    @staticmethod
    def _resolve_model_path(model_path: str) -> str:
        candidates = [
            model_path,
            "data/models/yolov8n.onnx",
        ]
        seen: set[str] = set()
        for path in candidates:
            if not path or path in seen:
                continue
            seen.add(path)
            if os.path.isfile(path):
                return path
        return model_path

    def _provider_order(self) -> list[str]:
        available = set(ort.get_available_providers())
        wants_gpu = self.provider_pref in ("auto", "directml", "dml", "gpu")
        order: list[str] = []
        if wants_gpu and _DML_PROVIDER in available:
            order.append(_DML_PROVIDER)
        elif wants_gpu and self.provider_pref != "auto":
            print(
                "[DETECTOR] [WARN] DirectML requested but onnxruntime-directml is not installed "
                "(pip uninstall -y onnxruntime && pip install onnxruntime-directml); using the CPU."
            )
        order.append(_CPU_PROVIDER)
        return order

    def describe(self) -> str:
        """Short label for the UI, e.g. '960 GPU' or '640 CPU'; empty without a model."""
        if self.ort_session is None:
            return ""
        return f"{self.input_w} {self.provider_name}"

    def _load_onnx_model(self) -> None:
        last_error: Exception | None = None
        for provider in self._provider_order():
            try:
                session_options = ort.SessionOptions()
                session_options.inter_op_num_threads = 1
                session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                if provider == _DML_PROVIDER:
                    # Required by the DirectML execution provider.
                    session_options.enable_mem_pattern = False
                    providers = [_DML_PROVIDER, _CPU_PROVIDER]
                else:
                    session_options.intra_op_num_threads = self.threads
                    providers = [_CPU_PROVIDER]
                session = ort.InferenceSession(self.model_path, sess_options=session_options, providers=providers)
                output_shape = session.get_outputs()[0].shape
                if len(output_shape) != 3:
                    raise ValueError(
                        f"Expected rank-3 YOLO output, got rank {len(output_shape)} "
                        f"(shape {output_shape}). Wrong model file?"
                    )
                input_shape = session.get_inputs()[0].shape
                if len(input_shape) == 4 and isinstance(input_shape[2], int) and isinstance(input_shape[3], int):
                    self.input_h, self.input_w = int(input_shape[2]), int(input_shape[3])
                self.ort_session = session
                self.provider_name = "GPU" if provider == _DML_PROVIDER else "CPU"
                print(
                    f"[DETECTOR] [SUCCESS] Target bounding box detector model loaded: {self.model_path} "
                    f"(input {self.input_w}x{self.input_h}, {self.provider_name}"
                    f"{'' if self.provider_name == 'GPU' else f', {self.threads} threads'})"
                )
                return
            except Exception as exc:
                last_error = exc
                if provider != _CPU_PROVIDER:
                    print(f"[DETECTOR] [WARN] {provider} could not open the model ({exc}); trying the CPU.")
        print(
            f"[DETECTOR] [WARN] Object detection model absent at {self.model_path}. "
            f"Simulating fallback tracker. ({last_error})"
        )
        self.ort_session = None
        self.provider_name = ""

    def _detect_output_layout(self, predictions: np.ndarray) -> str:
        if predictions.ndim != 3 or predictions.shape[0] != 1:
            return "unknown"
        axis1_size, axis2_size = predictions.shape[1], predictions.shape[2]
        if axis1_size > axis2_size:
            return "yolov5"
        else:
            return "yolov8"

    def _decode_predictions(self, predictions: np.ndarray) -> list[tuple[float, float, float, float, float, int]]:
        """Candidate boxes above the confidence floor for the wanted classes.

        Vectorised: a 960 model emits 18,900 candidates per frame and the
        old per-row Python loop cost ~55 ms, more than the GPU pass itself.
        """
        layout = self._detect_output_layout(predictions)
        if layout == "unknown" or not self.player_class_ids:
            return []

        if layout == "yolov5":
            rows = np.asarray(predictions[0], dtype=np.float32)
            if rows.shape[1] < 6:
                return []
            class_scores = rows[:, 5:]
            class_ids = class_scores.argmax(axis=1)
            confidence = rows[:, 4] * class_scores[np.arange(rows.shape[0]), class_ids]
        else:
            rows = np.asarray(predictions[0], dtype=np.float32).T
            if rows.shape[1] < 5:
                return []
            class_scores = rows[:, 4:]
            class_ids = class_scores.argmax(axis=1)
            confidence = class_scores[np.arange(rows.shape[0]), class_ids]

        keep = (confidence > self.conf_threshold) & np.isin(class_ids, np.asarray(self.player_class_ids))
        if not keep.any():
            return []
        boxes = rows[keep, :4]
        return [
            (float(x), float(y), float(w), float(h), float(c), int(k))
            for (x, y, w, h), c, k in zip(boxes, confidence[keep], class_ids[keep])
        ]

    def _apply_letterbox(self, frame: np.ndarray) -> tuple[np.ndarray, float, int, int]:
        height, width = frame.shape[:2]
        in_w, in_h = self.input_w, self.input_h
        scale = min(in_w / width, in_h / height)
        new_w = max(1, int(width * scale))
        new_h = max(1, int(height * scale))

        resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        pad_x = (in_w - new_w) // 2
        pad_y = (in_h - new_h) // 2

        padded = np.full((in_h, in_w, 3), 114, dtype=np.uint8)
        padded[pad_y : pad_y + new_h, pad_x : pad_x + new_w] = resized

        return padded, scale, pad_x, pad_y

    def _rescale_boxes(
        self,
        detections: list[tuple[int, int, int, int, float, int]],
        scale: float,
        pad_x: int,
        pad_y: int,
        orig_width: int,
        orig_height: int,
    ) -> list[tuple[int, int, int, int, float, int]]:
        rescaled: list[tuple[int, int, int, int, float, int]] = []
        for x_center, y_center, box_w, box_h, conf, class_id in detections:
            x_center_scaled = (x_center - pad_x) / scale
            y_center_scaled = (y_center - pad_y) / scale
            box_w_scaled = box_w / scale
            box_h_scaled = box_h / scale

            x1 = max(0, int(x_center_scaled - box_w_scaled / 2))
            y1 = max(0, int(y_center_scaled - box_h_scaled / 2))
            x2 = min(orig_width - 1, int(x_center_scaled + box_w_scaled / 2))
            y2 = min(orig_height - 1, int(y_center_scaled + box_h_scaled / 2))

            rescaled.append((x1, y1, x2, y2, conf, class_id))

        return rescaled

    def _apply_nms(
        self, boxes: list[tuple[int, int, int, int, float, int]]
    ) -> list[tuple[int, int, int, int, float, int]]:
        if not boxes:
            return []

        boxes_arr = np.array([[b[0], b[1], b[2] - b[0], b[3] - b[1]] for b in boxes], dtype=np.float32)
        confidences = np.array([b[4] for b in boxes], dtype=np.float32)

        keep_indices = cv2.dnn.NMSBoxes(boxes_arr.tolist(), confidences.tolist(), self.conf_threshold, self.nms_threshold)

        if isinstance(keep_indices, np.ndarray):
            keep_indices = keep_indices.flatten().tolist()
        elif not isinstance(keep_indices, list):
            keep_indices = []

        kept = [boxes[i] for i in keep_indices]
        return sorted(kept, key=lambda b: b[4], reverse=True)

    def _calculate_iou(self, box_a: tuple[int, int, int, int], box_b: tuple[int, int, int, int]) -> float:
        x_a = max(box_a[0], box_b[0])
        y_a = max(box_a[1], box_b[1])
        x_b = min(box_a[2], box_b[2])
        y_b = min(box_a[3], box_b[3])

        inter_area = max(0, x_b - x_a + 1) * max(0, y_b - y_a + 1)
        box_a_area = (box_a[2] - box_a[0] + 1) * (box_a[3] - box_a[1] + 1)
        box_b_area = (box_b[2] - box_b[0] + 1) * (box_b[3] - box_b[1] + 1)
        return inter_area / float(box_a_area + box_b_area - inter_area)

    def detect_and_track(self, frame: np.ndarray) -> list[dict[str, Any]]:
        if self.ort_session is None:
            if not self._model_warning_emitted:
                print("YOLO MODEL ABSENT - INITIALIZE OBJECT DETECTOR WEIGHTS TO SCAN PLAYERS.")
                self._model_warning_emitted = True
            return []

        orig_height, orig_width = frame.shape[:2]
        try:
            padded_frame, scale, pad_x, pad_y = self._apply_letterbox(frame)
            blob = padded_frame.transpose((2, 0, 1))
            blob = np.expand_dims(blob, axis=0).astype(np.float32) / 255.0

            inputs = {self.ort_session.get_inputs()[0].name: blob}
            outputs = self.ort_session.run(None, inputs)
            predictions = np.asarray(outputs[0])

            raw_detections = self._decode_predictions(predictions)
            rescaled_detections = self._rescale_boxes(
                raw_detections, scale, pad_x, pad_y, orig_width, orig_height
            )
            fresh_detections = self._apply_nms(rescaled_detections)

        except Exception as exc:
            print(f"[DETECTOR] [WARN] Detection inference failed: {exc}")
            return []

        candidate_tracks = set(self.active_tracks.keys())
        updated_tracks: dict[int, tuple[int, int, int, int, int]] = {}
        tracked_entities: list[dict[str, Any]] = []

        for x1, y1, x2, y2, conf, class_id in fresh_detections:
            matched_id = None
            best_iou = 0.30

            for track_id in sorted(candidate_tracks):
                last_box = self.active_tracks[track_id][:4]
                iou = self._calculate_iou((x1, y1, x2, y2), last_box)
                if iou > best_iou:
                    best_iou = iou
                    matched_id = track_id

            if matched_id is not None:
                candidate_tracks.remove(matched_id)
                self.track_miss_streak[matched_id] = 0
            else:
                self.track_counter += 1
                matched_id = self.track_counter
                self.track_miss_streak[matched_id] = 0

            bbox = (x1, y1, x2, y2)
            center_x = (x1 + x2) // 2
            center_y = (y1 + y2) // 2

            updated_tracks[matched_id] = (x1, y1, x2, y2, class_id)
            tracked_entities.append(
                {
                    "track_id": matched_id,
                    "bbox": bbox,
                    "center": (center_x, center_y),
                    "confidence": conf,
                    "class_id": class_id,
                }
            )

        for unmatched_id in candidate_tracks:
            self.track_miss_streak[unmatched_id] = self.track_miss_streak.get(unmatched_id, 0) + 1
            if self.track_miss_streak[unmatched_id] <= 5:
                updated_tracks[unmatched_id] = self.active_tracks[unmatched_id]

        for track_id in list(self.track_miss_streak.keys()):
            if track_id not in updated_tracks:
                del self.track_miss_streak[track_id]

        self.active_tracks = updated_tracks
        return tracked_entities
