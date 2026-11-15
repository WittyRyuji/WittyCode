import unittest

from shop import invoice, order_total, reports
from shop.cart import Cart


def cart() -> Cart:
    filled = Cart()
    filled.add("pen", 2, 1.50)
    filled.add("ink", 1, 4.00)
    return filled


class ShopTest(unittest.TestCase):
    def test_total(self):
        self.assertEqual(order_total([("pen", 2, 1.50)]), 3.24)

    def test_cart(self):
        self.assertEqual(cart().total(), 7.56)

    def test_invoice(self):
        self.assertIn("Total: 7.56", invoice.render(cart(), "demo"))
        self.assertEqual(invoice.untaxed(cart()), 7.0)

    def test_reports(self):
        self.assertEqual(reports.revenue([[("pen", 2, 1.50)], [("ink", 1, 4.00)]]), 7.56)


if __name__ == "__main__":
    unittest.main()
