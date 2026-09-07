"""Run the differential oracle and emit a report.

Usage:
    python harness/run.py [--url http://localhost:8080] [--out reports/latest.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client import FrappeClient          # noqa: E402
from fixtures import ensure_all          # noqa: E402
from oracle import run_case              # noqa: E402

ASSERTED_RATE = 1.0   # what a careless agent might supply


def build_intent(fx: dict) -> dict:
    return {
        "name": "sales_invoice_single_line",
        "header": {
            "doctype": "Sales Invoice",
            "customer": fx["customer"],
            "company": fx["company"],
            "currency": "INR",
            "conversion_rate": 1,
            "selling_price_list": fx["price_list"],
            "price_list_currency": "INR",
            "plc_conversion_rate": 1,
            "posting_date": "2026-09-07",
            "due_date": "2026-10-07",
            "update_stock": 0,
        },
        "lines": [
            {"item_code": fx["item_code"], "qty": 4, "asserted_rate": ASSERTED_RATE}
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("ERPNEXT_URL", "http://localhost:8080"))
    ap.add_argument("--user", default=os.environ.get("ERPNEXT_USER", "Administrator"))
    ap.add_argument("--password", default=os.environ.get("ERPNEXT_PASSWORD", "admin"))
    ap.add_argument("--out", default="reports/latest.json")
    args = ap.parse_args()

    print(f"connecting to {args.url} ...")
    client = FrappeClient(args.url, args.user, args.password)
    print("  authenticated")

    fx = ensure_all(client)
    intent = build_intent(fx)

    results = []
    for mode in ("omit", "assert"):
        print(f"\nrunning mode={mode} ...")
        r = run_case(client, intent, mode)
        results.append(r)
        if r.error:
            print(f"  ERROR: {r.error}")
            continue
        real = [g for g in r.gaps if g.is_gap]
        print(f"  ui  doc: {r.ui_doc_name}  grand_total={r.ui_grand_total}")
        print(f"  api doc: {r.api_doc_name}  grand_total={r.api_grand_total}")
        print(f"  derivation gaps: {len(real)} / {len(r.gaps)} fields compared")
        for g in real:
            print(f"    - {g.fieldname}: ui={g.ui_value!r}  api={g.api_value!r}")
        print(f"  api doc GL balances: {r.api_doc_balances}")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "erpnext_url": args.url,
        "list_price": fx["list_price"],
        "asserted_rate": ASSERTED_RATE,
        "results": [r.to_dict() for r in results],
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"\nreport written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
