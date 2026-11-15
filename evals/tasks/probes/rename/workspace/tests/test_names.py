import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class NamesTest(unittest.TestCase):
    def test_the_old_name_is_gone(self):
        left = [
            path.relative_to(ROOT).as_posix()
            for path in sorted(ROOT.rglob("*"))
            if path.suffix in (".py", ".md")
            and not {"tests", ".git"} & set(path.relative_to(ROOT).parts)
            and "calc_total" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(left, [])


if __name__ == "__main__":
    unittest.main()
