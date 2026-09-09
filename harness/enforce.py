"""Provision the enforcement boundary. Idempotent.

The intent layer in harness/intent.py is a *client library*. A caller can
ignore it and POST to /api/resource directly, so on its own it is advice, not a
control. This module makes it a control:

  1. A role with NO write permission on any transaction doctype, and read-only
     access to the master data an agent legitimately needs (it must be able to
     read the price list; it must not be able to write invoices).
  2. A user bound to that role. Direct POST /api/resource/Sales Invoice -> 403.
  3. A server-side `bill_intent` endpoint that runs with elevated permission and
     enforces derive-or-refuse. This is the only way in.

Requires `server_script_enabled: true` in the site config; `ensure()` sets it.
"""
from __future__ import annotations

import os
from client import FrappeClient, FrappeError

ROLE = "Agent Writer"
AGENT_USER = "agent@headless.test"
AGENT_PASSWORD = "agentpass123"

# Read-only masters an agent needs to work. Deliberately no transaction doctype.
READABLE_MASTERS = [
    "Item", "Item Price", "Customer", "Company", "Price List", "Account",
    "Cost Center", "Warehouse", "Currency", "Fiscal Year", "UOM", "Item Group",
    "Customer Group", "Territory", "Sales Taxes and Charges Template",
    "Item Tax Template", "Payment Terms Template", "Address", "Contact",
    "Mode of Payment", "Accounting Dimension", "Finance Book", "Item Default",
    "Party Account",
]

SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "server_scripts", "bill_intent.py")


IDEMPOTENCY_FIELD = "intent_idempotency_key"


def ensure_idempotency_field(client: FrappeClient) -> None:
    """Create the Custom Field the idempotency guarantee rests on.

    This was made by hand during development and provisioned by nothing, so a
    fresh clone had no field and no unique index: the endpoint would fail on an
    unknown fieldname and there would be nothing for the database to settle a
    race with. The proof passed only on the machine that had been hand-edited,
    which is the exact failure the fresh-clone rule exists to catch.

    `unique=1` is the load-bearing part. The endpoint's check-then-insert has a
    window; the index is what closes it.
    """
    name = f"Sales Invoice-{IDEMPOTENCY_FIELD}"
    if client.exists("Custom Field", name):
        f = client.get_doc("Custom Field", name)
        if not f.get("unique"):
            client.call("frappe.client.set_value", doctype="Custom Field",
                        name=name, fieldname="unique", value=1)
            print("  idemp  : field existed without unique=1, corrected")
        else:
            print("  idemp  : Custom Field exists with unique=1")
        return
    client.insert({
        "doctype": "Custom Field", "dt": "Sales Invoice",
        "fieldname": IDEMPOTENCY_FIELD, "label": "Intent Idempotency Key",
        "fieldtype": "Data", "unique": 1, "read_only": 1, "no_copy": 1,
        "insert_after": "remarks",
    })
    print(f"  idemp  : created Custom Field {IDEMPOTENCY_FIELD} (unique=1)")


def ensure(client: FrappeClient) -> dict:
    ensure_idempotency_field(client)

    if not client.exists("Role", ROLE):
        client.insert({"doctype": "Role", "role_name": ROLE, "desk_access": 0})
        print(f"  role   : created {ROLE}")
    else:
        print(f"  role   : {ROLE} exists")

    granted = 0
    for dt in READABLE_MASTERS:
        try:
            client.insert({"doctype": "Custom DocPerm", "parent": dt,
                           "parenttype": "DocType", "parentfield": "permissions",
                           "role": ROLE, "permlevel": 0, "read": 1, "write": 0,
                           "create": 0, "delete": 0, "submit": 0, "cancel": 0})
            granted += 1
        except FrappeError:
            pass  # already granted
    print(f"  perms  : read-only on {len(READABLE_MASTERS)} masters ({granted} new)")

    if not client.exists("User", AGENT_USER):
        client.insert({"doctype": "User", "email": AGENT_USER,
                       "first_name": "Constrained Agent", "send_welcome_email": 0,
                       "new_password": AGENT_PASSWORD,
                       "roles": [{"role": ROLE}]})
        print(f"  user   : created {AGENT_USER}")
    else:
        print(f"  user   : {AGENT_USER} exists")

    # Substitute the scope at provisioning time. The endpoint refuses any other
    # company, so the elevation inside it is bounded to one tenant.
    company = (client.call("frappe.client.get_list", doctype="Company",
                           fields=["name"], limit_page_length=0) or [{}])[0].get("name", "")
    # Party scope. A real deployment provisions the customers this identity is
    # allowed to transact for; here that is the fixture customer. Empty would
    # mean "any customer", which is the wrong default and is documented as such.
    parties = [p["name"] for p in (client.call(
        "frappe.client.get_list", doctype="Customer",
        filters={"customer_name": ["like", "Headless%"]},
        fields=["name"], limit_page_length=0) or [])]
    script = (open(SCRIPT_PATH).read()
              .replace("__ALLOWED_COMPANY__", company)
              .replace("__ALLOWED_PARTIES__", "|".join(parties)))
    print(f"  scope  : company {company!r}, {len(parties)} permitted customer(s)")
    if client.exists("Server Script", "bill_intent"):
        client.call("frappe.client.set_value", doctype="Server Script",
                    name="bill_intent", fieldname="script", value=script)
        print("  endpoint: bill_intent updated")
    else:
        client.insert({"doctype": "Server Script", "name": "bill_intent",
                       "script_type": "API", "api_method": "bill_intent",
                       "allow_guest": 0, "script": script})
        print("  endpoint: bill_intent created")
    return {"role": ROLE, "user": AGENT_USER, "endpoint": "bill_intent"}
