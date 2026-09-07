"""Idempotent master data for the differential harness.

A fresh ERPNext site has no Company and no Chart of Accounts until the setup
wizard runs, so we complete it over the same API a client would use.
"""
from __future__ import annotations

from client import FrappeClient, FrappeError

COMPANY = "Headless Test Co"
ABBR = "HTC"
CURRENCY = "INR"
PRICE_LIST = "Standard Selling"
ITEM_CODE = "HL-WIDGET-001"
ITEM_GROUP = "Products"
PRICE_VALID_FROM = "2020-01-01"  # far enough back that no scenario date precedes it
LIST_PRICE = 250.0          # the Price List rate the UI would derive
CUSTOMER = "Headless Test Customer"
SUPPLIER = "Headless Test Supplier"
UNPRICED_ITEM = "HL-UNPRICED-001"  # deliberately has no Item Price anywhere
BUY_PRICE_LIST = "Standard Buying"
BUY_PRICE = 120.0

# -- derivation_map.md fixtures: conversion_rate, conversion_factor, and the
#    auto_insert_price_list_rate_if_missing master-data mutation ------------
FOREIGN_CURRENCY = "USD"
FX_RATE_DATE = "2026-09-07"
FX_RATE = 90.0              # fixed, so scenario totals don't depend on a live FX lookup
BOX_UOM = "Box"
BOX_CONVERSION_FACTOR = 10.0
PRICE_MUTATION_ITEM = "HL-PRICE-MUTATION-001"  # throwaway: see ensure_price_mutation_item


def ensure_setup(client: FrappeClient) -> None:
    """Run the setup wizard once. Safe to call repeatedly."""
    companies = client.call(
        "frappe.client.get_list", doctype="Company", fields=["name"], limit_page_length=0
    ) or []
    if companies:
        print(f"  setup: already complete (company={companies[0]['name']})")
        return
    print("  setup: running setup wizard (this takes ~60s)...")
    client.call(
        "frappe.desk.page.setup_wizard.setup_wizard.setup_complete",
        args={
            "language": "English (United States)",
            "country": "India",
            "timezone": "Asia/Kolkata",
            "currency": CURRENCY,
            "company_name": COMPANY,
            "company_abbr": ABBR,
            "chart_of_accounts": "Standard",
            "fy_start_date": "2026-04-01",
            "fy_end_date": "2027-03-31",
        },
    )
    print("  setup: done")


def ensure_item(client: FrappeClient) -> None:
    if client.exists("Item", ITEM_CODE):
        print(f"  item: {ITEM_CODE} exists")
    else:
        client.insert(
            {
                "doctype": "Item",
                "item_code": ITEM_CODE,
                "item_name": "Headless Widget",
                "item_group": ITEM_GROUP,
                "stock_uom": "Nos",
                "is_stock_item": 0,   # service item: no stock ledger needed for SI
                "description": "Fixture item for the differential harness.",
            }
        )
        print(f"  item: created {ITEM_CODE}")

    existing = client.call(
        "frappe.client.get_list",
        doctype="Item Price",
        filters={"item_code": ITEM_CODE, "price_list": PRICE_LIST},
        fields=["name", "price_list_rate"],
        limit_page_length=0,
    ) or []
    if existing:
        print(f"  price:  {PRICE_LIST} = {existing[0]['price_list_rate']} (exists)")
    else:
        client.insert(
            {
                "doctype": "Item Price",
                "item_code": ITEM_CODE,
                "price_list": PRICE_LIST,
                "price_list_rate": LIST_PRICE,
                # Pin this. ERPNext defaults valid_from to the creation date, so
                # a fixture built today is not valid for a scenario posting
                # yesterday. Without it the suites pass only on the day the
                # fixtures happened to be created, which is how they scored
                # 39/39 on a used instance and 11/39 on a fresh one.
                "valid_from": PRICE_VALID_FROM,
            }
        )
        print(f"  price:  {PRICE_LIST} = {LIST_PRICE} (created)")


def ensure_unpriced_item(client: FrappeClient) -> None:
    """An item with no price in any list. Used to prove the engine refuses to
    guess a rate rather than inventing one."""
    if client.exists("Item", UNPRICED_ITEM):
        print(f"  item: {UNPRICED_ITEM} exists (no price, by design)")
        return
    client.insert({"doctype": "Item", "item_code": UNPRICED_ITEM,
                   "item_name": "Headless Unpriced Widget", "item_group": ITEM_GROUP,
                   "stock_uom": "Nos", "is_stock_item": 0,
                   "description": "Deliberately has no Item Price."})
    print(f"  item: created {UNPRICED_ITEM} (no price, by design)")


def ensure_customer(client: FrappeClient) -> None:
    if client.exists("Customer", CUSTOMER):
        print(f"  customer: {CUSTOMER} exists")
        return
    client.insert(
        {
            "doctype": "Customer",
            "customer_name": CUSTOMER,
            "customer_type": "Company",
        }
    )
    print(f"  customer: created {CUSTOMER}")


