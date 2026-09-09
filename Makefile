.PHONY: trial-balance setup up data diff census contract boundary corpus simulate all down

PYTHON := ./.venv/bin/python
PIP    := ./.venv/bin/pip
COMPOSE := docker compose -f docker/pwd.yml -p headless-erp

# --- setup -------------------------------------------------------------
setup:
	@echo "==> setup: creating .venv and installing pinned requirements"
	python3 -m venv .venv
	$(PIP) install -r requirements.txt
	@echo "==> setup: done. Run 'make up' next."

# --- infrastructure ------------------------------------------------------
up:
	@echo "==> up: starting ERPNext v16.34.1 stack (docker/pwd.yml)"
	@echo "    first run creates the site and takes ~5 minutes -- watch with:"
	@echo "    $(COMPOSE) logs -f create-site"
	$(COMPOSE) up -d
	@echo "==> up: containers started (site: http://localhost:8080, Administrator/admin)"

down:
	@echo "==> down: stopping ERPNext stack"
	$(COMPOSE) down
	@echo "==> down: done"

# --- Phase 6 dataset -----------------------------------------------------
data:
	@echo "==> data: downloading (if needed) + cleaning UCI Online Retail II"
	$(PYTHON) harness/prepare_data.py
	@echo "==> data: done -- data/sales_clean.csv, reports/dataset_analysis.json"

# --- Phase 1: the original differential oracle ----------------------------
diff:
	@echo "==> diff: Phase 1 differential oracle (UI-derivation path vs naive API path)"
	$(PYTHON) harness/run.py
	@echo "==> diff: done -- reports/latest.json"

# --- Phase 2: silent-acceptance census ------------------------------------
census:
	@echo "==> census: Phase 2 silent-acceptance census across 9 transaction doctypes"
	$(PYTHON) harness/run_census.py
	@echo "==> census: done -- reports/census.json"

# --- Phase 4: the intent-layer contract, proved -------------------------
contract:
	@echo "==> contract: Phase 4 intent-layer proof (10 cases)"
	$(PYTHON) harness/prove_intent.py
	@echo "==> contract: done -- reports/intent_proof.json"

# --- enforcement boundary: the intent contract as a server-side control ---
# Requires server_script_enabled: true in the ERPNext container's
# sites/common_site_config.json -- see docs/REPRODUCE.md for how to set it.
# `boundary` provisions it (idempotent) and then proves it in the same run.
boundary:
	@echo "==> boundary: provisioning + proving the enforcement boundary (11 checks)"
	$(PYTHON) harness/prove_boundary.py
	@echo "==> boundary: done -- reports/boundary.json"

# --- Phase 5: the use-case corpus -----------------------------------------
corpus:
	@echo "==> corpus: Phase 5 use-case corpus (52 accounting-principle scenarios)"
	$(PYTHON) harness/run_corpus.py
	@echo "==> corpus: done -- reports/corpus.json"

# --- Phase 6: replay real invoices at scale -------------------------------
# --invoices 1000 matches the committed reports/simulation.json and the
# README's Phase 6 numbers (harness/simulate.py defaults to 250 invoices,
# which is faster but will not reproduce those exact figures).
simulate: data
	@echo "==> simulate: Phase 6 replay of 1,000 real invoices, naive path vs intent path"
	@echo "    (~6 minutes against a local stack; see docs/REPRODUCE.md to run fewer invoices)"
	$(PYTHON) harness/simulate.py --invoices 1000
	@echo "==> simulate: done -- reports/simulation.json"

# --- the whole thing, in the order the README presents it -----------------
all: setup up data diff census contract corpus boundary simulate
	@echo "==> all: full pipeline complete. See docs/REPRODUCE.md for what each report proves."


trial-balance:  ## Produce reports/trial_balance.json (reproducibility only; see the script header)
	@echo "==> trial balance (guaranteed balanced by construction)"
	./.venv/bin/python harness/trial_balance.py

citations:  ## Verify every load-bearing source citation against the running container
	@echo "==> citations: check line numbers against the running erpnext/frappe"
	./.venv/bin/python harness/verify_citations.py

concurrency:  ## Race 20 simultaneous overrides; verify the audit chain survives
	@echo "==> concurrency: 20 parallel overrides against the audit chain"
	./.venv/bin/python harness/prove_concurrency.py

numbers:  ## Regenerate the README Current numbers table from reports/clean/*.json
	@echo "==> numbers: regenerating the README table from the artifacts"
	./.venv/bin/python harness/render_numbers.py
