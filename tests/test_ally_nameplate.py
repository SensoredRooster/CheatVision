from pathlib import Path
import cv2
import pytest

from src.core.anti_cheat_pipeline import AntiCheatPipeline

INCIDENT = Path("data/incidents/20260917-002814_sticky_aim_fr185412/snapshot.png")

@pytest.mark.skipif(not INCIDENT.exists(), reason="local incident snapshot not in checkout")
def test_ally_nameplate_blocks_sticky_on_jacrispy_incident():
    img = cv2.imread(str(INCIDENT))
    assert img is not None
    pipe = AntiCheatPipeline(game_profile="warzone")
    bbox = (485, 227, 571, 439)
    assert pipe._looks_like_ally(img, bbox) is True
    entities = [{"track_id": 2046, "bbox": bbox, "center": (528, 333), "confidence": 0.85, "class_id": 0}]
    metrics = {"last_dx": 8.0, "last_dy": 2.0, "last_step": 9.6, "velocity": 9.6, "path": [[0, 0], [4, 1], [8, 2]], "_timestamp": 1.0}
    for i in range(12):
        metrics["_timestamp"] = 1.0 + i * 0.05
        assert pipe._score_replica_aim(metrics, entities, (479, 269), 960, img) is None
    assert pipe._sticky_hits.get(2046, 0) == 0