def ensure_supplier(client: FrappeClient) -> None:
    if not client.exists("Supplier", SUPPLIER):
        client.insert({"doctype": "Supplier", "supplier_name": SUPPLIER,
                       "supplier_group": "All Supplier Groups"})
        print(f"  supplier: created {SUPPLIER}")
    else:
        print(f"  supplier: {SUPPLIER} exists")
    existing = client.call("frappe.client.get_list", doctype="Item Price",
                           filters={"item_code": ITEM_CODE, "price_list": BUY_PRICE_LIST},
                           fields=["name"], limit_page_length=0) or []
    if not existing:
        client.insert({"doctype": "Item Price", "item_code": ITEM_CODE,
                       "price_list": BUY_PRICE_LIST, "price_list_rate": BUY_PRICE,
                       "valid_from": PRICE_VALID_FROM})
        print(f"  price:  {BUY_PRICE_LIST} = {BUY_PRICE} (created)")


def ensure_foreign_currency(client: FrappeClient) -> None:
    """A fixed Currency Exchange record, so conversion_rate scenarios don't
    depend on a live external FX lookup (derivation_map.md finding 1 used
    ERPNext's own fallback to a real exchange-rate API, which is correct for
    an audit but not for a reproducible test: the rate, and therefore the
    expected totals, would change every day)."""
    existing = client.call(
        "frappe.client.get_list", doctype="Currency Exchange",
        filters={"from_currency": FOREIGN_CURRENCY, "to_currency": CURRENCY, "date": FX_RATE_DATE},
        fields=["name", "exchange_rate"], limit_page_length=0) or []
    if existing:
        print(f"  fx rate: {FOREIGN_CURRENCY}->{CURRENCY} on {FX_RATE_DATE} = "
              f"{existing[0]['exchange_rate']} (exists)")
        return
    client.insert({"doctype": "Currency Exchange", "from_currency": FOREIGN_CURRENCY,
                   "to_currency": CURRENCY, "date": FX_RATE_DATE,
                   "exchange_rate": FX_RATE, "for_buying": 1, "for_selling": 1})
    print(f"  fx rate: created {FOREIGN_CURRENCY}->{CURRENCY} on {FX_RATE_DATE} = {FX_RATE}")


def ensure_box_uom(client: FrappeClient) -> None:
    """Give the fixture item a second UOM whose conversion factor is not 1,
    so a caller can name a uom != stock_uom (derivation_map.md finding 7:
    master Box=10, caller-supplied conversion_factor survives regardless)."""
    item = client.get_doc("Item", ITEM_CODE)
    uoms = item.get("uoms") or []
    for u in uoms:
        if u.get("uom") == BOX_UOM:
            print(f"  uom: {ITEM_CODE} {BOX_UOM} = {u.get('conversion_factor')} (exists)")
            return
    uoms.append({"uom": BOX_UOM, "conversion_factor": BOX_CONVERSION_FACTOR})
    client.call("frappe.client.set_value", doctype="Item", name=ITEM_CODE,
               fieldname="uoms", value=uoms)
    print(f"  uom: added {ITEM_CODE} {BOX_UOM} = {BOX_CONVERSION_FACTOR}")


def ensure_price_mutation_item(client: FrappeClient) -> None:
    """A dedicated throwaway item for the auto_insert_price_list_rate_if_missing
    finding. Unlike UNPRICED_ITEM (which every other scenario relies on
    staying unpriced, so drift there is only ever flagged, never fixed), this
    item exists for exactly one scenario that intentionally triggers ERPNext
    writing a caller's rate into the Item Price master. That mutation is the
    finding, so this function self-heals: it deletes whatever the last run
    left behind, rather than merely reporting it, so the scenario starts
    clean every time instead of accumulating stray Item Price rows."""
    if not client.exists("Item", PRICE_MUTATION_ITEM):
        client.insert({"doctype": "Item", "item_code": PRICE_MUTATION_ITEM,
                       "item_name": "Throwaway price-mutation item", "item_group": ITEM_GROUP,
                       "stock_uom": "Nos", "is_stock_item": 0,
                       "description": "Dedicated to the auto_insert_price_list_rate_if_missing "
                                      "finding (derivation_map.md finding 5). Self-cleaned "
                                      "before every run by ensure_price_mutation_item()."})
        print(f"  item: created {PRICE_MUTATION_ITEM} (throwaway, self-cleaning)")
    else:
        print(f"  item: {PRICE_MUTATION_ITEM} exists (throwaway, self-cleaning)")
    stray = client.call("frappe.client.get_list", doctype="Item Price",
                        filters={"item_code": PRICE_MUTATION_ITEM},
                        fields=["name", "price_list_rate"], limit_page_length=0) or []
    for pr in stray:
        client.call("frappe.client.delete", doctype="Item Price", name=pr["name"])
        print(f"    cleaned stray Item Price {pr['name']} "
              f"({PRICE_MUTATION_ITEM} @ {pr['price_list_rate']}) left by the last run")


