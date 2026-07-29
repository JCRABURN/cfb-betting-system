.PHONY: test verify-cfbd

YEAR ?= 2025
WEEK ?= 10

test:
	pytest -q

# One real call per CFBD endpoint fetch_stats.py/backfill_historical_stats.py use;
# checks the field names those scripts assume against the live response.
# Requires CFBD_API_KEY in .env or the environment.
verify-cfbd:
	python verify_cfbd_fields.py --year $(YEAR) --week $(WEEK)
