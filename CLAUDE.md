# span-viewer: notes for coding agents

Read [README.md](README.md) first: what the app is, how it's configured, the API v1 contract.

## Layout

- `app/`: the FastAPI app. `main.py` (pages and exports), `api.py` (`/api/v1`), `queries.py` (all SQL), `config.py` (every setting), `live_store.py` (the 3-s poller behind `/live`, `panel_live` and `space_live`), `span_ebus.py` (the panel's MQTT broker) and `span_local.py` (the old v1 REST API, the fallback until SPAN retires it), `mqtt_publisher.py`, `nilm_fhmm.py`. Templates in `app/templates/`, Jinja2 + HTMX + ECharts, no build step.
- `ingester/`: `backfill.py` (SPAN cloud history, all four retention tiers), `ongoing.py` (the 10-minute ingest), `firmware_watch.py`.
- `db/schema/`: idempotent SQL, applied in order by `scripts/apply_schema.py` or `scripts/apply-schema.sh`. Nothing applies them automatically.
- `scripts/sample_data.py`: the synthetic house the tests, the compose quickstart and the screenshots use.

## Rules

- **Only the panel's own data.** No utility bills, weather or other sources: a page or endpoint that needs one belongs in another app. Cost is the optional flat rate (`config.flat_rate()` / `queries.current_rates()`, `None` when unset) and every `$` must disappear cleanly without it.
- **No host, name or address of any one deployment** in code, defaults, comments or commit messages. Settings come from `app/config.py`; circuit roles from `circuits.yaml`.
- **API v1 is a contract** (README): add, never rename or remove; breaking changes go to `/api/v2`.
- **Colour-blind safe.** Hue-distinct colours (blue, orange, yellow, magenta, violet), never red against green as the only signal, and every series or status also carries a glyph (■ ▲ ● ◆) or a word. Check a new chart in grayscale.
- **Signs:** circuit power is negative when consuming on both of SPAN's local APIs, and the eBus grid flow is positive when exporting; the snapshot `/live` and MQTT use is normalised to "positive = consuming" (see `span_local._shape()`).
- **Tests:** `ruff check .` and `pytest` against the sample data (see `.gitlab-ci.yml`). Add a smoke test for every new page or endpoint.
- A deployment's own notes can live in a git-ignored `CLAUDE.local.md`.
