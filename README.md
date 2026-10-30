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
The integrated crawler is described in [docs/pipeline.md](docs/pipeline.md); the 30-game pilot
in [docs/pilot/README.md](docs/pilot/README.md).

```sh
export STEAM_API_KEY=...                 # from https://steamcommunity.com/dev/apikey; never commit it
python -m crawler audit                  # read-only checks
python -m crawler preflight --live       # at most 8 real requests for one app id
python -m crawler run --limit 30         # discover -> crawl -> export -> validate
python -m crawler run --resume           # continue after an interruption or --limit
python -m crawler run --appids-file F    # seed mode: specific AppIDs, no key needed for discovery
```

Validation and reports:

```sh
python3.13 -m pytest                          # 664 offline tests (mocked Steam APIs, sockets blocked)
python3.13 -m crawler validate                # re-check the outputs against the contract
python3.13 -m crawler backtest --sample 25    # live: re-extract 25 original games, compare structure
python3.13 -m crawler quality --run-tests     # writes reports/quality_report.md
```

Documentation: [data dictionary](docs/data_dictionary.md), [methodology](docs/methodology.md),
[quality report](reports/quality_report.md).

Outputs: `steamproducts2020_2026_new.csv`, `steamproducts2020_2026_merged.csv`,
`steam_metadata_provenance.csv`, `crawl_manifest.json` and `work/failed_appids.csv`.
