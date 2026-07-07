import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

from room_graph import alias_map, resolve_room  # noqa: E402
from state_utils import clean  # noqa: E402


def fixture_graph() -> dict[str, Any]:
    return {
        "rooms": {
            "study": {"display_name": "スタディ", "tags": ["bedroom", "private", "study"]},
            "bedroom": {"display_name": "ベッドルーム", "tags": ["bedroom"]},
            "kids_room": {"display_name": "キッズルーム", "tags": ["bedroom", "private"]},
            "kitchen": {"display_name": "台所", "tags": ["cooking"]},
            "living_room": {"display_name": "リビング", "tags": ["living", "hub"]},
        },
        "aliases_pending": {
            "living_room": ["居間", "LDK"],
            "kitchen": ["キッチン"],
        },
    }


def old_body_context_resolve_room(value: Any, graph: dict[str, Any]) -> str | None:
    key = clean(value)
    if not key:
        return None
    room_map = {clean(k): v for k, v in graph.get("rooms", {}).items() if clean(k) and isinstance(v, dict)}
    if key in room_map:
        return key
    lowered = key.lower()
    for room_id, item in room_map.items():
        if clean(item.get("display_name")).lower() == lowered:
            return room_id
    aliases = graph.get("aliases_pending")
    if isinstance(aliases, dict):
        for room_id, values in aliases.items():
            canonical = clean(room_id)
            if canonical not in room_map or not isinstance(values, list):
                continue
            if any(clean(alias).lower() == lowered for alias in values):
                return canonical
    return None


class RoomGraphTests(unittest.TestCase):
    def test_alias_map_rejects_colliding_tags(self):
        graph = fixture_graph()

        self.assertNotIn("bedroom", alias_map(graph))
        self.assertNotIn("private", alias_map(graph))
        self.assertEqual(resolve_room("bedroom", graph), "bedroom")
        self.assertIsNone(resolve_room("private", graph))

    def test_unique_tags_resolve_to_room_ids(self):
        graph = fixture_graph()

        self.assertEqual(resolve_room("study", graph), "study")
        self.assertEqual(resolve_room("cooking", graph), "kitchen")
        self.assertEqual(resolve_room("living", graph), "living_room")

    def test_display_names_and_pending_aliases_resolve(self):
        graph = fixture_graph()

        self.assertEqual(resolve_room("リビング", graph), "living_room")
        self.assertEqual(resolve_room("居間", graph), "living_room")
        self.assertEqual(resolve_room("LDK", graph), "living_room")

    def test_old_body_context_logic_diff_is_limited_to_colliding_room_id(self):
        graph = fixture_graph()
        values: list[str] = []
        for room_id, room in graph["rooms"].items():
            values.append(room_id)
            values.append(room["display_name"])
        for aliases in graph["aliases_pending"].values():
            values.extend(aliases)

        diffs = {
            value: (old_body_context_resolve_room(value, graph), resolve_room(value, graph))
            for value in values
            if old_body_context_resolve_room(value, graph) != resolve_room(value, graph)
        }
        self.assertEqual(diffs, {})


if __name__ == "__main__":
    unittest.main()
