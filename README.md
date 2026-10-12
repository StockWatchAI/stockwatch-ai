# StockWatch AI website and notification jobs

The public repository contains GitHub Pages and scheduled market-data jobs.
Firestore user documents are private inputs, so public Actions logs must never
include user IDs, FCM tokens, watchlists, watched-symbol lists or personalized
model output. External failures log only their exception class and HTTP status;
SDK messages, request URLs, response bodies and tracebacks are omitted.

`scripts/security.py` validates user-supplied watchlists before aggregation and
before per-user notification matching. Operational ceilings apply independently
of the app's subscription UI:

- At most 100 entries examined per user's watchlist (the free app UI allows 10;
  Pro previously had no UI ceiling).
- At most 10,000 user documents read per job and 500 shared symbols processed.
  Shared selection takes turns across users before reaching the symbol ceiling.
- At most 200 paid morning-summary calls per run. Once exhausted, notifications
  fall back to the factual price/change text; stored summaries may be empty.
- Existing EDGAR and sector-digest per-run limits remain in place.

Users exceeding a ceiling receive only the processed subset. Aggregate counts
should be monitored before increasing limits; do not restore private diagnostics
in this public repository. The limiter bounds workload and does not prove that a
Firebase anonymous user is a paid subscriber. Firebase production rules are
managed outside this repository and still need appropriate schema validation.

Run the offline regressions without credentials or provider packages:

```bash
python -m unittest discover -s tests -v
```

The normal jobs use Python 3.11. Requests is pinned to patched version 2.34.2.
GitHub Actions are pinned to official immutable commits and Dependabot proposes
weekly pip and Actions updates. This source change does not delete historical
workflow logs, rotate credentials or change Firebase configuration.
