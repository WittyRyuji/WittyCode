"""Prices."""

TAX_RATE = 0.08


def calc_total(items: list[tuple[str, int, float]], tax_rate: float = TAX_RATE) -> float:
    """The total of (name, quantity, unit price) lines, tax included, rounded to cents."""
    subtotal = sum(quantity * price for _name, quantity, price in items)
    return round(subtotal * (1 + tax_rate), 2)
