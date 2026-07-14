# Atlas Macro

Atlas Macro is a clean-room macro-research and AI supply-chain intelligence
platform. It reproduces the useful information architecture and analytical
workflows of a dense research terminal while using original presentation,
first-party calculations, traceable upstream sources, and explicit data-quality
labels. It does **not** copy protected pages, private APIs, proprietary reports,
or restricted datasets from another site.

The application is a Django 5.2 server-rendered site backed by PostgreSQL. Celery
and Redis run scheduled ingestion, validation, and snapshot publication. ECharts
and lightweight browser JavaScript provide the interactive charts and filters.

## Included product areas

- Daily thesis, evidence, invalidation conditions, triggers, regime ledger, and
  paginated daily reports.
- Cross-asset, rates, Federal Reserve, liquidity, economy, volatility, credit,
  positioning, options, and crypto-derivatives dashboards.
- Searchable news, research mentions, fund letters, and glossary entries. Only
  metadata, links, and original summaries are stored for third-party material.
- AI supply-chain map and graph, company profiles, model and coding-agent
  rankings, GitHub application radar, and glossary.
- Component-level provenance, observation timestamps, quality/fallback states,
  dynamic sitemap, robots policy, light/dark themes, and PWA offline fallback.

The offline seed command exists only for component and contract testing. Public
views reject its records, and production releases purge them before publishing
official snapshots. Missing licensed feeds render an explicit data-source or
procurement gap rather than an illustrative number.

## Quick start with Docker

Requirements: Docker Engine with Compose v2.

```bash
cp .env.example .env
docker compose build
docker compose up -d db redis
docker compose run --rm web python manage.py migrate
docker compose run --rm web python manage.py sync_data_requirements
docker compose run --rm web python manage.py refresh_official_data
# Initial Treasury history: keep each annual shard in its own bounded process.
current_year=$(date +%Y)
for year in $(seq $((current_year - 5)) "$current_year"); do
  docker compose run --rm web python manage.py refresh_treasury_curve_data --start-year "$year" --end-year "$year" --no-publish
done
docker compose run --rm web python manage.py refresh_treasury_curve_data --start-year "$current_year" --end-year "$current_year"
docker compose run --rm web python manage.py refresh_prates_data
docker compose run --rm web python manage.py refresh_h10_data
docker compose run --rm web python manage.py refresh_h41_data
docker compose run --rm web python manage.py refresh_h8_data
docker compose run --rm web python manage.py refresh_macro_data
docker compose run --rm web python manage.py refresh_cftc_data
docker compose run --rm web python manage.py refresh_berkshire_letters
docker compose run --rm web python manage.py sync_official_glossary
docker compose run --rm web python manage.py sync_ai_glossary_catalog
docker compose run --rm web python manage.py sync_ai_reference_catalog
docker compose run --rm web python manage.py sync_ai_supply_chain_catalog
docker compose up -d
```

The default configuration leaves `SEC_USER_AGENT` blank, so the base Quick
Start never makes an unidentified SEC request. After setting a real product
identity and monitored contact email, run the reviewed four-company refresh:

```bash
docker compose run --rm web python manage.py refresh_sec_financials
```

Open <http://localhost:3080>. The admin is at <http://localhost:3080/admin/>. Create its
first user with:

```bash
docker compose run --rm web python manage.py createsuperuser
```

Useful operational commands:

```bash
docker compose ps
docker compose logs -f web worker beat
docker compose exec web python manage.py check --deploy
docker compose exec web python manage.py sync_data_requirements
docker compose exec web python manage.py purge_demo_data --dry-run
docker compose exec web python manage.py refresh_official_data
docker compose exec web python manage.py refresh_prates_data
docker compose exec web python manage.py refresh_h10_data
docker compose exec web python manage.py refresh_h41_data
docker compose exec web python manage.py refresh_h8_data
docker compose exec web python manage.py refresh_macro_data
docker compose exec web python manage.py refresh_berkshire_letters
docker compose exec web python manage.py sync_ai_glossary_catalog
docker compose exec web python manage.py sync_ai_reference_catalog
docker compose exec web python manage.py sync_ai_supply_chain_catalog
docker compose down
```

`seed_platform` is idempotent but development/test-only. Never run it as a
production bootstrap. Data in PostgreSQL and Redis uses named Docker volumes and
survives `docker compose down`; use `docker compose down -v` only when
intentionally deleting local data.

## Local development

