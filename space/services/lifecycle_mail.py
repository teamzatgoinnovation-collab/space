"""Customer emails of the subscription lifecycle: sent at most once per (subscription, kind, cycle), retried on failure.

The Space Lifecycle Event row is both the dedupe record (unique `dedupe_key`) and the retry queue: the email's
content is stored on it, so `retry_failed()` can send it again without recomputing anything. A row only becomes
"Sent" after Frappe accepted the mail for sending; an inactivity deletion needs that "Sent" -- no warning, no deletion.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import frappe

from space.services import lifecycle as lc

MAX_RETRIES = 5
SPACE_URL = "https://space.zatgoinnovation.com"


def _esc(text: Any) -> str:
	return frappe.utils.escape_html(str(text if text is not None else ""))


def build_html(heading: str, body: str, rows: list[tuple[str, str]], advice: str = "") -> str:
	table = "".join(
		f'<tr><td style="padding:6px 12px 6px 0;color:#6b7690">{_esc(k)}</td><td style="padding:6px 0"><b>{_esc(v)}</b></td></tr>'
		for k, v in rows
	)
	advice_html = (
		f'<p style="margin:16px 0;padding:12px 14px;background:#fff7e6;border-radius:8px;line-height:1.5">{_esc(advice)}</p>'
		if advice
		else ""
	)
	return (
		'<div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto;color:#1b2536">'
		f'<h2 style="margin:0 0 12px">{_esc(heading)}</h2><p style="line-height:1.55;margin:0 0 12px">{_esc(body)}</p>'
		f'<table style="border-collapse:collapse;font-size:14px">{table}</table>{advice_html}'
		f'<p style="margin:20px 0"><a href="{SPACE_URL}" style="background:#2f5bea;color:#fff;padding:10px 18px;'
		'border-radius:8px;text-decoration:none">Open ZatGo Space</a></p>'
		'<p style="font-size:12px;color:#6b7690">ZatGo Innovation. This is an automated message.</p></div>'
	)


def _deliver(event_name: str, now=None) -> str:
	"""Send the email stored on an event row. Returns Sent / Failed / Skipped and updates the row."""
	ev = frappe.get_doc("Space Lifecycle Event", event_name)
	payload = json.loads(ev.details or "{}")
	if not frappe.get_single("Space Settings").email_notifications_enabled:
		ev.status = "Skipped"
		payload["skipped_reason"] = "Space Settings > Email notifications is off"
	else:
		try:
			frappe.sendmail(recipients=[ev.recipient], subject=payload["subject"], message=payload["html"], delayed=True)
			ev.status = "Sent"
		except Exception as exc:
			ev.retry_count = int(ev.retry_count or 0) + 1
			ev.status = "Failed"
			ev.next_retry_at = (now or lc.utcnow()) + timedelta(minutes=min(60 * ev.retry_count, 360))
			payload["error"] = str(exc)[:300]
			frappe.log_error(title=f"Space lifecycle email failed: {ev.kind}")
	ev.details = json.dumps(payload, default=str)
	ev.save(ignore_permissions=True)
	return ev.status


def send_once(
	*, kind: str, subscription: str, site: str | None, dedupe_key: str, recipient: str | None,
	subject: str, heading: str, body: str, rows: list[tuple[str, str]], advice: str = "", now=None,
) -> str:
	"""Queue + send one customer email. 'duplicate' when this key was already used, 'no_recipient' without an address."""
	if not recipient:
		return "no_recipient"
	payload = {"subject": subject, "html": build_html(heading, body, rows, advice)}
	name = lc.record_event(
		kind, subscription=subscription, site=site, dedupe_key=dedupe_key, status="Pending",
		recipient=recipient, details=payload,
	)
	if name is None:
		return "duplicate"
	return _deliver(name, now)


def retry_failed(now=None) -> int:
	"""Hourly: send again every Failed email that is due and has retries left. Returns how many were tried."""
	now = now or lc.utcnow()
	rows = frappe.get_all(
		"Space Lifecycle Event",
		filters={"status": "Failed", "retry_count": ("<", MAX_RETRIES), "next_retry_at": ("<=", now)},
		pluck="name", limit_page_length=50,
	)
	for name in rows:
		frappe.db.get_value("Space Lifecycle Event", name, "modified", for_update=True)
		if frappe.db.get_value("Space Lifecycle Event", name, "status") != "Failed":
			continue  # another worker got it
		_deliver(name, now)
	return len(rows)


def warning_sent(subscription: str, cycle: str) -> bool:
	return bool(frappe.db.get_value("Space Lifecycle Event", {"dedupe_key": f"inactivity_warning:{subscription}:{cycle}", "status": "Sent"}))
