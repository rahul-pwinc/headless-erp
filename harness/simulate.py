"""Replay real commercial transactions through both paths at scale.

Data: UCI Online Retail II (1.07M line items, 53,628 invoices, 5,305 SKUs,
2 years of a UK wholesaler). Real prices, real quantities, real customers.

Two callers write the same real invoices:

  naive  - posts the price that is in the data, the way an MCP server or an
           agent with a price in hand would. ERPNext accepts it silently.
  intent - derives the list price first. If the real price differs, the write
           only succeeds with an explicit override carrying a reason.

The question this answers is not "can a wrong price get in" (Phase 1 settled
that). It is: at realistic volume, how many writes are indistinguishable from
errors afterwards, and how many are self-describing.
"""
from __future__ import annotations

import argparse, json, os, random, sys, time
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd                      # noqa: E402
from client import FrappeClient, FrappeError   # noqa: E402
from intent import IntentEngine, IntentRefused # noqa: E402

COMPANY_FALLBACK = "Headless Test Co"
PRICE_LIST = "Standard Selling"


def build_master_data(client, df, n_skus, n_customers):
    """Create Items, Item Prices (the list) and Customers from the real data."""
    top_skus = df.StockCode.value_counts().head(n_skus).index.tolist()
    sub = df[df.StockCode.isin(top_skus)]
    listprice = sub.groupby("StockCode").Price.median().to_dict()
    names = sub.groupby("StockCode").Description.first().to_dict()
    top_cust = sub.CustomerID.value_counts().head(n_customers).index.tolist()

    existing_items = {r["name"] for r in (client.call(
        "frappe.client.get_list", doctype="Item", fields=["name"],
        filters={"item_group": "Products"}, limit_page_length=0) or [])}
    made_i = 0
    for sku in top_skus:
        code = f"OR-{sku}"
        if code in existing_items:
            continue
        try:
            client.insert({"doctype": "Item", "item_code": code,
                           "item_name": str(names.get(sku, sku))[:140],
                           "item_group": "Products", "stock_uom": "Nos",
                           "is_stock_item": 0})
            client.insert({"doctype": "Item Price", "item_code": code,
                           "price_list": PRICE_LIST,
                           "price_list_rate": round(float(listprice[sku]), 2)})
            made_i += 1
        except FrappeError as e:
            print(f"    item {code} skipped: {str(e)[:80]}")
    existing_cust = {r["name"] for r in (client.call(
        "frappe.client.get_list", doctype="Customer", fields=["name"],
        limit_page_length=0) or [])}
    made_c = 0
    for cid in top_cust:
        name = f"OR-CUST-{int(cid)}"
        if name in existing_cust:
            continue
        try:
            client.insert({"doctype": "Customer", "customer_name": name,
                           "customer_type": "Company"})
            made_c += 1
        except FrappeError:
            pass
    print(f"  master data: +{made_i} items, +{made_c} customers "
          f"({len(top_skus)} SKUs / {len(top_cust)} customers in scope)")
    return set(top_skus), set(top_cust), listprice


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("ERPNEXT_URL", "http://localhost:8080"))
    ap.add_argument("--invoices", type=int, default=250)
    ap.add_argument("--skus", type=int, default=150)
    ap.add_argument("--customers", type=int, default=60)
    ap.add_argument("--max-lines", type=int, default=5)
    ap.add_argument("--out", default="reports/simulation.json")
    a = ap.parse_args()

    df = pd.read_csv("data/sales_clean.csv")
    client = FrappeClient(a.url, "Administrator", "admin")
    company = (client.call("frappe.client.get_list", doctype="Company",
                           fields=["name"], limit_page_length=0)
               or [{"name": COMPANY_FALLBACK}])[0]["name"]
    print(f"company: {company}")
    skus, custs, listprice = build_master_data(client, df, a.skus, a.customers)

    pool = df[df.StockCode.isin(skus) & df.CustomerID.isin(custs)]
    inv_ids = pool.Invoice.unique().tolist()
    random.seed(20260908)
    random.shuffle(inv_ids)

    eng = IntentEngine(client)
    stats = Counter()
    off_examples, errors = [], Counter()
    t0 = time.time()
    done = 0

    for inv in inv_ids:
        if done >= a.invoices:
            break
        rows = pool[pool.Invoice == inv].head(a.max_lines)
        if rows.empty:
            continue
        cust = f"OR-CUST-{int(rows.CustomerID.iloc[0])}"
        hdr = {"customer": cust, "company": company, "currency": "INR",
               "conversion_rate": 1, "selling_price_list": PRICE_LIST,
               "price_list_currency": "INR", "plc_conversion_rate": 1,
               "posting_date": "2026-09-07", "due_date": "2026-10-07",
               "update_stock": 0}

        # ---- naive caller: posts the real price straight into the row --------
        naive_doc = {**hdr, "doctype": "Sales Invoice", "items": [
            {"item_code": f"OR-{r.StockCode}", "qty": float(r.Quantity),
             "rate": float(r.Price)} for r in rows.itertuples()]}
        try:
            saved = client.insert(naive_doc)
            saved = client.submit(saved)
            stats["naive_written"] += 1
            for i, r in enumerate(rows.itertuples()):
                lp = round(float(listprice[r.StockCode]), 2)
                if abs(float(r.Price) - lp) >= 0.005:
                    stats["naive_offlist_lines"] += 1
                    stats["naive_offlist_unrecorded"] += 1
                else:
                    stats["naive_atlist_lines"] += 1
        except FrappeError as e:
            stats["naive_failed"] += 1
            errors[str(e)[:60]] += 1

        # ---- intent caller: derive, then override only with a reason ---------
        lines, overrides = [], []
        for i, r in enumerate(rows.itertuples()):
            lines.append({"item_code": f"OR-{r.StockCode}", "qty": float(r.Quantity)})
            lp = round(float(listprice[r.StockCode]), 2)
            if abs(float(r.Price) - lp) >= 0.005:
                overrides.append({"row": i, "field": "rate", "value": float(r.Price),
                                  "reason": f"source invoice {r.Invoice} priced at "
                                            f"{r.Price} vs list {lp}"})
        try:
            res = eng.execute("bill", hdr, lines, overrides=overrides or None)
            stats["intent_written"] += 1
            stats["intent_overrides_recorded"] += len(res.overrides)
            stats["intent_atlist_lines"] += len(lines) - len(overrides)
            if res.invariant_failures:
                stats["intent_invariant_failures"] += 1
            if overrides and len(off_examples) < 5:
                o = res.overrides[0]
                off_examples.append({"doc": res.name, "field": o.fieldname,
                                     "derived": o.derived_value,
                                     "supplied": o.supplied_value, "reason": o.reason})
        except IntentRefused as e:
            stats["intent_refused"] += 1
            errors["REFUSED " + str(e).splitlines()[0][:50]] += 1
        except FrappeError as e:
            stats["intent_failed"] += 1
            errors[str(e)[:60]] += 1

        done += 1
        if done % 25 == 0:
            print(f"  {done}/{a.invoices} invoices  ({time.time()-t0:.0f}s)")

    elapsed = time.time() - t0
    print("\n" + "=" * 70)
    for k in sorted(stats):
        print(f"  {k:32} {stats[k]:>8,}")
    print(f"  {'elapsed_seconds':32} {elapsed:>8.0f}")
    if errors:
        print("\n  top errors/refusals:")
        for e, c in errors.most_common(5):
            print(f"    {c:>4}x  {e}")

    json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
               "invoices_replayed": done, "elapsed_seconds": elapsed,
               "stats": dict(stats), "override_examples": off_examples,
               "errors": dict(errors.most_common(10))},
              open(a.out, "w"), indent=2, default=str)
    print(f"\nreport: {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
