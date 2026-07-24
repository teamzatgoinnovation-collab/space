"""Desk notification helpers (email optional)."""

from __future__ import annotations

import frappe


EVENT_MAP = {
	"site_created": "Site Created",
	"site_suspended": "Site Suspended",
	"site_deleted": "Site Deleted",
	"backup_finished": "Backup Finished",
	"backup_failed": "Backup Failed",
	"subscription_expired": "Subscription Expired",
	"deployment_failed": "Deployment Failed",
	"ssl_expired": "SSL Expired",
}


def notify(
	*,
	title: str,
	event_type: str,
	message: str = "",
	customer: str | None = None,
	user: str | None = None,
	ref_doctype: str | None = None,
	ref_name: str | None = None,
):
	event = EVENT_MAP.get(event_type, event_type)
	if event not in (
		"Site Created",
		"Site Suspended",
		"Site Deleted",
		"Backup Finished",
		"Backup Failed",
		"Subscription Expired",
		"Deployment Failed",
		"SSL Expired",
		"Generic",
	):
		event = "Generic"

	doc = frappe.get_doc(
		{
			"doctype": "Space Notification",
			"title": (title or event)[:140],
			"event_type": event,
			"message": message,
			"customer": customer,
			"user": user,
			"ref_doctype": ref_doctype,
			"ref_name": ref_name,
			"is_read": 0,
			"email_sent": 0,
		}
	)
	doc.insert(ignore_permissions=True)

	# Optional email
	try:
		settings = frappe.get_single("Space Settings")
		if settings.email_notifications_enabled:
			recipients = []
			if settings.notification_email:
				recipients.append(settings.notification_email)
			if customer:
				email = frappe.db.get_value("Space Customer", customer, "email")
				if email:
					recipients.append(email)
			if recipients:
				frappe.sendmail(
					recipients=list(dict.fromkeys(recipients)),
					subject=f"[Space] {title}",
					message=message or title,
					delayed=True,
				)
				doc.email_sent = 1
				doc.save(ignore_permissions=True)
	except Exception:
		frappe.log_error(title="Space notification email failed")

	frappe.db.commit()
	return doc.name
