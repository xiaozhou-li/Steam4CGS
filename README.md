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

Steam API clients and their limits are described in [docs/steam_api.md](docs/steam_api.md).

```sh
export STEAM_API_KEY=...             # from https://steamcommunity.com/dev/apikey; never commit it
python -m crawler preflight          # offline configuration check
python -m crawler preflight --live   # at most 9 real requests for one app id
python -m crawler discover --limit 50  # discover new games (see docs/discovery.md)
python -m crawler extract --limit 25   # Store metadata for eligible games (see docs/metadata.md)
```
