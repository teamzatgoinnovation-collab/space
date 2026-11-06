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
	"account_created": "Account Created",
	"site_resumed": "Site Resumed",
	"plan_changed": "Plan Changed",
}

# What the customer is told for each event. Internal details (tracebacks,
# bench errors) go to the admin copy only.
_CUSTOMER_COPY = {
	"Account Created": ("Welcome to ZatGo Space", "Your account is ready. You can now sign in and create your ERPNext site."),
	"Site Created": ("Your site is ready", "Your ERPNext site has been created and is ready to use."),
	"Site Suspended": ("Your site was suspended", "Your site is currently suspended. Contact us if you did not expect this."),
	"Site Resumed": ("Your site is active again", "Your site has been resumed and is available again."),
	"Site Deleted": ("Your site was deleted", "Your site has been permanently deleted."),
	"Plan Changed": ("Your plan was changed", "The plan for your site has been updated."),
	"Deployment Failed": ("We could not finish your request", "Something went wrong while processing your request. Our team has been notified and will follow up."),
}

SPACE_URL = "https://space.zatgoinnovation.com"


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
		"Account Created",
		"Site Resumed",
		"Plan Changed",
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

	try:
		doc.email_sent = int(_send_emails(event, title, message, customer))
		if doc.email_sent:
			doc.save(ignore_permissions=True)
	except Exception:
		frappe.log_error(title="Space notification email failed")

	frappe.db.commit()
	return doc.name


def _html(heading: str, body: str, detail: str = "") -> str:
	esc = frappe.utils.escape_html
	detail_html = (
		f'<p style="margin:16px 0;padding:12px 14px;background:#f3f6fb;border-radius:8px;'
		f'font-family:monospace;font-size:13px;word-break:break-all">{esc(detail)}</p>'
		if detail
		else ""
	)
	return (
		'<div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto;color:#1b2536">'
		f'<h2 style="margin:0 0 12px">{esc(heading)}</h2>'
		f'<p style="line-height:1.55;margin:0">{esc(body)}</p>{detail_html}'
		f'<p style="margin:20px 0"><a href="{SPACE_URL}" style="background:#2f5bea;color:#fff;'
		'padding:10px 18px;border-radius:8px;text-decoration:none">Open ZatGo Space</a></p>'
		'<p style="font-size:12px;color:#6b7690">ZatGo Innovation. This is an automated message.</p></div>'
	)


def _send_emails(event: str, title: str, message: str, customer: str | None) -> bool:
	"""Customer gets a plain-language mail; the admin address gets the full detail.
	Does nothing unless "Email notifications" is on in Space Settings. Sending
	goes through the site's default outgoing Email Account (e.g. Gmail SMTP)."""
	settings = frappe.get_single("Space Settings")
	if not settings.email_notifications_enabled:
		return False

	sent = False
	customer_email = frappe.db.get_value("Space Customer", customer, "email") if customer else None
	admin_email = (settings.notification_email or "").strip()

	if customer_email and event in _CUSTOMER_COPY:
		heading, body = _CUSTOMER_COPY[event]
		detail = message if event in ("Site Created", "Site Deleted", "Site Suspended", "Site Resumed", "Plan Changed") else ""
		frappe.sendmail(recipients=[customer_email], subject=f"[ZatGo Space] {heading}", message=_html(heading, body, detail), delayed=True)
		sent = True

	if admin_email and admin_email != customer_email:
		who = f" (customer: {customer_email})" if customer_email else ""
		frappe.sendmail(recipients=[admin_email], subject=f"[Space] {title}", message=_html(title + who, message or title), delayed=True)
		sent = True
	return sent
