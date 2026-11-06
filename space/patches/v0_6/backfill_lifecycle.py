"""Give every existing customer subscription a lifecycle state: a fresh 30-day trial that starts at rollout.

Nothing turns read-only and nothing is deleted at once: enforcement and automatic deletion are both off in
Space Settings until an admin ticks them. Idempotent -- a subscription that already has a state is left alone.
The portal's own site and protected domains are skipped.
"""

from datetime import timedelta

import frappe

from space.services import lifecycle as lc


def execute():
	if not frappe.db.has_column("Space Subscription", "lifecycle_status"):
		return
	protected = lc.protected_domains()
	started = lc.utcnow()
	for sub in frappe.get_all(
		"Space Subscription", filters={"lifecycle_status": ("in", ("", None))}, fields=["name", "plan", "payment_status"]
	):
		site = lc.site_of(sub.name)
		row = frappe.db.get_value("Space Site", site, ["domain", "status"], as_dict=True) if site else None
		if not row or row.status in ("Deleted", "Failed", "Draft") or (row.domain or "").lower() in protected:
			continue
		days = lc.settings()["trial_days_default"]
		price = float(frappe.db.get_value("Space Plan", sub.plan, "monthly_price") or 0) if sub.plan else 0
		state = lc.ACTIVE if price == 0 else lc.TRIAL
		fields = {"lifecycle_status": state, "trial_started_at": started}
		if state == lc.TRIAL:
			fields["trial_ends_at"] = started + timedelta(days=days)
		frappe.db.set_value("Space Subscription", sub.name, fields)
		lc.record_event(
			"trial_started", subscription=sub.name, site=site, dedupe_key=f"backfill:{sub.name}",
			details={"state": state, "note": "rollout backfill"},
		)
	frappe.db.commit()
