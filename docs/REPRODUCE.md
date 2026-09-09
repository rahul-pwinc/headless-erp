# Reproducing this repo, end to end

Every number in the README is produced by a committed script. This document
is the exact path from a fresh clone to each of them, with real runtimes
measured on this machine (Apple Silicon Mac, local Docker Desktop), and the
honest gaps that remain.

## 0. Prerequisites

- Docker Desktop (or any Docker + Compose v2) running
- Python 3.10+ (tested on 3.14)
- ~250 MB free disk for the UCI dataset (xlsx + 2 CSVs), ~2 GB for the
  ERPNext/MariaDB containers

## 1. Clone and set up Python

```bash
git clone <this repo>
cd headless-erp
make setup       # python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

`requirements.txt` pins `requests`, `pandas`, `openpyxl`, `pyyaml` — the
full set the harness actually imports (the old README only mentioned
`requests`, which does not run `simulate.py` or `prepare_data.py`).

Verified: `./.venv/bin/pip install -r requirements.txt` completes with no
conflicts on a clean venv (checked 2026-09-08, pandas 3.0.5 / openpyxl
3.1.5 / pyyaml 6.0.3 / requests 2.34.2).

## 2. Start ERPNext

```bash
make up           # docker compose -f docker/pwd.yml -p headless-erp up -d
```

**First run takes about 5 minutes.** `pwd.yml` runs a `create-site`
container that installs Frappe + ERPNext into a fresh MariaDB and only
exits once the site exists; every other container depends on it. Watch it
with:

```bash
docker compose -f docker/pwd.yml -p headless-erp logs -f create-site
```

The stack is ready when `http://localhost:8080` returns the ERPNext login
page. Credentials: `Administrator` / `admin`. Subsequent `make up` runs are
fast (~10s) because the site already exists in the `sites` volume.

## 3. Get the Phase 6 dataset

```bash
make data          # ./.venv/bin/python harness/prepare_data.py
```

This is the script that was missing before: without it, `data/sales_clean.csv`
does not exist and `harness/simulate.py` cannot run at all. It:

1. Downloads `online+retail+ii.zip` from the UCI ML repository (skipped if
   `data/online_retail_II.xlsx` is already present) — ~45 MB, ~10-30s
   depending on connection.
2. Converts both Excel sheets (`Year 2009-2010`, `Year 2010-2011`) to
   `data/online_retail_II.csv` (1,067,371 rows).
3. Cleans it into `data/sales_clean.csv` (1,033,527 rows): drops credit
   invoices (`Invoice` starting with `C`), non-positive `Quantity`/`Price`,
   and non-product `StockCode`s (postage, manual adjustments, bank
   charges, etc. — anything not matching `^\d{5,6}[A-Za-z]?$`); computes a
   per-SKU median `ListPrice` and a `dev_pct` deviation, then drops rows
   with `|dev_pct| > 500` as data-quality noise rather than real pricing
   variation.
4. Writes `reports/dataset_analysis.json`.

Measured runtime: **~55s** end to end on this machine (mostly the Excel
read — `.xlsx` parsing is the slow part, not the arithmetic).

Idempotent: re-running it produces byte-identical `sales_clean.csv` and the
same `dataset_analysis.json` numbers (verified 2026-09-08 — regenerated
file diffs empty against the previously committed one).

### The off-list number, four ways

`dataset_analysis.json` reports four independent answers to "how often does
real commerce sell off list", because the obvious definition of "list price"
is partly circular:

| reference definition | lines | off-list lines | off-list revenue |
|---|---|---|---|
| **median_reference** — median price per SKU, pooled across all customers (the README headline number) | 1,033,527 | 31.4% | 50.8% |
| **per_customer_reference** — each customer's own modal price for that SKU, i.e. "did this customer pay what they usually pay" | 801,418 | 3.4% | 8.7% |
| **per_customer_reference_excl_single_purchase** — same, excluding (SKU, customer) pairs bought exactly once | 457,813 | 6.0% | 13.1% |
| **per_customer_reference_min6_purchases** — same, restricted to pairs bought 6+ times | 139,690 | 8.9% | 17.9% |

