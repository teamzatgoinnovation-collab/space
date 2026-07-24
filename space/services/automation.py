"""Automation rules — trial reminders, suspend, backup retry, etc."""

from __future__ import annotations

import frappe
from frappe.utils import add_days, getdate, today

from space.services import notifications


def run_daily_automation():
	for name in frappe.get_all("Space Automation Rule", filters={"is_active": 1}, pluck="name"):
		rule = frappe.get_doc("Space Automation Rule", name)
		try:
			_run_rule(rule)
		except Exception:
			frappe.log_error(title=f"Automation rule failed: {name}")


def _run_rule(rule):
	actions = [a.strip() for a in (rule.actions or "").split(",") if a.strip()]
	trigger = rule.trigger_event

	if trigger == "Trial Ending":
		days = int(rule.days_before or 3)
		target = add_days(today(), days)
		for sub in frappe.get_all(
			"Space Subscription",
			filters={"status": "Trial", "trial_ends_on": target},
			fields=["name", "customer"],
		):
			_apply(actions, customer=sub.customer, ref=("Space Subscription", sub.name), context="trial_ending")

	elif trigger == "Trial Ended":
		for sub in frappe.get_all(
			"Space Subscription",
			filters={"status": "Expired", "payment_status": "Expired", "trial_ends_on": ("<", today())},
			fields=["name", "customer"],
		):
			_apply(actions, customer=sub.customer, ref=("Space Subscription", sub.name), context="trial_ended")

	elif trigger == "Subscription Expired":
		for sub in frappe.get_all(
			"Space Subscription",
			filters={"status": "Expired"},
			fields=["name", "customer"],
		):
			_apply(actions, customer=sub.customer, ref=("Space Subscription", sub.name), context="sub_expired")

	elif trigger == "Backup Failed":
		if not frappe.db.exists("DocType", "Space Backup"):
			return
		for b in frappe.get_all(
			"Space Backup",
			filters={"status": "Failed", "modified": (">", add_days(today(), -1))},
			fields=["name", "site"],
		):
			if not b.site or not frappe.db.exists("Space Site", b.site):
				continue
			site = frappe.get_doc("Space Site", b.site)
			_apply(actions, customer=site.customer, ref=("Space Backup", b.name), context="backup_failed", site=site.name)

	elif trigger == "Deployment Failed":
		if not frappe.db.exists("DocType", "Space Deployment Job"):
			return
		for j in frappe.get_all(
			"Space Deployment Job",
			filters={"status": "Failed", "modified": (">", add_days(today(), -1))},
			fields=["name", "site"],
		):
			if not j.site or not frappe.db.exists("Space Site", j.site):
				continue
			site = frappe.get_doc("Space Site", j.site)
			_apply(actions, customer=site.customer, ref=("Space Deployment Job", j.name), context="deploy_failed")


def _apply(actions, *, customer=None, ref=None, context="", site=None):
	for action in actions:
		if action == "notify_customer" and customer:
			notifications.notify(
				title=f"Automation: {context}",
				event_type="subscription_expired" if "trial" in context or "sub" in context else "generic",
				message=f"Rule action for {context}",
				customer=customer,
				ref_doctype=ref[0] if ref else None,
				ref_name=ref[1] if ref else None,
			)
		elif action == "notify_admin":
			notifications.notify(
				title=f"Admin alert: {context}",
				event_type="generic",
				message=f"{context} {ref}",
			)
		elif action == "send_reminder" and customer:
			notifications.notify(
				title="Reminder",
				event_type="subscription_expired",
				message="Your trial or subscription needs attention",
				customer=customer,
			)
		elif action == "suspend_site" and site:
			from space.jobs.lifecycle import enqueue_suspend_site

			try:
				enqueue_suspend_site(site)
			except Exception:
				pass
		elif action == "retry_backup" and site:
			from space.jobs.backup import enqueue_backup

			try:
				enqueue_backup(site, backup_type="Automatic")
			except Exception:
				pass
