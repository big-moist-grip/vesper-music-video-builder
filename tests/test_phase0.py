import unittest

from backend.routes import health_payload
from nodes import MusicVideoBuilder


class HealthPayloadTests(unittest.TestCase):
    def test_health_payload_has_required_values(self):
        self.assertEqual(
            health_payload(),
            {
                "ok": True,
                "service": "music-video-builder",
                "phase": 0,
            },
        )


class MusicVideoBuilderNodeTests(unittest.TestCase):
    def test_launcher_node_contract(self):
        self.assertEqual(MusicVideoBuilder.INPUT_TYPES(), {"required": {}})
        self.assertEqual(MusicVideoBuilder.RETURN_TYPES, ())
        self.assertEqual(MusicVideoBuilder.FUNCTION, "noop")
        self.assertEqual(MusicVideoBuilder.CATEGORY, "Music Video")
        self.assertEqual(MusicVideoBuilder().noop(), ())


if __name__ == "__main__":
    unittest.main()