All four are computed by `prepare_data.py` and written side by side under
those exact keys. Neither the median nor any per-customer cut is asserted to
be "the" right answer here — that's a framing choice, not a data fact.

Why the last two exist: a (SKU, customer) pair bought only once is
**trivially** at-reference under `per_customer_reference` — with a single
sample, the mode of that sample is itself, so it can never register as
off-list. 42.9% of the 801,418 customer-attributed lines are exactly such
singletons. Excluding them (`_excl_single_purchase`) raises the rate from
3.4% to 6.0%; restricting further to pairs with an actually well-sampled
buying pattern (6+ purchases, `_min6_purchases`) raises it again to 8.9%.
`off_reference` (27,340) is identical between `per_customer_reference` and
`per_customer_reference_excl_single_purchase` — confirming, exactly as the
math predicts, that singleton pairs contribute zero off-list lines and the
whole rate difference is a denominator effect, not new deviating lines.

The per-customer cuts are computed only over the 801,418 (of 1,033,527)
lines that carry a Customer ID; 232,109 anonymous lines are excluded from
all three per-customer numbers, not counted as either at- or off-list.

**Reading across the table**: the pooled-median 31.4% is dominated by the
wholesale/retail split the README calls out. As soon as you ask "does this
specific customer usually pay this" instead of "does this differ from the
market-wide median", off-list drops by roughly 3-10x. Off-list is not rare
even under the strictest per-customer cut (8.9%, established repeat buyers
only) — but it is far rarer than the headline number suggests once you
control for the fact that a wholesaler selling at two legitimate price
points isn't "off list" by any definition a customer would recognize.

## 4. Enable server scripts (required for the enforcement boundary)

`harness/enforce.py` installs a server-side `bill_intent` API method as a
Frappe **Server Script**, which ERPNext refuses to run unless the site
config explicitly allows it. Set it once per site:

```bash
docker exec <backend-container> bench --site frontend set-config server_script_enabled true
```

