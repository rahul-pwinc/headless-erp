"""Phase 2 — silent-acceptance census.

For every transaction doctype that routes through AccountsController's
derivation path, ask the server what it *would* derive for an item row, then
test each derived field empirically:

  supply a different value  ->  save  ->  read back

  value stuck      = SILENTLY ACCEPTED. The server had a derivation available
                     and used the caller's number instead, without a signal.
  value overwritten = PROTECTED. The server asserted its own derivation.

This is the census. It produces the number: across N doctypes, how many of the
fields the system knows how to derive will it silently take from a caller.

No browser. No UI. Every call here is one an agent or MCP server can make.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Any

from client import FrappeClient, FrappeError

# accounts_controller.py:97 (v16.34.1) — the only fields the server re-asserts.
FORCE_ITEM_FIELDS = {
    "item_group", "brand", "stock_uom", "is_fixed_asset", "pricing_rules",
    "weight_per_unit", "weight_uom", "total_weight", "valuation_rate",
}

# Fields that are structural rather than derived business values.
SKIP_FIELDS = {"item_code", "item_name", "description", "doctype", "name",
               "parent", "parenttype", "parentfield", "idx", "owner"}


@dataclass
class FieldProbe:
    doctype: str
    fieldname: str
    derived_value: Any
    supplied_value: Any
    stored_value: Any
    verdict: str          # "silently_accepted" | "protected" | "rejected" | "skipped"
    note: str = ""


def _mutate(value: Any) -> Any:
    """A different, still-plausible value of the same shape."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        v = float(value)
        if v == 0:
            return 7.0
        # deliberately far from the derived value so a partial recompute is visible
        return round(v * 0.004, 6) or 1.0
    return None  # non-numeric: not probed in v0


def probe_doctype(
    client: FrappeClient, doctype: str, header: dict, item_code: str, qty: float = 4
) -> list[FieldProbe]:
    """Probe every numeric field the server derives for one item row."""
    from oracle import build_ctx

    probes: list[FieldProbe] = []
    row = {"item_code": item_code, "qty": qty}
    try:
        derived = client.call(
            "erpnext.stock.get_item_details.get_item_details",
            doc=json.dumps(header),
            ctx=build_ctx(header, row),
        ) or {}
    except FrappeError as exc:
        return [FieldProbe(doctype, "*", None, None, None, "rejected",
                           f"get_item_details failed: {str(exc)[:180]}")]

    child_dt = f"{doctype} Item"
    try:
        meta_fields = {
            f["fieldname"]
            for f in (client.call("frappe.client.get_list", doctype="DocField",
                                  filters={"parent": child_dt}, fields=["fieldname"],
                                  parent=child_dt, limit_page_length=0) or [])
        }
    except FrappeError:
        meta_fields = set()

    for fname, dval in sorted(derived.items()):
        if fname in SKIP_FIELDS or dval is None:
            continue
        if meta_fields and fname not in meta_fields:
            continue
        supplied = _mutate(dval)
        if supplied is None or supplied == dval:
            continue

        doc = dict(header)
        doc["items"] = [{"item_code": item_code, "qty": qty, fname: supplied}]
        try:
            saved = client.insert(doc)
        except FrappeError as exc:
            probes.append(FieldProbe(doctype, fname, dval, supplied, None, "rejected",
                                     str(exc)[:160]))
            continue

        stored = (saved.get("items") or [{}])[0].get(fname)
        try:
            stuck = abs(float(stored or 0) - float(supplied or 0)) < 1e-6
        except (TypeError, ValueError):
            stuck = stored == supplied
        probes.append(
            FieldProbe(doctype, fname, dval, supplied, stored,
                       "silently_accepted" if stuck else "protected",
                       "in force_item_fields" if fname in FORCE_ITEM_FIELDS else "")
        )
    return probes


def summarise(probes: list[FieldProbe]) -> dict:
    by_verdict: dict[str, int] = {}
    for p in probes:
        by_verdict[p.verdict] = by_verdict.get(p.verdict, 0) + 1
    accepted = [p for p in probes if p.verdict == "silently_accepted"]
    protected = [p for p in probes if p.verdict == "protected"]
    return {
        "probes_run": len(probes),
        "by_verdict": by_verdict,
        "doctypes_probed": sorted({p.doctype for p in probes}),
        "silently_accepted_fields": sorted({p.fieldname for p in accepted}),
        "protected_fields": sorted({p.fieldname for p in protected}),
        "probes": [asdict(p) for p in probes],
    }
