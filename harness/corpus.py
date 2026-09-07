"""Phase 5 — the use-case corpus engine.

Every scenario states an accounting *principle* and asserts against it. The
oracle is double-entry bookkeeping, not ERPNext's behaviour. If a scenario
fails, that is a finding: either the intent layer is wrong, or ERPNext does
something an accountant would not expect. Both are worth knowing, and neither
is discoverable by diffing against the UI.

Assertions available to a scenario:

  balanced           the voucher's GL entries net to zero
  gl                 an account is debited/credited by an amount
  grand_total        the document total
  outstanding        remaining receivable/payable on a document
  status             document status string
  docstatus          0 draft, 1 submitted, 2 cancelled
  refused            the intent must refuse, optionally matching text
  no_gl              the intent must post nothing to the ledger
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from client import FrappeClient
from intent import IntentEngine, IntentRefused


@dataclass
class ScenarioResult:
    id: str
    category: str
    principle: str
    passed: bool = False
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)


class CorpusRunner:
    def __init__(self, client: FrappeClient, engine: IntentEngine, fx: dict):
        self.c, self.eng, self.fx = client, engine, fx

    # ---- helpers -------------------------------------------------------------

    def _gl(self, doctype: str, name: str) -> list[dict]:
        return self.c.call(
            "frappe.client.get_list", doctype="GL Entry",
            filters={"voucher_type": doctype, "voucher_no": name, "is_cancelled": 0},
            fields=["account", "debit", "credit"], limit_page_length=0) or []

    def _resolve(self, v: Any, ctx: dict) -> Any:
        if isinstance(v, str) and v.startswith("$"):
            return ctx.get(v[1:])
        return v

    def _header(self, intent_id: str, extra: dict | None = None) -> dict:
        sell = {"customer": self.fx["customer"], "selling_price_list": self.fx["price_list"]}
        buy = {"supplier": self.fx["supplier"], "buying_price_list": self.fx["buy_price_list"]}
        base = {"company": self.fx["company"], "currency": "INR",
                "price_list_currency": "INR",
                "posting_date": "2026-09-07", "transaction_date": "2026-09-07"}
        if intent_id in ("quote", "sell", "fulfil", "bill", "return"):
            base.update(sell)
        if intent_id in ("procure", "receive", "expense", "source"):
            base.update(buy)
        if intent_id == "bill":
            base.update({"due_date": "2026-10-07", "update_stock": 0})
        if intent_id == "expense":
            base.update({"due_date": "2026-10-07"})
        if intent_id == "sell":
            base["delivery_date"] = "2026-10-07"
        if intent_id in ("procure", "source"):
            base["schedule_date"] = "2026-10-07"
        if intent_id == "quote":
            base.update({"quotation_to": "Customer", "party_name": self.fx["customer"],
                         "valid_till": "2026-10-07"})
            base.pop("customer", None)
        base.update(extra or {})
        return base

    # ---- assertions ----------------------------------------------------------

    def _assert(self, a: dict, ctx: dict, res: ScenarioResult) -> None:
        kind = a["assert"]
        doc = self._resolve(a.get("doc", "$last"), ctx)

        if kind == "refused":
            return  # handled at the action level

        if kind in ("balanced", "gl", "no_gl"):
            if not doc:
                res.failures.append(f"{kind}: no document in context"); return
            dt, name = doc
            rows = self._gl(dt, name)
            if kind == "no_gl":
                if rows:
                    res.failures.append(f"no_gl: expected no ledger movement, found {len(rows)} entries")
                return
            if kind == "balanced":
                d = sum(float(r["debit"] or 0) for r in rows)
                c = sum(float(r["credit"] or 0) for r in rows)
                if not rows:
                    res.failures.append("balanced: no GL entries posted")
                elif abs(d - c) >= 0.01:
                    res.failures.append(f"balanced: Dr {d} != Cr {c}")
                return
            frag, side, amt = a["account"], a.get("side", "debit"), float(a["amount"])
            got = sum(float(r[side] or 0) for r in rows if frag.lower() in r["account"].lower())
            if abs(got - amt) >= 0.01:
                res.failures.append(
                    f"gl: {side} on '{frag}' expected {amt}, got {got} "
                    f"(accounts seen: {sorted({r['account'] for r in rows})})")
            return

        if kind in ("grand_total", "outstanding", "status", "docstatus"):
            if not doc:
                res.failures.append(f"{kind}: no document in context"); return
            dt, name = doc
            d = self.c.get_doc(dt, name)
            fieldmap = {"grand_total": "grand_total", "outstanding": "outstanding_amount",
                        "status": "status", "docstatus": "docstatus"}
            got = d.get(fieldmap[kind])
            want = a["value"]
            if kind in ("grand_total", "outstanding"):
                if abs(float(got or 0) - float(want)) >= 0.01:
                    res.failures.append(f"{kind}: expected {want}, got {got}")
            elif got != want:
                res.failures.append(f"{kind}: expected {want!r}, got {got!r}")
            return

        res.failures.append(f"unknown assertion {kind!r}")

    # ---- run -----------------------------------------------------------------

    def run(self, sc: dict) -> ScenarioResult:
        res = ScenarioResult(sc["id"], sc.get("category", "-"), sc.get("principle", ""))
        ctx: dict[str, Any] = {}
        expect_refused = [a for a in sc.get("then", []) if a.get("assert") == "refused"]

        try:
            for step in sc.get("when", []):
                intent_id = step["intent"]
                if intent_id in self.eng.ITEMLESS:
                    allocs = [{**a, "doc": self._resolve(a["doc"], ctx)}
                              for a in step.get("allocate", [])]
                    hdr = {"company": self.fx["company"],
                           "posting_date": "2026-09-07",
                           "party": self.fx["customer"] if intent_id == "collect"
                                    else self.fx["supplier"],
                           **(step.get("header") or {})}
                    r = self.eng.execute_payment(intent_id, hdr, allocs,
                                                 submit=step.get("submit", True))
                    ctx["last"] = (r.doctype, r.name)
                    ctx[step.get("as", intent_id)] = (r.doctype, r.name)
                    res.docs.append(f"{r.doctype}/{r.name}")
                    if r.invariant_failures:
                        res.failures.extend(f"invariant: {f}" for f in r.invariant_failures)
                    continue
                header = self._header(intent_id, step.get("header"))
                lines = [{"item_code": self.fx[l.get("item", "item_code")]
                          if l.get("item", "item_code") in self.fx else l.get("item"),
                          "qty": l.get("qty", 1), **{k: v for k, v in l.items()
                                                     if k not in ("item", "qty")}}
                         for l in step.get("lines", [])]
                r = self.eng.execute(intent_id, header, lines,
                                     overrides=step.get("overrides"),
                                     submit=step.get("submit", True))
                ctx["last"] = (r.doctype, r.name)
                ctx[step.get("as", intent_id)] = (r.doctype, r.name)
                res.docs.append(f"{r.doctype}/{r.name}")
                if r.invariant_failures:
                    res.failures.extend(f"invariant: {f}" for f in r.invariant_failures)
        except IntentRefused as e:
            if expect_refused:
                pat = expect_refused[0].get("matching")
                if pat and not re.search(pat, str(e), re.I):
                    res.failures.append(f"refused, but not matching {pat!r}: {str(e)[:120]}")
                res.passed = not res.failures
                res.notes.append(f"refused: {str(e).splitlines()[0]}")
                return res
            res.failures.append(f"unexpected refusal: {str(e).splitlines()[0]}")
            res.passed = False
            return res
        except Exception as e:  # noqa: BLE001
            res.failures.append(f"{type(e).__name__}: {str(e)[:200]}")
            res.passed = False
            return res

        if expect_refused:
            res.failures.append("expected a refusal, the intent succeeded")

        for a in sc.get("then", []):
            self._assert(a, ctx, res)

        res.passed = not res.failures
        return res
