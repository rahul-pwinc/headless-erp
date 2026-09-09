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

Every accepted override is written to an `Intent Override Log` record — a
standalone, submittable, hash-chained doctype (harness/audit.py) holding what
the server derived, what the caller requested, and what the document actually
stored. Only `rate` can produce one: the other nine fields in the contract are
refused outright, so there is nothing to record about them beyond the refusal.

If that record cannot be written, the document is undone and the call raises.
See IntentEngine._record_overrides for why that is the right trade.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from typing import Any

import yaml

import audit
from client import FrappeClient, FrappeError
from oracle import build_ctx


class IntentRefused(Exception):
    """The caller asked for something the intent will not do."""


class AuditWriteFailed(RuntimeError):
    """The document was written but its override could not be recorded.

    Raised only after the engine has tried to undo the document, so the message
    also says whether that compensation succeeded. If it did not, the state
    named in the message needs a human.
    """


@dataclass
class Override:
    """One declared override, and what became of it.

    `requested_value` is what the caller asked for. `stored_value` is what the
    saved document turned out to hold, and it is filled in only after the
    document exists and has been read back — because ERPNext runs its own
    machinery (Pricing Rules, tax and currency rules, hooks) during validate,
    after this engine has set the field. See harness/audit.py for the incident
    that made this a three-value record instead of a two-value one.
    """
    fieldname: str
    derived_value: Any
    requested_value: Any
    reason: str
    row_idx: int = 0

    # Filled after the audit write. None until then.
    stored_value: Any = None
    drift: str | None = None
    record: str | None = None       # the Intent Override Log name

    @property
    def supplied_value(self) -> Any:
        """Compatibility alias for the pre-drift name. `requested_value` is the
        accurate one: what the caller supplied is not necessarily what the
        document stored."""
        return self.requested_value


@dataclass
class IntentResult:
    intent: str
    doctype: str
    name: str | None = None
    docstatus: int | None = None
    grand_total: float | None = None
    overrides: list[Override] = field(default_factory=list)
    # Names of the Intent Override Log records written for this document, so a
    # caller can cite them without re-querying. Empty when nothing was overridden.
    override_records: list[str] = field(default_factory=list)
    derived: dict = field(default_factory=dict)
    invariants_checked: list[str] = field(default_factory=list)
    invariant_failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = not self.invariant_failures
        return d


def _default_actor() -> str:
    """Name the calling program, and do not pretend to know more than that.

    The audit record distinguishes `actor` (what the caller says it is, which
    it can lie about) from `actor_user` (the authenticated session, which it
    cannot). With no identity passed in, the truthful `actor` is the script
    that ran, not an invented service name.
    """
    prog = os.path.basename(sys.argv[0] or "")
    return f"harness:{prog}" if prog else "intent-engine:unidentified"


