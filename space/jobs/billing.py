"""Billing / subscription expiry / monthly invoice stubs (gateway disabled)."""

from __future__ import annotations

import frappe
from frappe.utils import add_days, getdate, today

from space.services import notifications
from space.utils.activity import log_activity


def process_subscription_expiry():
	"""Daily: mark expired trials/subscriptions; apply grace; notify."""
	for sub_name in frappe.get_all(
		"Space Subscription",
		filters={"status": ("in", ["Active", "Trial"])},
		pluck="name",
	):
		sub = frappe.get_doc("Space Subscription", sub_name)
		changed = False
		end = getdate(sub.end_date) if sub.end_date else None
		trial_end = getdate(sub.trial_ends_on) if sub.trial_ends_on else None
		grace = int(sub.grace_period_days or 0)

		if sub.status == "Trial" and trial_end and trial_end < getdate(today()):
			sub.status = "Expired"
			sub.payment_status = "Expired"
			changed = True
			notifications.notify(
				title=f"Trial expired: {sub.name}",
				event_type="subscription_expired",
				message=f"Trial ended for subscription {sub.name}",
				customer=sub.customer,
				ref_doctype="Space Subscription",
				ref_name=sub.name,
			)

		if end and end < getdate(today()):
			grace_end = add_days(end, grace)
			if getdate(today()) <= grace_end:
				if sub.payment_status != "Unpaid":
					sub.payment_status = "Unpaid"
					changed = True
			else:
				sub.status = "Expired"
				sub.payment_status = "Expired"
				changed = True
				notifications.notify(
					title=f"Subscription expired: {sub.name}",
					event_type="subscription_expired",
					message=f"Subscription {sub.name} expired after grace period",
					customer=sub.customer,
					ref_doctype="Space Subscription",
					ref_name=sub.name,
				)

		if not sub.renewal_date and end:
			sub.renewal_date = end
			changed = True

		if changed:
			sub.save(ignore_permissions=True)
			frappe.db.commit()
			log_activity("Subscription expiry processed", "Space Subscription", sub.name)


def generate_monthly_invoices():
	"""Monthly stub invoices for Active/Paid/Free subscriptions (status-only billing)."""
	period_start = getdate(today()).replace(day=1)
	period_end = add_days(add_days(period_start, 32).replace(day=1), -1)
	for sub_name in frappe.get_all(
		"Space Subscription",
		filters={"status": ("in", ["Active", "Trial"])},
		pluck="name",
	):
		sub = frappe.get_doc("Space Subscription", sub_name)
		exists = frappe.db.exists(
			"Space Invoice",
			{"subscription": sub.name, "period_start": period_start, "period_end": period_end},
		)
		if exists:
			continue
		plan = frappe.get_doc("Space Plan", sub.plan)
		amount = float(plan.monthly_price or 0)
		pay_status = sub.payment_status or "Free"
		if amount <= 0:
			pay_status = "Free"
			inv_status = "Paid"
		else:
			inv_status = "Unpaid" if pay_status not in ("Paid", "Free", "Trial") else "Paid"
			if pay_status == "Trial":
				amount = 0
				inv_status = "Paid"

		inv = frappe.get_doc(
			{
				"doctype": "Space Invoice",
				"customer": sub.customer,
				"subscription": sub.name,
				"status": inv_status,
				"period_start": period_start,
				"period_end": period_end,
				"due_date": add_days(period_start, 14),
				"amount": amount,
				"currency": "USD",
				"payment_status": pay_status if pay_status in ("Free", "Trial", "Unpaid", "Paid", "Expired", "Suspended") else "Unpaid",
				"notes": "Phase 2 status-only invoice (payment gateway disabled)",
			}
		).insert(ignore_permissions=True)

		frappe.get_doc(
			{
				"doctype": "Space Payment History",
				"customer": sub.customer,
				"invoice": inv.name,
				"subscription": sub.name,
				"amount": amount,
				"currency": "USD",
				"status": "Free" if amount <= 0 else "Pending",
				"method": "None",
				"notes": "Gateway disabled",
			}
		).insert(ignore_permissions=True)

		sub.last_invoice = inv.name
		sub.save(ignore_permissions=True)
		frappe.db.commit()


def record_weekly_usage():
	"""Weekly usage rollup from site counters (requires space_cloud Space Site)."""
	if not frappe.db.exists("DocType", "Space Site"):
		return
	period_end = getdate(today())
	period_start = add_days(period_end, -7)
	for site_name in frappe.get_all(
		"Space Site",
		filters={"status": ("in", ["Active", "Suspended"])},
		pluck="name",
	):
		site = frappe.get_doc("Space Site", site_name)
		frappe.get_doc(
			{
				"doctype": "Space Usage",
				"site": site.name,
				"customer": site.customer,
				"period_start": period_start,
				"period_end": period_end,
				"storage_mb": site.storage_used_mb or 0,
				"database_mb": site.database_size_mb or 0,
			}
		).insert(ignore_permissions=True)
	frappe.db.commit()
