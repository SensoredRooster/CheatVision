from pathlib import Path
import cv2
import pytest

from src.core import anti_cheat_pipeline as acp
from src.core.anti_cheat_pipeline import AntiCheatPipeline

INCIDENT = Path("data/incidents/20260917-002814_sticky_aim_fr185412/snapshot.png")


def test_ally_nameplate_gate_is_disabled():
    assert acp._ALLY_NAMEPLATE_GATE is False


@pytest.mark.skipif(not INCIDENT.exists(), reason="local incident snapshot not in checkout")
def test_looks_like_ally_returns_false_while_gate_off():
    img = cv2.imread(str(INCIDENT))
    assert img is not None
    pipe = AntiCheatPipeline(game_profile="warzone")
    bbox = (485, 227, 571, 439)
    assert pipe._looks_like_ally(img, bbox) is False
