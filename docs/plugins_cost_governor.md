# Cost Governor plugin

Records the real OpenRouter cost of every request per user, and enforces calendar-month spending limits by Open WebUI group. Users never see any cost figure.

## Enable

1. Turn on `ENABLE_PLUGIN_SYSTEM` and `GOVERNOR_ENABLE` in the pipe's valves (admin only).
2. Leave `GOVERNOR_DB_URL` blank to write the ledger into Open WebUI's own database, or set a SQLAlchemy URL (for example `mysql+pymysql://user:pass@host/db`) to keep it elsewhere. The table `openrouter_cost_ledger` is created on first use.
3. Create Open WebUI groups and list their limits in `GOVERNOR_GROUP_LIMITS`, e.g. `quota-base=5, quota-medium=20, quota-pro=100`.

## Rules

- A user's limit is the most generous matching group; users in none of the listed groups get `GOVERNOR_DEFAULT_MONTHLY_LIMIT_USD` (negative means unlimited). `unlimited` is a valid group amount.
- The month is the calendar month in `GOVERNOR_TIMEZONE`; allowances do not roll over.
- Limits are checked before each request, so one request can overshoot. Cost is written when the request ends.
- Over the limit, a user is served `GOVERNOR_OVER_LIMIT_MODEL` if set (it must be allowed by `MODEL_ID`, and should be free), otherwise refused with `GOVERNOR_OVER_LIMIT_MESSAGE`. Fallback rows are flagged `degraded`.
- Administrators are exempt when `GOVERNOR_EXEMPT_ADMINS` is on; their requests are still recorded. Title, tag and other background tasks are recorded but never blocked.
- If the database cannot be reached, requests are allowed and the failure is logged.

## Keeping cost from users

`HIDE_COST_FROM_USERS` (default on) removes the cost from the final status line and strips `cost`, `cost_details`, `cache_discount` and related fields from every usage object sent to Open WebUI. Without this, Open WebUI stores the cost on the message and shows it in the message info tooltip. Plugins, including this one, still receive the full figures.

## Reports

```bash
python scripts/ledger_report.py --db-url sqlite:////path/to/webui.db --month 2026-10
python scripts/ledger_report.py --db-url sqlite:////path/to/webui.db --month 2026-10 --detail
```

The first prints one CSV line per user; `--detail` prints every request.
