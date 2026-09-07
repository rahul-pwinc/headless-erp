"""Produce reports/trial_balance.json.

This exists for reproducibility, not for evidence. ERPNext refuses to post an
unbalanced voucher (`raise_debit_credit_not_equal_error`,
erpnext/accounts/general_ledger.py), so the result below is guaranteed by
construction and can only ever come out balanced.

That is the point worth taking from it: a check that cannot fail is not a check.
Any assurance process whose ledger test is "do debits equal credits" will pass
this company regardless of what was written into it. The corpus in
corpus/scenarios.yaml exists because that question is the wrong one.
"""
from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import FrappeClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("ERPNEXT_URL", "http://localhost:8080"))
    ap.add_argument("--company", default="Headless Test Co")
    ap.add_argument("--out", default="reports/trial_balance.json")
    a = ap.parse_args()

    c = FrappeClient(a.url, "Administrator", "admin")
    rows = c.call("frappe.client.get_list", doctype="GL Entry",
                  filters={"company": a.company, "is_cancelled": 0},
                  fields=["account", "debit", "credit"], limit_page_length=0) or []
    d = sum(float(r["debit"] or 0) for r in rows)
    cr = sum(float(r["credit"] or 0) for r in rows)
    inv = c.call("frappe.client.get_list", doctype="Sales Invoice",
                 filters={"company": a.company, "docstatus": 1},
                 fields=["name"], limit_page_length=0) or []

    by_account: dict[str, list[float]] = {}
    for r in rows:
        acc = by_account.setdefault(r["account"], [0.0, 0.0])
        acc[0] += float(r["debit"] or 0)
        acc[1] += float(r["credit"] or 0)

    print(f"GL entries        : {len(rows):,}")
    print(f"submitted invoices: {len(inv):,}")
    print(f"total debits      : {d:>16,.2f}")
    print(f"total credits     : {cr:>16,.2f}")
    print(f"difference        : {d - cr:>16,.2f}")
    print(f"balanced          : {abs(d - cr) < 0.01}")
    print("\nThis was guaranteed. ERPNext will not post an unbalanced voucher, so")
    print("'balanced' here carries no information about whether the amounts are right.")

    json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
               "company": a.company, "gl_entries": len(rows),
               "submitted_invoices": len(inv), "total_debits": d, "total_credits": cr,
               "difference": d - cr, "balanced": abs(d - cr) < 0.01,
               "by_account": {k: {"debit": v[0], "credit": v[1]} for k, v in by_account.items()},
               "caveat": ("Guaranteed by construction: ERPNext raises "
                          "raise_debit_credit_not_equal_error before posting. This artifact "
                          "demonstrates that balance is enforced, therefore uninformative.")},
              open(a.out, "w"), indent=2)
    print(f"\nreport: {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
