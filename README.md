# Steam4CGS
Collects Steam game metadata in the format of the legacy `steamproducts2020.csv`
(read-only reference data). The column contract is documented in
[docs/legacy_schema.md](docs/legacy_schema.md) and enforced by
[crawler/schema.py](crawler/schema.py).

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

Tests never touch the network and verify the legacy CSV is unmodified.
