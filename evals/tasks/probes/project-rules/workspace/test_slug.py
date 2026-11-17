import unittest

from textkit.words import slugify, words


class SlugTest(unittest.TestCase):
    def test_words_are_joined_by_hyphens(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_spaces_and_punctuation_collapse(self):
        self.assertEqual(slugify("  Rock & Roll -- 2024  "), "rock-roll-2024")

    def test_nothing_is_left(self):
        self.assertEqual(slugify("!!!"), "")

    def test_words_still_works(self):
        self.assertEqual(words("a b"), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
