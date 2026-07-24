import base64
import os
import tempfile
import unittest
from pathlib import Path

from embodied_ha import content_blocks


JPEG = b"\xff\xd8\xff\xe0fixture"
PNG = b"\x89PNG\r\n\x1a\nfixture"


def image_block(data: bytes, media_type: str) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


class ContentBlockExpansionTests(unittest.TestCase):
    def test_preserves_two_image_label_order_for_both_harnesses(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "eha-content-test"
            images = content_blocks.expand_content_blocks(
                [
                    {"type": "text", "text": "台所:"},
                    image_block(JPEG, "image/jpeg"),
                    {"type": "text", "text": "玄関:"},
                    image_block(PNG, "image/png"),
                ],
                output,
            )

            self.assertEqual([path.name for path in images], ["image-001.jpg", "image-002.png"])
            codex = (output / "codex-prompt.txt").read_text()
            agy = (output / "agy-prompt.txt").read_text()
            self.assertLess(codex.index("台所:"), codex.index("【画像1】"))
            self.assertLess(codex.index("【画像1】"), codex.index("玄関:"))
            self.assertLess(codex.index("玄関:"), codex.index("【画像2】"))
            self.assertIn(f"@{images[0]}", agy)
            self.assertIn(f"@{images[1]}", agy)
            self.assertEqual(images[0].read_bytes(), JPEG)
            self.assertEqual(images[1].read_bytes(), PNG)

    def test_rejects_invalid_base64_and_removes_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "eha-content-invalid"
            block = image_block(JPEG, "image/jpeg")
            block["source"]["data"] = "not-base64!"
            with self.assertRaisesRegex(ValueError, "invalid base64"):
                content_blocks.expand_content_blocks([block], output)
            self.assertFalse(output.exists())

    def test_rejects_mismatched_media_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "eha-content-mismatch"
            with self.assertRaisesRegex(ValueError, "does not match"):
                content_blocks.expand_content_blocks(
                    [image_block(PNG, "image/jpeg")], output
                )

    def test_rejects_unsupported_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "eha-content-unsupported"
            with self.assertRaisesRegex(ValueError, "unsupported type"):
                content_blocks.expand_content_blocks(
                    [{"type": "document", "source": {}}], output
                )

    def test_rejects_more_than_eight_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "eha-content-too-many"
            with self.assertRaisesRegex(ValueError, "more than 8 images"):
                content_blocks.expand_content_blocks(
                    [image_block(JPEG, "image/jpeg") for _ in range(9)], output
                )

    def test_rejects_text_over_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "eha-content-text-limit"
            with self.assertRaisesRegex(ValueError, "content text exceeds"):
                content_blocks.expand_content_blocks(
                    [{"type": "text", "text": "x" * (content_blocks.MAX_TEXT_BYTES + 1)}],
                    output,
                )

    def test_cleanup_stale_removes_only_expired_matching_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale = root / "eha-content-stale"
            fresh = root / "eha-content-fresh"
            unrelated = root / "other"
            stale.mkdir()
            fresh.mkdir()
            unrelated.mkdir()
            os.utime(stale, (1, 1))
            os.utime(fresh, (9_999, 9_999))
            os.utime(unrelated, (1, 1))

            removed = content_blocks.cleanup_stale(root, now=10_000)

            self.assertEqual(removed, 1)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
