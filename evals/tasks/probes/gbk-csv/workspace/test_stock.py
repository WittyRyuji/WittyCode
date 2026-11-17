import unittest

import stock


class StockTest(unittest.TestCase):
    def test_screwdrivers_were_restocked(self):
        self.assertEqual(stock.load()["螺丝刀"], 35)

    def test_wrenches_were_added(self):
        self.assertEqual(stock.load()["扳手"], 18)

    def test_nothing_else_changed(self):
        self.assertEqual(stock.total(), 623)


if __name__ == "__main__":
    unittest.main()
