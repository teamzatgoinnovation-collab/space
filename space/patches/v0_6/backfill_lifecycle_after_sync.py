"""Give every existing customer subscription a lifecycle state (a fresh 30-day trial that starts at rollout).

Runs AFTER the schema sync ([post_model_sync] in patches.txt): the lifecycle columns do not exist before it, and a
patch that runs earlier would find nothing to do and still be recorded as done. The logic lives in
space.services.lifecycle.backfill_missing (idempotent); nothing turns read-only and nothing is deleted at once,
because enforcement and automatic deletion stay off in Space Settings until an admin ticks them.
"""

import frappe

from space.services import lifecycle


def execute():
	counts = lifecycle.backfill_missing()
	frappe.db.commit()
	print(f"Space lifecycle backfill: {counts}")
