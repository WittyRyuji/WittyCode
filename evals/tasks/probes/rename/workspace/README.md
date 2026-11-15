# shop

A tiny shop: prices, carts, invoices and daily figures.

```python
from shop import calc_total

calc_total([("pen", 2, 1.50)])  # 3.24
```

`calc_total` takes `(name, quantity, unit price)` lines and returns their total, tax included.

Tests: `python -m unittest discover -s tests -q`
