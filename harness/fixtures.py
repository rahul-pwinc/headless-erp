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
LIST_PRICE = 250.0          # the Price List rate the UI would derive
CUSTOMER = "Headless Test Customer"
SUPPLIER = "Headless Test Supplier"
UNPRICED_ITEM = "HL-UNPRICED-001"  # deliberately has no Item Price anywhere
BUY_PRICE_LIST = "Standard Buying"
BUY_PRICE = 120.0


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
                       "price_list": BUY_PRICE_LIST, "price_list_rate": BUY_PRICE})
        print(f"  price:  {BUY_PRICE_LIST} = {BUY_PRICE} (created)")


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
    }
