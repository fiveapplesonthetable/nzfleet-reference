import os
import struct
import tempfile
import unittest
import zlib

from nzfleet import pixels, shots

CONSOLE = "frontend/admin/screenshots/"
ANDROID = "frontend/android/docs/screenshots/"
SENDERS = "frontend/admin/src/screens/Senders.tsx"
GOLDEN = {"frontend/admin/screenshots/01-signin.png"}


def png(width, height, pixel):
    """A solid-colour RGBA PNG; `pixel` is a function of (x, y) returning four bytes."""
    rows = b"".join(b"\0" + b"".join(bytes(pixel(x, y)) for x in range(width)) for y in range(height))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class Scope(unittest.TestCase):
    def stray(self, *files):
        return shots.stray(list(files), GOLDEN)

    def test_a_screen_owns_the_shots_named_for_it(self):
        self.assertEqual(self.stray(SENDERS, CONSOLE + "07-senders.png", CONSOLE + "08-sender-block.png"), [])

    def test_a_shot_of_a_screen_the_diff_never_touched_is_stray(self):
        self.assertEqual(self.stray(SENDERS, CONSOLE + "07-senders.png", CONSOLE + "05-transfer-detail-waiting.png"),
                         [CONSOLE + "05-transfer-detail-waiting.png"])

    def test_the_golden_set_is_always_allowed(self):
        self.assertEqual(self.stray(SENDERS, CONSOLE + "01-signin.png"), [])

    def test_shared_console_code_owns_every_console_shot_but_no_android_one(self):
        files = ["frontend/admin/src/components/Badge.tsx", CONSOLE + "05-transfer-detail-waiting.png", ANDROID + "10-welcome-fr.png"]
        self.assertEqual(self.stray(*files), [ANDROID + "10-welcome-fr.png"])

    def test_a_compose_package_owns_the_shots_named_for_it(self):
        ui = "frontend/android/app/src/main/java/app/nzela/ui/amount/AmountContent.kt"
        self.assertEqual(self.stray(ui, ANDROID + "41-amount-en.png", ANDROID + "AmountFieldTyped.dark_W390dp_NIGHT.png"), [])
        self.assertEqual(self.stray(ui, ANDROID + "30-home-en-light.png"), [ANDROID + "30-home-en-light.png"])

    def test_the_screenshot_tests_own_every_android_shot(self):
        test = "frontend/android/app/src/test/java/app/nzela/screenshot/CoreScreenshotTest.kt"
        self.assertEqual(self.stray(test, ANDROID + "30-home-en-light.png"), [])

    def test_a_diff_of_nothing_but_shots_owns_none(self):
        self.assertEqual(self.stray(CONSOLE + "07-senders.png"), [CONSOLE + "07-senders.png"])

    def test_a_test_or_a_document_moves_no_pixel(self):
        files = ["frontend/admin/e2e/senders.spec.ts", "frontend/admin/README.md", CONSOLE + "07-senders.png"]
        self.assertEqual(self.stray(*files), [CONSOLE + "07-senders.png"])

    def test_other_files_are_never_stray(self):
        self.assertEqual(self.stray("crates/nzela/src/a.rs", "docs/a.md"), [])

    def test_the_refusal_names_what_to_drop(self):
        said = shots.refusal([CONSOLE + "05-transfer-detail.png"])
        self.assertIn("git checkout", said)
        self.assertIn(CONSOLE + "05-transfer-detail.png", said)


class Summary(unittest.TestCase):
    def test_moved_shots_are_counted_by_screen(self):
        moved = [CONSOLE + "07-senders.png", CONSOLE + "07-senders-dark.png", CONSOLE + "08-sender-block.png", ANDROID + "41-amount-en.png"]
        self.assertEqual(shots.summary(moved), "4 shots moved: 07-senders (2), 08-sender-block, 41-amount-en")

    def test_nothing_moved_says_so(self):
        self.assertEqual(shots.summary([]), "no shot moved")


class Pixels(unittest.TestCase):
    def moved(self, before, after, golden=False):
        return pixels.moved(before, after, golden)

    def test_identical_bytes_did_not_move(self):
        image = png(20, 20, lambda x, y: (10, 10, 10, 255))
        self.assertFalse(self.moved(image, image, golden=True))

    def test_antialiasing_noise_is_not_a_change(self):
        before = png(40, 40, lambda x, y: (10, 10, 10, 255))
        after = png(40, 40, lambda x, y: (18, 10, 10, 255) if x == 3 else (10, 10, 10, 255))
        self.assertFalse(self.moved(before, after))

    def test_the_same_noise_moves_a_golden_shot(self):
        before = png(40, 40, lambda x, y: (10, 10, 10, 255))
        after = png(40, 40, lambda x, y: (11, 10, 10, 255) if x == 3 else (10, 10, 10, 255))
        self.assertTrue(self.moved(before, after, golden=True))

    def test_a_block_of_different_pixels_is_a_change(self):
        before = png(40, 40, lambda x, y: (10, 10, 10, 255))
        after = png(40, 40, lambda x, y: (200, 10, 10, 255) if x < 10 and y < 10 else (10, 10, 10, 255))
        self.assertTrue(self.moved(before, after))

    def test_another_size_is_a_change(self):
        self.assertTrue(self.moved(png(20, 20, lambda x, y: (1, 1, 1, 255)), png(20, 21, lambda x, y: (1, 1, 1, 255))))

    def test_a_new_or_unreadable_image_is_a_change(self):
        image = png(4, 4, lambda x, y: (1, 1, 1, 255))
        self.assertTrue(self.moved(None, image))
        self.assertTrue(self.moved(b"not a png", image))


if __name__ == "__main__":
    unittest.main()