Python 3.12+, Node.js 22+, and a running PostgreSQL/Redis pair are recommended.
SQLite and eager Celery remain available for a lightweight UI session.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
npm install
npm run build
cp .env.example .env
export DATABASE_URL=sqlite:///db.sqlite3
export CELERY_TASK_ALWAYS_EAGER=1
python manage.py migrate
python manage.py seed_platform --allow-demo-data
python manage.py runserver
```

During UI work, run `npm run dev:css` and `npm run dev:js` in separate terminals
to rebuild the Tailwind and JavaScript bundles on changes.

Run verification with:

```bash
pytest
ruff check .
python manage.py check
```

## Configuration

All runtime configuration is environment-driven; see [`.env.example`](.env.example).
Important settings are:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL URL; omitted means local SQLite |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | Redis endpoints for tasks |
| `SITE_URL` / `SITE_NAME` | Canonical URL and independent product identity |
| `BLS_REGISTRATION_KEY` | Optional higher BLS public API quota |
| `BEA_API_KEY` / `CENSUS_API_KEY` | BEA API is reserved for explicit backfills; Census key is required for the current 1992-present MARTS retail batch, while archived workbooks remain revision witnesses |
| `SEC_USER_AGENT` | Required descriptive identity for SEC requests |
| `RAW_ARTIFACT_ROOT` | Private content-addressed storage for immutable SEC response bytes; defaults to `data/artifacts/` |
| `MARKET_DATA_PROVIDER` | Redistribution-approved provider; default `none` |
| `MARKET_DATA_API_KEY` | Credential for a redistribution-approved provider |
| `AI_PROVIDER` / `AI_API_KEY` | Optional evidence-bound analysis provider |

Do not use the defaults from `.env.example` in production. Production must use a random
`DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0`, explicit hosts and trusted origins,
encrypted secrets, TLS at the edge, and a licensed market-data provider.

## Data provenance and publication contract

Every numerical observation or published snapshot carries its source, value/as-
of time, fetch time, batch ID, quality state, licence scope, and fallback marker
where relevant. Raw artifacts are content-hashed. A dashboard snapshot becomes
public only after its required inputs pass the batch quality gate; on failure,
the last complete snapshot remains visible and is marked stale.

Production snapshots currently pull directly from the New York Fed, U.S.
Treasury interest-rate and FiscalData APIs, BLS, CFTC PRE, Federal Reserve RSS,
H.4.1, H.10, PRATES and Consumer Credit G.19; the consumer page also uses BEA
Personal Income and Outlays, Census MARTS API plus archived revision witnesses, and New York Fed Household Debt and
Credit workbooks with the required Consumer Credit Panel / Equifax attribution.
The employment page combines BLS CES/CPS/JOLTS with U.S. Department of Labor
national weekly-claims XML and the current immutable weekly-release PDF. The PDF
overrides the lagging XML tail, and advance/preliminary status remains visible.
The fund-letter library also stores metadata-only links from Berkshire Hathaway's
first-party index. FRED is not treated as a blanket redistribution licence. OKX and Deribit
adapters are internal diagnostics only and never feed the public site without
written display and redistribution permission. Paid CDS, commercial news,
exchange data, branded indices, and third-party PDFs stay disabled until the
required rights are recorded.

This is a research interface, not an order-entry or automated-trading system.
Estimates such as GEX, DEX, Vanna, Charm, gamma flip, walls, max pain, and proxy
credit metrics must retain their on-page method labels.

The SEC annual-financials integration is intentionally narrower: it covers only
Microsoft, Alphabet, Amazon, and Meta, requires five consecutive annual USD
`10-K`/`10-K/A` periods, and publishes one atomic `supply-chain-demand` batch.
The values are company-level cash capital-spend facts or proxies, not AI-only
CapEx. Amazon's productive-assets tag is broader and is not fully comparable;
GPU counts, leases, and project-level AI splits are not inferred. Raw EDGAR
responses remain in the private ignored artifact volume and are never served by
nginx.

SEC access requires a real product identity and a monitored contact email in
`SEC_USER_AGENT`; the scheduled job skips without it. Do not use a placeholder
or an unmonitored address.

## Service topology and production notes

The Compose stack runs `nginx -> gunicorn/Django`, PostgreSQL, Redis, a Celery
worker, and Celery beat. Nginx serves versioned static assets directly, prevents
service-worker caching mistakes, and forwards application traffic with proxy
headers. WhiteNoise remains a safe direct-Gunicorn fallback.

Before public deployment:

1. Put TLS and rate limiting at the edge (for example Cloudflare or a managed
   load balancer) and restrict direct access to the origin.
2. Run migrations as a one-off release job instead of concurrently on every
   replica; the Compose web command is intended for a single local instance.
3. Configure PostgreSQL point-in-time backups and object-store versioning.
4. Set Sentry/OpenTelemetry endpoints and alert on ingestion failure, stale
   required snapshots, queue depth, HTTP error rate, and backup verification.
5. Run `python manage.py check --deploy`, the complete test suite, and a route/
   sitemap reconciliation before promoting the release.

## Tests

The test suite treats public URLs and shareable query parameters as a product
contract. It covers seed cardinality and idempotence, numerical lineage,
calculation formulas, route status, dynamic detail pages, search/filter isolation,
sitemap and robots behaviour, retired-route HTTP 410 responses, and PWA assets.

The repository intentionally avoids exhaustive pixel matching. Visual regression
should compare this product's own 1440 px and 390 px baselines in both themes,
preserving information density and usability without copying another site's
branding or CSS.
