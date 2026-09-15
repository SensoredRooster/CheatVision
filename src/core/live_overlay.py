from __future__ import annotations

from typing import Any, Optional

import cv2
import numpy as np

# OpenCV draws in BGR. Tracked players are steel so the only red on the
# picture is a flagged track, in the alert red of the app palette
# (src/ui/theme.py, assets/brand/BRAND.md).
_TRACK_COLOR = (227, 222, 217)  # #D9DEE3
_FLAG_COLOR = (87, 77, 255)  # #FF4D57 ALERT
_TEXT_ON_TRACK = (39, 35, 30)  # #1E2327 charcoal, readable on the steel label
_TEXT_ON_FLAG = (255, 255, 255)


class PixelVisionLiveOverlay:
    def __init__(self):
        self.box_color = _TRACK_COLOR
        self.alert_color = _FLAG_COLOR
        self.text_color = _TEXT_ON_FLAG
        self.line_thickness = 2
        self.font = cv2.FONT_HERSHEY_SIMPLEX

    def render_overlays(
        self,
        frame: np.ndarray,
        tracked_entities: list[dict[str, Any]],
        flagged_id: Optional[int] = None,
    ) -> np.ndarray:
        overlay_canvas = frame

        for entity in tracked_entities:
            track_id = entity.get("track_id")
            bbox = entity.get("bbox", (0, 0, 0, 0))
            confidence = float(entity.get("confidence", 0.0))

            x1, y1, x2, y2 = bbox
            is_flagged = track_id == flagged_id
            active_color = self.alert_color if is_flagged else self.box_color
            text_color = self.text_color if is_flagged else _TEXT_ON_TRACK

            cv2.rectangle(overlay_canvas, (x1, y1), (x2, y2), active_color, self.line_thickness)

            label_text = f"CV #{track_id} {confidence * 100:.0f}%"
            (width, height), _ = cv2.getTextSize(label_text, self.font, 0.4, 1)
            cv2.rectangle(overlay_canvas, (x1, max(0, y1 - 20)), (x1 + width + 6, y1), active_color, -1)
            cv2.putText(
                overlay_canvas,
                label_text,
                (x1 + 3, max(0, y1 - 6)),
                self.font,
                0.4,
                text_color,
                1,
                cv2.LINE_AA,
            )

        return overlay_canvas
