"""Phase 2 runner — silent-acceptance census across transaction doctypes."""
from __future__ import annotations

import argparse, json, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import FrappeClient          # noqa: E402
from fixtures import ensure_all          # noqa: E402
from census import probe_doctype, summarise  # noqa: E402

TODAY, LATER = "2026-09-07", "2026-10-07"


def headers(fx: dict) -> dict[str, dict]:
    sell = {"company": fx["company"], "currency": "INR", "conversion_rate": 1,
            "selling_price_list": fx["price_list"], "price_list_currency": "INR",
            "plc_conversion_rate": 1}
    buy = {"company": fx["company"], "currency": "INR", "conversion_rate": 1,
           "buying_price_list": fx["buy_price_list"], "price_list_currency": "INR",
           "plc_conversion_rate": 1, "supplier": fx["supplier"]}
    return {
        "Quotation":         {**sell, "doctype": "Quotation", "quotation_to": "Customer",
                              "party_name": fx["customer"], "transaction_date": TODAY,
                              "valid_till": LATER},
        "Sales Order":       {**sell, "doctype": "Sales Order", "customer": fx["customer"],
                              "transaction_date": TODAY, "delivery_date": LATER},
        "Sales Invoice":     {**sell, "doctype": "Sales Invoice", "customer": fx["customer"],
                              "posting_date": TODAY, "due_date": LATER, "update_stock": 0},
        "Delivery Note":     {**sell, "doctype": "Delivery Note", "customer": fx["customer"],
                              "posting_date": TODAY},
        "Supplier Quotation":{**buy, "doctype": "Supplier Quotation", "transaction_date": TODAY},
        "Purchase Order":    {**buy, "doctype": "Purchase Order", "transaction_date": TODAY,
                              "schedule_date": LATER},
        "Purchase Receipt":  {**buy, "doctype": "Purchase Receipt", "posting_date": TODAY},
        "Purchase Invoice":  {**buy, "doctype": "Purchase Invoice", "posting_date": TODAY,
                              "due_date": LATER, "bill_no": "HL-TEST-001", "bill_date": TODAY},
        "Material Request":  {"doctype": "Material Request", "company": fx["company"],
                              "material_request_type": "Purchase",
                              "transaction_date": TODAY, "schedule_date": LATER},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("ERPNEXT_URL", "http://localhost:8080"))
    ap.add_argument("--out", default="reports/census.json")
    a = ap.parse_args()

    client = FrappeClient(a.url, "Administrator", "admin")
    fx = ensure_all(client)

    all_probes = []
    for dt, header in headers(fx).items():
        print(f"\nprobing {dt} ...")
        probes = probe_doctype(client, dt, header, fx["item_code"])
        for p in probes:
            if p.verdict == "silently_accepted":
                print(f"  ACCEPTED  {p.fieldname:24} derived={p.derived_value!r} -> stored={p.stored_value!r}")
            elif p.verdict == "protected":
                print(f"  protected {p.fieldname:24} supplied={p.supplied_value!r} -> stored={p.stored_value!r}")
            elif p.verdict == "rejected":
                print(f"  rejected  {p.fieldname:24} {p.note[:80]}")
        all_probes.extend(probes)

    s = summarise(all_probes)
    print("\n" + "=" * 66)
    print(f"probes run          : {s['probes_run']}")
    for k, v in sorted(s["by_verdict"].items()):
        print(f"  {k:20}: {v}")
    print(f"\nsilently accepted   : {len(s['silently_accepted_fields'])} distinct fields")
    print(f"  {', '.join(s['silently_accepted_fields'])}")
    print(f"protected           : {len(s['protected_fields'])} distinct fields")
    print(f"  {', '.join(s['protected_fields'])}")

    s["generated_at"] = datetime.now(timezone.utc).isoformat()
    s["erpnext_version"] = "v16.34.1"
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(s, open(a.out, "w"), indent=2, default=str)
    print(f"\nreport: {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
