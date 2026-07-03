import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audio_source_resolve_module():
    path = ROOT / "embodied_ha" / "audio_source_resolve.py"
    import sys

    sys.path.insert(0, str(ROOT / "embodied_ha"))
    spec = importlib.util.spec_from_file_location("audio_source_resolve_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AudioSourceResolveTests(unittest.TestCase):
    def setUp(self):
        self.resolver = load_audio_source_resolve_module()

    def test_physical_body_prefers_current_room_tcp_source(self):
        body_loc = {"current_entity": "", "current_room": "study"}
        sources = [
            {"source": "rtsp://example.local/living", "room": "living"},
            {"source": "tcp://192.168.1.50:3333", "room": "study"},
            {"source": "alsa://default", "room": "study"},
        ]
        self.assertEqual(self.resolver.resolve_audio_source(body_loc, sources), "tcp://192.168.1.50:3333")

    def test_tcp_body_prefers_same_host_source(self):
        body_loc = {"current_entity": "tcp://192.168.1.50:3333", "current_room": "study", "projected_room": "living"}
        sources = [
            {"source": "tcp://192.168.1.50:3333", "room": "study"},
            {"source": "rtsp://example.local/living", "room": "living"},
        ]
        self.assertEqual(self.resolver.resolve_audio_source(body_loc, sources), "tcp://192.168.1.50:3333")

    def test_room_miss_falls_back_to_first_source(self):
        body_loc = {"current_entity": "camera.kitchen", "current_room": "hallway", "projected_room": "kitchen"}
        sources = [
            {"source": "rtsp://example.local/first", "room": "living"},
            {"source": "rtsp://example.local/second", "room": "study"},
        ]
        self.assertEqual(self.resolver.resolve_audio_source(body_loc, sources), "rtsp://example.local/first")

