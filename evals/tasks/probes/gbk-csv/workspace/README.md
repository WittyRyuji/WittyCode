# Warehouse stock

`data/stock.csv` is exchanged with a legacy warehouse system. It must stay as that system writes it:

- GBK-encoded text (not UTF-8),
- CRLF line ends, the last record included,
- one record per line (`品名,库存,单位`), no blank lines.

`stock.py` reads it. The tests: `python -m unittest -q`
