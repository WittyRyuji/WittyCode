"""A long module with one wrong line in the middle. What it asks of a harness: find the place without reading 1,400
lines (the failing test names the rule), change that line, and leave every other line as it was. The check compares the
rest of the file with what was given, so a file written back with a part missing does not pass."""

from __future__ import annotations

import json
import re
from pathlib import Path

RULES = 180
WRONG = 97
_DEF = re.compile(r"^def (rule_\d{3})\(", re.MULTILINE)

_HEAD = '''"""Volume pricing: one rule per price tier.

Every rule takes the quantity ordered and the price of one unit (in cents) and returns the price of the order: a
discount from a tier's threshold on, and never more than the tier's cap.
"""

from __future__ import annotations

'''

_TEST = '''import json
import unittest
from pathlib import Path

from pricing import rules

CASES = json.loads((Path(__file__).parent / "cases.json").read_text(encoding="utf-8"))


class RulesTest(unittest.TestCase):
    def test_every_rule(self):
        for name, quantity, unit_price, expected in CASES:
            with self.subTest(rule=name, quantity=quantity, unit_price=unit_price):
                self.assertEqual(getattr(rules, name)(quantity, unit_price), expected)


if __name__ == "__main__":
    unittest.main()
'''


def _tier(number: int) -> tuple[int, int, int]:
    """The threshold, the percent and the cap of a tier."""
    return 5 + number * 7 % 40, 5 + number * 3 % 30, 2000 + number * 137 % 5000


def _rule(number: int, *, wrong: bool) -> str:
    threshold, percent, cap = _tier(number)
    return (
        f"def rule_{number:03d}(quantity: int, unit_price: int) -> int:\n"
        f'    """Tier {number:03d}: {percent}% off from {threshold} units on, never more than {cap}."""\n'
        f"    total = quantity * unit_price\n"
        f"    if quantity {'>' if wrong else '>='} {threshold}:\n"
        f"        total -= total * {percent} // 100\n"
        f"    return min(total, {cap})\n"
    )


def rules_text(*, wrong: int | None = WRONG) -> str:
    return _HEAD + "\n\n".join(_rule(number, wrong=number == wrong) for number in range(1, RULES + 1))


def _price(number: int, quantity: int, unit_price: int) -> int:
    threshold, percent, cap = _tier(number)
    total = quantity * unit_price
    if quantity >= threshold:
        total -= total * percent // 100
    return min(total, cap)


def _cases() -> list[list[object]]:
    cases: list[list[object]] = []
    for number in range(1, RULES + 1):
        threshold = _tier(number)[0]
        for quantity, unit_price in ((threshold, 3 + number % 9), (400 + number, 20)):
            cases.append([f"rule_{number:03d}", quantity, unit_price, _price(number, quantity, unit_price)])
    return cases


def build(root: Path) -> None:
    (root / "pricing").mkdir()
    (root / "pricing" / "__init__.py").write_text('"""Prices of orders."""\n', encoding="utf-8", newline="\n")
    (root / "pricing" / "rules.py").write_text(rules_text(), encoding="utf-8", newline="\n")
    (root / "cases.json").write_text(json.dumps(_cases()) + "\n", encoding="utf-8", newline="\n")
    (root / "test_rules.py").write_text(_TEST, encoding="utf-8", newline="\n")


def solve(root: Path) -> None:
    (root / "pricing" / "rules.py").write_text(rules_text(wrong=None), encoding="utf-8", newline="\n")


def _blocks(text: str) -> dict[str, str]:
    """The module's head and each rule, by name, without regard to line ends or blank lines between rules."""
    text = text.replace("\r\n", "\n")
    marks = list(_DEF.finditer(text))
    blocks = {"": text[: marks[0].start()].strip() if marks else text.strip()}
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        blocks[mark.group(1)] = text[mark.start() : end].strip()
    return blocks


def check(root: Path, run) -> str:
    code, output = run(["python", "-m", "unittest", "-q"])
    if code:
        return output
    given, now = _blocks(rules_text()), _blocks((root / "pricing" / "rules.py").read_text(encoding="utf-8"))
    fixed = f"rule_{WRONG:03d}"
    if list(given) != list(now):
        return f"pricing/rules.py no longer holds the same rules in the same order ({len(now) - 1} rules, {len(given) - 1} were given)."
    touched = [name or "the module's head" for name in given if name != fixed and given[name] != now[name]]
    return f"Other parts of pricing/rules.py were changed: {', '.join(touched[:8])}." if touched else ""