(container name depends on your compose project; with `-p headless-erp`
it's `headless-erp-backend-1`). This writes `server_script_enabled: true`
into `sites/frontend/site_config.json` (or `sites/common_site_config.json`
if applied to all sites). Verified present on this instance:

```bash
$ docker exec headless-erp-backend-1 cat /home/frappe/frappe-bench/sites/common_site_config.json
{
 ...
 "server_script_enabled": true
}
```

`harness/prove_boundary.py` calls `harness/enforce.py`'s `ensure()` itself
before running its checks, so `make boundary` provisions the role, the
constrained user, and the `bill_intent` Server Script on every run
(idempotent — it updates in place if they already exist). It does **not**
set `server_script_enabled` for you; if it's `false`, ERPNext will silently
refuse to invoke the script and `make boundary` will fail at step 3 with a
permission or "method not whitelisted" error.

## 5. Run the harness phases

```bash
make diff          # Phase 1 - the original differential (reports/latest.json)
make census        # Phase 2 - silent-acceptance census (reports/census.json)
make contract       # Phase 4 - the intent contract, 10 cases (reports/intent_proof.json)
make corpus         # Phase 5 - the 52-scenario corpus (reports/corpus.json)
make boundary        # the enforcement boundary, 11 checks (reports/boundary.json)
make simulate        # Phase 6 - replay 1,000 real invoices (reports/simulation.json)
```

Or all of it, in order:

```bash
make all
```

Measured runtimes against a local stack on this machine (2026-09-08):

| target | runtime | notes |
|---|---|---|
| `make census` | ~15s | 203 probes across 9 doctypes |
| `make contract` | ~5s | 10 cases. **Hit a transient `QueryDeadlockError` on `tabSeries` on the first attempt** — a MariaDB naming-series race, not a code bug; retried immediately and passed 10/10. If this happens, just re-run. |
| `make corpus` | ~25s | 52 scenarios |
| `make data` | ~55s | see above |
| `make boundary` | ~5s | provisions the role/user/Server Script, then runs 11 checks; verified 11/11 (2026-09-08) |
| `make simulate` | ~6 min | 1,000 invoices written twice (naive + intent); the committed `reports/clean/simulation.json` is a fresh-clone run (244s). A 250-invoice smoke test (~78s) was also run during this pass to verify the target mechanically works, then the original 1,000-invoice `reports/simulation.json` was restored so the committed figures weren't disturbed. |

### The enforcement boundary, in one run

`make boundary` (`harness/prove_boundary.py`) is the strongest evidence in
the repo, because it doesn't trust any client-side code at all:

```
1. direct write, unconstrained identity   -> ALLOWED   (Administrator can still do anything)
2. direct write, constrained identity     -> 403        (the "Agent Writer" role has no write perm on any transaction doctype)
3. constrained identity, via bill_intent:
     clean call, no caller input           -> derives list price, writes
     caller supplies rate                  -> REFUSED
     caller supplies price_list_rate       -> REFUSED
     override with no reason               -> REFUSED
     override with a reason                -> allowed, recorded
     item with no resolvable price         -> REFUSED

11/11 boundary checks passed
```

Case 2 is the point of this section: it is not merely that the intent layer
*chooses* to derive-or-refuse (that's `prove_intent.py`, a client library
that a caller could simply not use) — a constrained identity has **no other
way in**. `/api/resource/Sales Invoice` returns 403 before any business
logic runs. The only path to a Sales Invoice for that identity is
`bill_intent`, and that endpoint enforces the same derive-or-refuse contract
server-side.

### A real finding from re-running `make corpus` on this shared instance

On this specific long-lived ERPNext container, `make corpus` currently
scores **29/39, not 39/39.** The cause is not a bug in `corpus.py` or in the
new files here — it's a leftover `Pricing Rule` (`PRLE-0001`, a 10% selling
discount on one item code) sitting in this instance's database. No committed
script creates a Pricing Rule anywhere in `harness/`, `corpus/`, or
`intents/` (checked by grep); this is manual/ad-hoc state from earlier
exploratory work on this shared instance, the same kind of thing the
`dataset_analysis.json` gap came from. It silently discounts every sale of
that item by 10%, which is exactly the `expected 1000, got 900.0` pattern
in the failures.

**This is a state-hygiene issue with this particular long-lived container,
not a reproducibility defect in the scripts.** A genuinely fresh
`make up` (new site, no manual pokes since) should score 39/39, matching the
README. If you hit a similar drop on a shared instance, check for stray
`Pricing Rule` documents before assuming the harness regressed:

```bash
./.venv/bin/python -c "
import sys; sys.path.insert(0, 'harness')
from client import FrappeClient
c = FrappeClient('http://localhost:8080', 'Administrator', 'admin')
print(c.call('frappe.client.get_list', doctype='Pricing Rule', fields=['name','discount_percentage'], limit_page_length=0))
"
```

This was not fixed here (out of scope: no existing `harness/*.py` was
modified for this pass), and the stray rule was left in place rather than
deleted mid-review — flag it and let whoever owns the instance decide
whether to reset it.

## 6. What's still not reproducible from a committed script

Stated plainly so nothing here is claimed as more solid than it is:

- **`reports/trial_balance.json`** has no producer in `harness/`. It reads
  as a GL trial-balance check (6,466 GL entries, debits = credits =
  451,856.02) consistent with the 1,000-invoice `simulate.py` run, but no
  committed script regenerates it. Whoever produced it did so the same way
  `dataset_analysis.json` used to be produced — outside the repo. This is
  a real gap; it just wasn't in the four defects this pass was scoped to
  fix.
- `make simulate` passes `--invoices 1000` specifically to match the
  committed `reports/simulation.json` and the README's Phase 6 numbers.
  Running `./.venv/bin/python harness/simulate.py` with no arguments uses
  its own default of 250 invoices, which is faster (~80s) but will not
  reproduce those exact figures.

## 7. Tearing down

```bash
make down          # docker compose -f docker/pwd.yml -p headless-erp down
```

Data persists in the named volumes (`db-data`, `sites`, ...) unless you add
`-v`. `make up` again reuses the existing site rather than recreating it.
