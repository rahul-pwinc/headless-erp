"""Differential oracle for ERPNext document derivation.

The Desk UI derives item fields (rate, price_list_rate, income_account, tax
template, UOM conversion...) by calling a whitelisted server method before the
document is ever saved. A caller that speaks only to /api/resource never
triggers that step.

Server-side, `AccountsController.set_missing_item_details` fills a field ONLY
when it is None (accounts_controller.py:1127 on v16.34.1, :785 on v17-dev),
plus 9 always-forced fields in `force_item_fields`. `rate` and
`price_list_rate` are in neither set.

That produces two materially different outcomes, and this module measures both:

  OMIT   - caller leaves the field out entirely.
           Server fills it from the Price List. Expect NO gap.
           This is the control: it proves the derivation exists server-side.

  ASSERT - caller supplies its own value.
           Server keeps it, silently. Expect a gap.
           This is the defect: the UI would never have produced this document,
           and every accounting invariant still holds afterwards.

Only the ASSERT/OMIT contrast makes the finding falsifiable. Showing ASSERT
alone would not distinguish "the server never derives" from "the server
declines to overwrite".
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, asdict
from typing import Any

from client import FrappeClient

# Item-row fields the Desk UI derives via get_item_details before save.
# Compared verbatim between the UI-replay document and the API document.
DERIVED_ITEM_FIELDS = [
    "rate",
    "price_list_rate",
    "uom",
    "conversion_factor",
    "stock_uom",
    "income_account",
    "expense_account",
    "cost_center",
    "item_tax_template",
    "discount_percentage",
    "item_name",
    "description",
    "item_group",
    "warehouse",
]


@dataclass
class Gap:
    doctype: str
    row_idx: int
    fieldname: str
    ui_value: Any
    api_value: Any
    mode: str  # "omit" | "assert"

    @property
    def is_gap(self) -> bool:
        return not _equalish(self.ui_value, self.api_value)


@dataclass
class CaseResult:
    case_name: str
    mode: str
    ui_doc_name: str | None = None
    api_doc_name: str | None = None
    gaps: list[Gap] = field(default_factory=list)
    ui_grand_total: float | None = None
    api_grand_total: float | None = None
    api_doc_balances: bool | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["gaps"] = [asdict(g) for g in self.gaps if g.is_gap]
        d["gap_count"] = len(d["gaps"])
        return d


def _equalish(a: Any, b: Any) -> bool:
    """Numeric-tolerant equality; ERPNext returns floats as str in places."""
    if a is None and b is None:
        return True
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        try:
            return abs(float(a or 0) - float(b or 0)) < 1e-6
        except (TypeError, ValueError):
            pass
    return (a or "") == (b or "")


def build_ctx(parent: dict, item_row: dict) -> dict:
    """Reproduce the ctx object erpnext/public/js/controllers/transaction.js
    builds at process_item_selection() (v16.34.1 line ~818) before calling
    get_item_details. Field-for-field, so the replay is defensible.
    """
    return {
        "item_code": item_row.get("item_code"),
        "barcode": None,
        "serial_no": None,
        "batch_no": None,
        "set_warehouse": parent.get("set_warehouse"),
        "warehouse": item_row.get("warehouse"),
        "customer": parent.get("customer") or parent.get("party_name"),
        "quotation_to": parent.get("quotation_to"),
        "supplier": parent.get("supplier"),
        "currency": parent.get("currency"),
        "is_internal_supplier": parent.get("is_internal_supplier"),
        "is_internal_customer": parent.get("is_internal_customer"),
        "update_stock": parent.get("update_stock", 0),
        "conversion_rate": parent.get("conversion_rate", 1),
        "price_list": parent.get("selling_price_list") or parent.get("buying_price_list"),
        "price_list_currency": parent.get("price_list_currency"),
        "plc_conversion_rate": parent.get("plc_conversion_rate", 1),
        "company": parent.get("company"),
        "order_type": parent.get("order_type"),
        "is_pos": 0,
        "is_return": 0,
        "is_subcontracted": parent.get("is_subcontracted"),
        "ignore_pricing_rule": parent.get("ignore_pricing_rule"),
        "doctype": parent.get("doctype"),
        "name": parent.get("name"),
        "project": item_row.get("project") or parent.get("project"),
        "qty": item_row.get("qty") or 1,
        # NOTE: the JS sends net_rate (not rate) — an empty row has no rate yet.
        "net_rate": item_row.get("rate"),
        "base_net_rate": item_row.get("base_net_rate"),
        "stock_qty": item_row.get("stock_qty"),
        "conversion_factor": item_row.get("conversion_factor"),
        "weight_per_unit": 0,
        "uom": item_row.get("uom"),
        "weight_uom": "",
        "manufacturer": item_row.get("manufacturer"),
        "stock_uom": item_row.get("stock_uom"),
        "pos_profile": "",
        "cost_center": item_row.get("cost_center"),
        "tax_category": parent.get("tax_category"),
        "item_tax_template": item_row.get("item_tax_template"),
        "child_doctype": f"{parent['doctype']} Item",
        "child_docname": "new-row-1",
        "use_serial_batch_fields": 0,
        "serial_and_batch_bundle": None,
    }


def ui_replay_doc(client: FrappeClient, intent: dict) -> dict:
    """The document a human at the Desk UI would have produced.

    Builds the header, then for each line calls the same whitelisted method the
    browser calls, and merges the returned values the way the JS callback does.
    """
    parent = copy.deepcopy(intent["header"])
    rows = []
    for line in intent["lines"]:
        row = {"item_code": line["item_code"], "qty": line["qty"]}
        # get_item_details declares doc as `Document | str | None` and does
        # `json.loads(doc)` when it is a str (get_item_details.py:118 on
        # v16.34.1). frappe.call in the browser serialises it the same way.
        derived = client.call(
            "erpnext.stock.get_item_details.get_item_details",
            doc=json.dumps(parent),
            ctx=build_ctx(parent, row),
        ) or {}
        # The JS callback writes every returned key onto the child row.
        merged = {k: v for k, v in derived.items() if v is not None}
        merged.update({"item_code": line["item_code"], "qty": line["qty"]})
        rows.append(merged)
    doc = dict(parent)
    doc["items"] = rows
    return doc


def api_doc(intent: dict, mode: str) -> dict:
    """The document a naive API caller / MCP server / agent produces.

    mode="omit"   -> no rate supplied (control)
    mode="assert" -> caller supplies its own rate (the defect)
    """
    doc = copy.deepcopy(intent["header"])
    rows = []
    for line in intent["lines"]:
        row = {"item_code": line["item_code"], "qty": line["qty"]}
        if mode == "assert":
            row["rate"] = line["asserted_rate"]
        rows.append(row)
    doc["items"] = rows
    return doc


def gl_balances(client: FrappeClient, doctype: str, name: str) -> bool | None:
    """Sum debit/credit of the GL entries this voucher produced.

    Returns None for a draft (a draft posts nothing). The point of this check:
    a document with a wrong rate still balances perfectly, so the accounting
    invariant cannot be used to detect the defect.
    """
    entries = client.call(
        "frappe.client.get_list",
        doctype="GL Entry",
        filters={"voucher_type": doctype, "voucher_no": name, "is_cancelled": 0},
        fields=["debit", "credit"],
        limit_page_length=0,
    ) or []
    if not entries:
        return None
    total = sum(float(e.get("debit") or 0) - float(e.get("credit") or 0) for e in entries)
    return abs(total) < 1e-6


def run_case(client: FrappeClient, intent: dict, mode: str) -> CaseResult:
    res = CaseResult(case_name=intent["name"], mode=mode)
    try:
        ui = ui_replay_doc(client, intent)
        api_payload = api_doc(intent, mode)

        ui_saved = client.insert(ui)
        api_saved = client.insert(api_payload)
        # Submit both: docstatus 0 -> 1 is what writes GL entries. Without this
        # the "books still balance" claim is untested.
        ui_saved = client.submit(ui_saved)
        api_saved = client.submit(api_saved)
        res.ui_doc_name = ui_saved.get("name")
        res.api_doc_name = api_saved.get("name")
        res.ui_grand_total = ui_saved.get("grand_total")
        res.api_grand_total = api_saved.get("grand_total")

        for idx, (u_row, a_row) in enumerate(zip(ui_saved.get("items", []), api_saved.get("items", []))):
            for fname in DERIVED_ITEM_FIELDS:
                res.gaps.append(
                    Gap(
                        doctype=intent["header"]["doctype"],
                        row_idx=idx,
                        fieldname=fname,
                        ui_value=u_row.get(fname),
                        api_value=a_row.get(fname),
                        mode=mode,
                    )
                )
        res.api_doc_balances = gl_balances(client, api_saved["doctype"], api_saved["name"])
    except Exception as exc:  # noqa: BLE001 - harness reports, never crashes the run
        res.error = f"{type(exc).__name__}: {exc}"
    return res
