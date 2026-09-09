# Letting software write to your ledger

## The problem, in one example

An automated system creates a sales invoice for 1,000 US dollars. Your ERP holds
the correct exchange rate and will use it — but only if the system leaves that
field blank. If the system fills it in with 1.0, the ERP accepts that number
without comment. At a rate of 90, the invoice posts to your ledger at **1,000 in
local currency instead of 90,000**.

The multiplier is whatever the rate happens to be that day; the error is the
whole of it.

Your books balance perfectly. Debits equal credits. Every reconciliation passes.
Nothing in the record distinguishes this from a correct entry, and no report will
ever flag it.

This is not a defect report against the software. Accepting a value the caller
supplied is intended behaviour, and for a human typing at a screen it is
reasonable. It stops being reasonable when the caller is a machine that never
looked the rate up, because a machine's missing context becomes a record that
looks exactly like a human decision.

**That is the whole issue: the system cannot tell you whether anybody decided.**

## What we proved

Working against ERPNext version 16.34.1, every result reproducible from a clean
installation:

- **Eight fields** where the system holds a correct value and takes the caller's
  instead without warning. The exchange rate is the most severe. Others include
  the price a customer is charged, the account revenue posts to, and a unit
  conversion that moves warehouse quantities rather than only money.
- **One of these cannot be typed by a human, under the default configuration.**
  The reference price field is read-only on screen unless a setting is turned on
  that makes it editable. Left at its default, only an automated caller can set
  it, and when it does, the discount recorded against it is measured from a
  baseline that never existed. One checkbox makes that no longer true, which is
  why it is stated with the qualifier rather than as a flat claim.
- **A working control.** An automated identity is denied permission to write
  invoices directly and can only act through a vetted operation that computes
  every derivable value itself, refuses ten fields outright, and permits a
  deliberate override only with a recorded reason. **14 of 14 checks pass**,
  including one that forces the audit record to fail and confirms the invoice is
  rolled back rather than posted unrecorded.
- **Retries do not double-post.** Six simultaneous identical requests produce one
  invoice.

## What we did not prove, and you should ask about

- **Only one business operation is protected.** Invoicing. The other ten
  (quotations, orders, deliveries, receipts, payments) are covered by a library
  that a caller can simply choose not to use. **A company runs on all eleven.**
- **We do not know the throughput.** <!-- THROUGHPUT: pending item 2 --> The
  current design serialises on a shared audit sequence, and an early test landed
  only 3 of 20 simultaneous writes. That is being measured properly on dedicated
  hardware; **until it is, treat this as unproven at production volume.**
- **This is one vendor.** We checked Odoo's source: it does not have the exchange
  rate defect, because its defaults are the opposite way round. **The severe
  finding is specific to this ERP family.** SAP and NetSuite are undetermined.
- **One company, one currency.** Subsidiaries and intercompany transactions are
  untested.
- **Every human role still bypasses the control.** Only the identity we
  deliberately constrained is bound by it.

## What it would cost to run for real

- **Engineering.** The control is currently a script stored in the ERP's own
  database. That is adequate to prove a point and not something an auditor should
  accept. Running it properly means a packaged application, versioned and
  deployed like any other code. Extending it to all eleven operations is the
  larger part of the work.
- **Throughput.** Unknown, and possibly the deciding constraint. See above.
- **Usability.** Enforcing this on everyone means people can no longer create
  these documents by hand in the standard interface. Something will break; we
  have not measured what.
- **Governance.** The control records that a reason was given. It cannot tell you
  the reason is true. The replay in this repository generated all of its
  justifications from a template, which is the honest demonstration of that
  limit.

## What we would tell your auditor

Balanced books are not evidence of correct books. The system refuses to post an
unbalanced entry, so balance is guaranteed and carries no information. Any
assurance process whose ledger test is "do debits equal credits" will pass a
company whose automated entries are systematically wrong.

The distinction between a decision and an omission exists only at the moment of
writing, and only if something captures it. **That is what this control does, for
one operation out of eleven, at a throughput we have not yet measured.**

## What we recommend

**Not yet, as this stands.** Do not let automated systems write every transaction
through this control today: ten of the eleven operations a company runs on are
not enforced, and we cannot yet tell you what it does to throughput.

**Before you would, three things have to be true, in this order.** The throughput
number has to exist and be acceptable at your volume. The control has to be a
packaged, versioned application rather than a script held in the ERP's database.
And all eleven operations have to be enforced, not one.

**What you can adopt this week, independent of any of that:** reconcile every
foreign-currency document against the published rate for its posting date. It is
a report, not a project, it needs nothing from this repository, and on the
evidence here it is the single highest-value check available to you.

---

*Supporting detail: [CLAIMS.md](CLAIMS.md) grades every claim and lists what we
retracted. [LIMITATIONS.md](LIMITATIONS.md) and [THREAT_MODEL.md](THREAT_MODEL.md)
state what this does not stop. [REPRODUCE.md](REPRODUCE.md) reproduces every
number from a clean clone.*