def assert_clean_fixtures(client: FrappeClient) -> list[str]:
    """Fail loudly when shared state has drifted.

    Learned the hard way. A probe left `income_account = Interest Income` on the
    fixture item's defaults, so every invoice booked revenue to the wrong
    account. Debits still equalled credits, so nothing that checks arithmetic
    noticed. Ten scenarios failed with confusing messages instead of one clear
    one. Another probe left an active Pricing Rule that silently moved 250 to
    225. A corpus that asserts accounting facts is worthless if its own
    fixtures are quietly wrong, so check them first and say so plainly.
    """
    problems = []

    item = client.get_doc("Item", ITEM_CODE)
    for d in item.get("item_defaults") or []:
        if d.get("income_account"):
            problems.append(
                f"{ITEM_CODE} has a default income_account of {d['income_account']!r}. "
                f"Revenue will not post to Sales, and the books will still balance.")

    box = next((u for u in item.get("uoms") or [] if u.get("uom") == BOX_UOM), None)
    if box and abs(float(box.get("conversion_factor") or 0) - BOX_CONVERSION_FACTOR) > 1e-6:
        problems.append(
            f"{ITEM_CODE} uom {BOX_UOM} conversion_factor is {box.get('conversion_factor')}, "
            f"expected {BOX_CONVERSION_FACTOR}. conversion_factor scenario totals assume this.")

    fx = client.call("frappe.client.get_list", doctype="Currency Exchange",
                     filters={"from_currency": FOREIGN_CURRENCY, "to_currency": CURRENCY,
                              "date": FX_RATE_DATE}, fields=["exchange_rate"],
                     limit_page_length=0) or []
    if not fx or abs(float(fx[0]["exchange_rate"]) - FX_RATE) > 1e-6:
        problems.append(
            f"Currency Exchange {FOREIGN_CURRENCY}->{CURRENCY} on {FX_RATE_DATE} is "
            f"{fx[0]['exchange_rate'] if fx else 'missing'}, expected {FX_RATE}. "
            f"conversion_rate scenario totals assume this.")

    rules = client.call("frappe.client.get_list", doctype="Pricing Rule",
                        filters={"disable": 0}, fields=["name", "title"],
                        limit_page_length=0) or []
    for r in rules:
        problems.append(
            f"active Pricing Rule {r['name']} ({r.get('title')}) will move derived "
            f"prices, so hardcoded expected totals will not match.")

    prices = client.call("frappe.client.get_list", doctype="Item Price",
                         filters={"item_code": UNPRICED_ITEM},
                         fields=["name", "price_list_rate"], limit_page_length=0) or []
    for pr in prices:
        problems.append(
            f"{UNPRICED_ITEM} has an Item Price of {pr['price_list_rate']}. It is meant "
            f"to have none. Note ERPNext creates these by itself when "
            f"Stock Settings.auto_insert_price_list_rate_if_missing is set.")

    dated = client.call("frappe.client.get_list", doctype="Item Price",
                        filters={"item_code": ITEM_CODE},
                        fields=["name", "price_list", "valid_from"],
                        limit_page_length=0) or []
    for row in dated:
        if row.get("valid_from") and str(row["valid_from"]) > PRICE_VALID_FROM:
            problems.append(
                f"Item Price {row['name']} on {row['price_list']} is only valid from "
                f"{row['valid_from']}. Scenarios posting before that date will find no "
                f"price and the whole suite will fail for a reason that looks like "
                f"something else.")

    real = client.call("frappe.client.get_list", doctype="Item Price",
                       filters={"item_code": ITEM_CODE, "price_list": PRICE_LIST},
                       fields=["price_list_rate"], limit_page_length=0) or []
    if not real or abs(float(real[0]["price_list_rate"]) - LIST_PRICE) > 0.005:
        problems.append(
            f"{ITEM_CODE} list price is {real[0]['price_list_rate'] if real else None}, "
            f"expected {LIST_PRICE}.")
    return problems


def ensure_all(client: FrappeClient) -> dict:
    print("fixtures:")
    ensure_setup(client)
    ensure_item(client)
    ensure_customer(client)
    ensure_supplier(client)
    ensure_unpriced_item(client)
    ensure_foreign_currency(client)
    ensure_box_uom(client)
    ensure_price_mutation_item(client)
    problems = assert_clean_fixtures(client)
    if problems:
        print("\n  FIXTURE STATE IS NOT CLEAN:")
        for p in problems:
            print(f"    - {p}")
        print("  Results below are measuring a contaminated instance.\n")
    else:
        print("  precheck: fixture state clean")

    company = (
        client.call("frappe.client.get_list", doctype="Company", fields=["name"], limit_page_length=0)
        or [{"name": COMPANY}]
    )[0]["name"]
    return {
        "company": company,
        "item_code": ITEM_CODE,
        "unpriced_item": UNPRICED_ITEM,
        "customer": CUSTOMER,
        "price_list": PRICE_LIST,
        "list_price": LIST_PRICE,
        "supplier": SUPPLIER,
        "buy_price_list": BUY_PRICE_LIST,
        "foreign_currency": FOREIGN_CURRENCY,
        "fx_rate": FX_RATE,
        "box_uom": BOX_UOM,
        "box_conversion_factor": BOX_CONVERSION_FACTOR,
        "price_mutation_item": PRICE_MUTATION_ITEM,
    }
