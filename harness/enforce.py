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


def ensure(client: FrappeClient) -> dict:
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

    script = open(SCRIPT_PATH).read()
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
