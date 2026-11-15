"""Invoices as text."""

from shop import pricing
from shop.cart import Cart


def render(cart: Cart, customer: str) -> str:
    lines = [f"Invoice for {customer}"]
    lines += [f"{quantity} x {name} @ {price:.2f}" for name, quantity, price in cart.lines]
    lines.append(f"Total: {pricing.calc_total(cart.lines):.2f}")
    return "\n".join(lines)


def untaxed(cart: Cart) -> float:
    return pricing.calc_total(cart.lines, tax_rate=0.0)
