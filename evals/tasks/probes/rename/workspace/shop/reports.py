"""Daily figures."""

from shop.pricing import calc_total


def revenue(orders: list[list[tuple[str, int, float]]]) -> float:
    """The sum of calc_total over the day's orders."""
    return round(sum(calc_total(order) for order in orders), 2)
