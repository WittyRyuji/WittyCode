"""A cart collects lines; calc_total prices it."""

from shop.pricing import calc_total


class Cart:
    def __init__(self) -> None:
        self.lines: list[tuple[str, int, float]] = []

    def add(self, name: str, quantity: int, price: float) -> None:
        self.lines.append((name, quantity, price))

    def total(self) -> float:
        return calc_total(self.lines)
