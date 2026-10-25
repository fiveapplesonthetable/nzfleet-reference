import os
import subprocess
import tempfile
import unittest

from nzfleet import book_map


class BookMap(unittest.TestCase):
    def repo(self, chapters, files):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "docs/book"))
        for name, text in {**chapters, **files}.items():
            path = os.path.join(root, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as out:
                out.write(text)
        subprocess.run(["git", "init", "-q", root], check=True)
        subprocess.run(["git", "-C", root, "add", "-A"], check=True)
        return root

    def chapter(self, covers):
        return f"---\ncovers: [{covers}]\n---\ntext\n"

    def test_an_entry_covers_everything_under_a_directory(self):
        self.assertTrue(book_map.matches("crates/a", "crates/a/src/lib.rs"))

    def test_a_star_stays_inside_one_path_segment(self):
        self.assertTrue(book_map.matches("crates/a/payout*", "crates/a/payout_x.rs"))
        self.assertFalse(book_map.matches("crates/*", "other/a"))

    def test_a_changed_covered_path_reminds_of_its_chapter(self):
        root = self.repo({"docs/book/01-a.md": self.chapter("src")}, {"src/x.rs": ""})
        self.assertEqual(book_map.remind(root, ["src/x.rs"]),
                         ["this change may need docs/book/01-a.md updated (it covers src)"])

    def test_a_chapter_changed_in_the_same_diff_is_not_reminded(self):
        root = self.repo({"docs/book/01-a.md": self.chapter("src")}, {"src/x.rs": ""})
        self.assertEqual(book_map.remind(root, ["src/x.rs", "docs/book/01-a.md"]), [])

    def test_an_entry_that_matches_no_file_is_dead(self):
        root = self.repo({"docs/book/01-a.md": self.chapter("gone")}, {"src/x.rs": ""})
        self.assertEqual(book_map.dead(root), ["docs/book/01-a.md covers gone, which matches no tracked file"])

    def test_a_chapter_without_covers_is_dead(self):
        root = self.repo({"docs/book/01-a.md": "no front matter\n"}, {})
        self.assertEqual(book_map.dead(root), ["docs/book/01-a.md has no covers list in its front matter"])


if __name__ == "__main__":
    unittest.main()
