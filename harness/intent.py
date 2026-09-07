"""Phase 4 — the intent executor.

The Phase 2 census showed ERPNext silently accepts caller-supplied values for
five fields it knows how to derive. This module closes that, not by patching
ERPNext, but by refusing to be the kind of caller that exploits it.

The contract, per intents/catalog.yaml:

    refuse    the caller may not supply it at all. No Desk session could.
    override  the caller may supply it, but only with a reason, and the derived
              value is computed first so the delta is recorded.
    derive    the system fills it. Callers never send it.

A caller that omits everything gets a correct document. A caller that lies gets
an error, not a silently wrong invoice.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any

import yaml

from client import FrappeClient, FrappeError
from oracle import build_ctx


class IntentRefused(Exception):
    """The caller asked for something the intent will not do."""


@dataclass
class Override:
    fieldname: str
    derived_value: Any
    supplied_value: Any
    reason: str
    row_idx: int = 0


@dataclass
class IntentResult:
    intent: str
    doctype: str
    name: str | None = None
    docstatus: int | None = None
    grand_total: float | None = None
    overrides: list[Override] = field(default_factory=list)
    derived: dict = field(default_factory=dict)
    invariants_checked: list[str] = field(default_factory=list)
    invariant_failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = not self.invariant_failures
        return d


class IntentEngine:
    def __init__(self, client: FrappeClient, catalog_path: str = "intents/catalog.yaml"):
        self.client = client
        self.catalog = yaml.safe_load(open(catalog_path))
        self.intents = {i["id"]: i for i in self.catalog["intents"]}
        self.defaults = self.catalog.get("defaults", {})

    # ---- contract resolution -------------------------------------------------

    def _refused(self, spec: dict) -> dict[str, dict]:
        out = {r["field"]: r for r in self.defaults.get("refuse", [])}
        out.update({r["field"]: r for r in (spec.get("refuse") or [])})
        return out

    def _overridable(self, spec: dict) -> dict[str, dict]:
        out = {o["field"]: o for o in self.defaults.get("override", [])}
        out.update({o["field"]: o for o in (spec.get("override") or [])})
        # An intent-level refuse beats a default override (see `return`).
        for f in (spec.get("refuse") or []):
            out.pop(f["field"], None)
        return out

    # ---- execution -----------------------------------------------------------

    # ---- payment intents (no item rows) --------------------------------------

    ITEMLESS = {"collect", "pay"}

    def execute_payment(
        self, intent_id: str, header: dict, allocations: list[dict] | None = None,
        submit: bool = True,
    ) -> IntentResult:
        """Payment Entry has no item table, so the derive/refuse contract applies
        to allocation instead: outstanding comes from the referenced documents,
        never from the caller, and the total allocated may not exceed what is paid.
        """
        spec = self.intents[intent_id]
        doctype = spec["maps_to"]["doctype"]
        res = IntentResult(intent=intent_id, doctype=doctype)
        is_receive = intent_id == "collect"

        refs, allocated = [], 0.0
        for a in (allocations or []):
            ref_dt, ref_name = a["doc"]
            live = self.client.get_doc(ref_dt, ref_name)
            outstanding = float(live.get("outstanding_amount") or 0)
            if "outstanding" in a:
                raise IntentRefused(
                    f"{intent_id}: outstanding is derived from {ref_name}, not supplied."
                )
            amt = float(a.get("amount", outstanding))
            if amt - outstanding > 1e-6:
                raise IntentRefused(
                    f"{intent_id}: allocating {amt} to {ref_name} exceeds its "
                    f"outstanding of {outstanding}."
                )
            allocated += amt
            refs.append({"reference_doctype": ref_dt, "reference_name": ref_name,
                         "total_amount": float(live.get("grand_total") or 0),
                         "outstanding_amount": outstanding, "allocated_amount": amt})
            res.derived[ref_name] = {"outstanding": outstanding}

        paid = float(header.get("paid_amount") or allocated)
        if allocated - paid > 1e-6:
            raise IntentRefused(
                f"{intent_id}: allocated {allocated} exceeds paid_amount {paid}."
            )

        party_acct_type = "Receivable" if is_receive else "Payable"
        party_type = "Customer" if is_receive else "Supplier"
        bank = self._default_bank(header["company"])
        doc = {
            "doctype": doctype,
            "payment_type": "Receive" if is_receive else "Pay",
            "company": header["company"],
            "posting_date": header.get("posting_date"),
            "party_type": party_type,
            "party": header["party"],
            "paid_amount": paid,
            "received_amount": paid,
            "source_exchange_rate": 1,
            "target_exchange_rate": 1,
            "references": refs,
        }
        doc["paid_to" if is_receive else "paid_from"] = bank
        saved = self.client.insert(doc)
        if submit:
            saved = self.client.submit(saved)
        res.name, res.docstatus = saved.get("name"), saved.get("docstatus")
        res.grand_total = saved.get("paid_amount")
        self._check_invariants(spec, saved, res)
        return res

    def _default_bank(self, company: str) -> str:
        rows = self.client.call(
            "frappe.client.get_list", doctype="Account",
            filters={"company": company, "account_type": ["in", ["Bank", "Cash"]],
                     "is_group": 0}, fields=["name"], limit_page_length=0) or []
        if not rows:
            raise IntentRefused(f"no Bank or Cash account on {company}")
        return rows[0]["name"]

    def execute(
        self,
        intent_id: str,
        header: dict,
        lines: list[dict],
        overrides: list[dict] | None = None,
        submit: bool = True,
    ) -> IntentResult:
        if intent_id not in self.intents:
            raise IntentRefused(f"unknown intent {intent_id!r}")
        spec = self.intents[intent_id]
        doctype = spec["maps_to"]["doctype"]
        refused = self._refused(spec)
        overridable = self._overridable(spec)
        res = IntentResult(intent=intent_id, doctype=doctype)

        # 1. A refused field in the payload is an error, never a silent accept.
        for idx, line in enumerate(lines):
            for fname in line:
                if fname in refused:
                    r = refused[fname]
                    raise IntentRefused(
                        f"{intent_id}: line {idx} may not supply {fname!r}.\n"
                        f"  reason:   {r['reason'].strip()}\n"
                        f"  evidence: {r.get('evidence', 'n/a')}"
                    )

        # 2. Overrides must be declared, with a reason, for an overridable field.
        want: dict[tuple[int, str], dict] = {}
        for o in overrides or []:
            f = o.get("field")
            if f in refused:
                raise IntentRefused(f"{intent_id}: {f!r} is refused and cannot be overridden.")
            if f not in overridable:
                raise IntentRefused(
                    f"{intent_id}: {f!r} is derived and not overridable. "
                    f"Overridable here: {sorted(overridable)}"
                )
            if not (o.get("reason") or "").strip():
                raise IntentRefused(f"{intent_id}: override of {f!r} requires a reason.")
            want[(o.get("row", 0), f)] = o

        # 3. Derive every line from the server, the way the system intends.
        parent = dict(header)
        parent["doctype"] = doctype
        rows: list[dict] = []
        for idx, line in enumerate(lines):
            probe = {"item_code": line["item_code"], "qty": line.get("qty", 1)}
            derived = self.client.call(
                "erpnext.stock.get_item_details.get_item_details",
                doc=json.dumps(parent),
                ctx=build_ctx(parent, probe),
            ) or {}
            if derived.get("price_list_rate") in (None, 0):
                raise IntentRefused(
                    f"{intent_id}: line {idx}: no price is resolvable for "
                    f"{line['item_code']!r} in {parent.get('selling_price_list') or parent.get('buying_price_list')!r}. "
                    f"Refusing to guess a rate."
                )
            row = {k: v for k, v in derived.items() if v is not None}
            row.update(probe)
            res.derived[f"line{idx}"] = {
                "price_list_rate": derived.get("price_list_rate"),
                "rate": derived.get("rate"),
            }

            # 4. Apply declared overrides on top of the derived value, recorded.
            for fname in list(overridable):
                o = want.get((idx, fname))
                if not o:
                    continue
                # get_item_details returns rate=0 when no explicit rate was in
                # ctx (get_item_details.py:198 -> `out.rate = ctx.rate or
                # out.price_list_rate`, and callers see the pre-fallback value).
                # Recording 0 as "the derived value" would tell an auditor the
                # baseline was zero, which is false and defeats the purpose of
                # the log. Fall back to the reference price.
                derived_value = derived.get(fname)
                if fname == "rate" and not derived_value:
                    derived_value = derived.get("price_list_rate")
                res.overrides.append(
                    Override(fname, derived_value, o["value"], o["reason"].strip(), idx)
                )
                row[fname] = o["value"]
            rows.append(row)

        doc = dict(parent)
        doc["items"] = rows
        if res.overrides:
            doc["remarks"] = "; ".join(
                f"override {o.fieldname}: {o.derived_value} -> {o.supplied_value} ({o.reason})"
                for o in res.overrides
            )

        saved = self.client.insert(doc)
        if submit and spec["maps_to"].get("submittable"):
            saved = self.client.submit(saved)
        res.name = saved.get("name")
        res.docstatus = saved.get("docstatus")
        res.grand_total = saved.get("grand_total")

        self._check_invariants(spec, saved, res)
        return res

    # ---- postcondition invariants -------------------------------------------

    def _check_invariants(self, spec: dict, saved: dict, res: IntentResult) -> None:
        if spec["maps_to"].get("posts_gl") is True and saved.get("docstatus") == 1:
            entries = self.client.call(
                "frappe.client.get_list", doctype="GL Entry",
                filters={"voucher_type": saved["doctype"], "voucher_no": saved["name"],
                         "is_cancelled": 0},
                fields=["debit", "credit"], limit_page_length=0) or []
            if entries:
                delta = sum(float(e.get("debit") or 0) - float(e.get("credit") or 0) for e in entries)
                res.invariants_checked.append("debits == credits")
                if abs(delta) >= 1e-6:
                    res.invariant_failures.append(f"debits != credits (delta {delta})")

        items = saved.get("items") or []
        if items and saved.get("grand_total") is not None:
            net = sum(float(i.get("net_amount") or 0) for i in items)
            taxes = float(saved.get("total_taxes_and_charges") or 0)
            res.invariants_checked.append("grand_total == sum(net_amount) + taxes")
            if abs((net + taxes) - float(saved["grand_total"])) >= 0.01:
                res.invariant_failures.append(
                    f"grand_total {saved['grand_total']} != net {net} + taxes {taxes}")

        # An override must leave the document self-consistent: the delta from the
        # untouched reference price has to be recorded somewhere.
        #
        # ERPNext records it in one of two places depending on direction
        # (transaction.js:70-90, confirmed empirically on live documents):
        #   rate <  price_list_rate  ->  discount_amount = plr - rate
        #   rate >  price_list_rate  ->  margin_type="Amount",
        #                                margin_rate_or_amount = rate - plr
        # Checking only the discount direction silently passes every premium
        # sale. In the UCI Online Retail II data, 21.3% of real lines price
        # ABOVE list, so the one-directional check was wrong far more often
        # than it was right.
        for o in res.overrides:
            if o.fieldname != "rate":
                continue
            row = items[o.row_idx] if o.row_idx < len(items) else {}
            plr = float(row.get("price_list_rate") or 0)
            rate = float(row.get("rate") or 0)
            delta = plr - rate
            if abs(delta) < 0.005:
                continue
            if delta > 0:
                recorded = float(row.get("discount_amount") or 0)
                label = "discount_amount == price_list_rate - rate"
            else:
                recorded = -float(row.get("margin_rate_or_amount") or 0)
                label = "margin_rate_or_amount == rate - price_list_rate"
            res.invariants_checked.append(label)
            if abs(delta - recorded) >= 0.01:
                res.invariant_failures.append(
                    f"row {o.row_idx}: {label} violated "
                    f"(plr={plr} rate={rate} recorded={abs(recorded)})")