class IntentEngine:
    def __init__(self, client: FrappeClient, catalog_path: str = "intents/catalog.yaml",
                 actor: str | None = None, actor_kind: str = "Agent"):
        self.client = client
        self.catalog = yaml.safe_load(open(catalog_path))
        self.intents = {i["id"]: i for i in self.catalog["intents"]}
        self.defaults = self.catalog.get("defaults", {})
        self.actor = actor or _default_actor()
        self.actor_kind = actor_kind
        self._audit_ready = False

    # ---- the override record -------------------------------------------------

    def _ensure_audit(self) -> None:
        """Create the Intent Override Log doctype if this site has never seen
        it. Lazy on purpose: an engine that never overrides anything should not
        need write access to DocType."""
        if not self._audit_ready:
            audit.ensure_doctype(self.client, verbose=False)
            self._audit_ready = True

    def _record_overrides(self, res: IntentResult) -> None:
        """Write the override records, and refuse to leave a document behind
        that has an unrecorded override on it.

        Why the document is undone rather than kept:

        The tempting alternative is to log a warning and return the invoice —
        the books balance either way, and the caller got what it asked for. But
        the entire premise of this layer is that an off-list price is allowed
        *because* it is recorded. An override that is not recorded is the exact
        defect Phase 2 documented, arrived at by a different route: a document
        priced away from the list with nothing anywhere saying who decided that
        or why. Returning it successfully would mean the harness itself
        produces the artefact it exists to prevent.

        So on an audit failure the document is cancelled (or deleted, if it
        never got past draft) and the caller gets an exception. A cancelled
        Sales Invoice reverses its own GL entries, so the books are left where
        they started. The compensation can itself fail — the network is down,
        the document is already linked — and when it does, the exception says
        so in as many words, because that is a state that needs a person.
        """
        if not res.overrides:
            return
        self._ensure_audit()
        try:
            records = audit.record_result(
                self.client, res, actor=self.actor, actor_kind=self.actor_kind)
        except Exception as e:                      # noqa: BLE001 — re-raised below
            raise AuditWriteFailed(
                f"{res.doctype} {res.name} has {len(res.overrides)} unrecorded "
                f"override(s) and was rolled back.\n"
                f"  audit write failed: {type(e).__name__}: {str(e)[:300]}\n"
                f"  compensation: {self._unwind(res)}"
            ) from e

        # Carry the truth back to the caller: what the document actually stored,
        # whether it drifted from the request, and the record that says so.
        for o, rec in zip(res.overrides, records):
            o.record = rec["name"]
            o.drift = rec["drift"]
            if rec["drift"] != "unverified":
                o.stored_value = (rec["stored_num"] if rec.get("is_numeric")
                                  else rec["stored_value"])
        res.override_records = [r["name"] for r in records]

    def _unwind(self, res: IntentResult) -> str:
        """Undo the document an override could not be recorded against.
        Returns a description of what happened, for the exception message."""
        if not res.name:
            return "nothing to undo; the document was never saved"
        try:
            if res.docstatus == 1:
                self.client.call("frappe.client.cancel",
                                 doctype=res.doctype, name=res.name)
                return (f"{res.name} cancelled (docstatus 2); its GL entries are "
                        f"reversed and the books are where they started")
            self.client.call("frappe.client.delete",
                             doctype=res.doctype, name=res.name)
            return f"{res.name} deleted; it never left draft"
        except FrappeError as ce:
            return (f"COMPENSATION FAILED — {res.doctype} {res.name} is still "
                    f"live with an unrecorded override. This needs a human. "
                    f"({str(ce)[:200]})")

    def _cite_record(self, res: IntentResult) -> None:
        """Leave a breadcrumb on the document pointing at its override records.

        Deliberately a Comment and not `remarks`. `remarks` is a business field
        that belongs to whoever wrote it — the previous version of this engine
        overwrote it unconditionally, destroying caller-supplied text to make
        room for a log line it had no right to put there. A Comment is the
        field Frappe provides for exactly this and it adds rather than replaces.

        Best-effort, and non-fatal by design: the authoritative record already
        exists and is immutable. Losing a convenience pointer is not worth
        cancelling a correct invoice over, which is the opposite of the call
        made in _record_overrides — and the difference is that this one is a
        duplicate of information already safely stored.
        """
        if not res.override_records:
            return
        try:
            self.client.insert({
                "doctype": "Comment", "comment_type": "Info",
                "reference_doctype": res.doctype, "reference_name": res.name,
                "content": ("Override recorded in Intent Override Log: "
                            + ", ".join(res.override_records)),
            })
        except FrappeError:
            pass

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
            verify = self.client.get_doc(saved["doctype"], saved["name"])
            if int(verify.get("docstatus") or 0) != 1:
                res.invariant_failures.append(
                    f"submit did not take: {saved['name']} is at docstatus "
                    f"{verify.get('docstatus')}")
            saved = verify
            res.invariants_checked.append("submit actually posted (re-read)")
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

        # 1a. Refused HEADER fields. conversion_rate and friends live on the
        # parent, not on item rows, so checking rows alone missed the single
        # highest-impact field in the whole contract.
        for fname in header:
            if fname in refused:
                r = refused[fname]
                raise IntentRefused(
                    f"{intent_id}: header may not supply {fname!r}.\n"
                    f"  reason:   {r['reason'].strip()}\n"
                    f"  evidence: {r.get('evidence', 'n/a')}"
                )

        # 1b. A refused field in a line is an error, never a silent accept.
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
            # `uom` is a legitimate caller input: selling in Boxes rather than
            # Nos is a business fact, not a derived one. It has to reach the ctx
            # or get_item_details cannot compute the conversion factor, and a
            # uom != stock_uom case becomes unreachable through this path -- as
            # it was until corpus scenario srv-01 exposed it. The FACTOR stays
            # refused; only the unit the caller is trading in comes from them.
            probe = {"item_code": line["item_code"], "qty": line.get("qty", 1)}
            if line.get("uom"):
                probe["uom"] = line["uom"]
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
                    Override(fieldname=fname, derived_value=derived_value,
                             requested_value=o["value"], reason=o["reason"].strip(),
                             row_idx=idx)
                )
                row[fname] = o["value"]
            rows.append(row)

        # A named tax template must actually be applied. ERPNext will accept a
        # document that names an 18% template and carries no tax rows, or a
        # forged 0% row, and post no tax at all (corpus tax-01, tax-02). Naming
        # a template is a caller decision; the RATES that template implies are
        # not. So derive them.
        if parent.get("taxes_and_charges") and not parent.get("taxes"):
            master_dt = ("Sales Taxes and Charges Template"
                         if spec["maps_to"]["doctype"] in
                            ("Sales Invoice", "Sales Order", "Delivery Note", "Quotation")
                         else "Purchase Taxes and Charges Template")
            derived_taxes = self.client.call(
                # v16.34.1 path. It moved to erpnext.accounts.services.taxes in
                # v17-dev; we target the version this repo actually runs against.
                # (Read the tag, not the working tree. Getting that wrong is how
                # this call 417'd the first time.)
                "erpnext.controllers.accounts_controller.get_taxes_and_charges",
                master_doctype=master_dt, master_name=parent["taxes_and_charges"]) or []
            if not derived_taxes:
                raise IntentRefused(
                    f"{intent_id}: tax template {parent['taxes_and_charges']!r} resolved to no "
                    f"rows. Refusing to post a document that claims a tax treatment it does "
                    f"not carry.")
            parent["taxes"] = derived_taxes

        doc = dict(parent)
        doc["items"] = rows
        # `remarks` is left exactly as the caller set it. The override is
        # recorded below, after the document exists, in a doctype built for it.

        saved = self.client.insert(doc)
        if submit and spec["maps_to"].get("submittable"):
            saved = self.client.submit(saved)
            # Re-read from the server rather than trusting the submit response.
            # The enforced endpoint taught this the hard way: doc.submit() there
            # was a silent no-op because submit() reads ignore_permissions off
            # the document flags rather than taking it as a parameter, so the
            # call neither submitted nor raised, and the harness reported
            # success while writing drafts for days.
            #
            # The general rule, and it is cheap: any check that asserts success
            # must re-read the state it claims to have changed.
            verify = self.client.get_doc(saved["doctype"], saved["name"])
            if int(verify.get("docstatus") or 0) != 1:
                res.invariant_failures.append(
                    f"submit did not take: {saved['name']} is at docstatus "
                    f"{verify.get('docstatus')} after submit() returned successfully")
            saved = verify
            res.invariants_checked.append("submit actually posted (re-read)")
        res.name = saved.get("name")
        res.docstatus = saved.get("docstatus")
        res.grand_total = saved.get("grand_total")

        # After the document is saved and submitted, never before: the record
        # is built by reading the persisted document back, and a read taken
        # before submit would miss whatever the submit path changed.
        self._record_overrides(res)
        self._cite_record(res)

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
