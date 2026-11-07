"""Customer emails of the subscription lifecycle: sent at most once per (subscription, kind, episode), retried on failure.

The Space Lifecycle Event row is both the dedupe record (unique `dedupe_key`) and the retry queue: the email's
content is stored on it, so `retry_failed()` can send it again without recomputing anything.

What "Sent" means: Frappe accepted the mail into its Email Queue (event status "Pending"), and LATER the queue row
itself says "Sent" (`confirm_delivery`, hourly). Only then does the event become "Sent". A queue row that ends in
"Error", never leaves, or is dropped (recipient unsubscribed) turns the event "Failed" and is retried. An inactivity
deletion is only ever scheduled behind an event that is truly "Sent": no confirmed warning, no deletion.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import frappe

from space.services import lifecycle as lc

MAX_RETRIES = 5
REARM_AFTER = timedelta(hours=24)  # an exhausted (failed 5 times) notice is tried again after this
QUEUED_STALE = timedelta(hours=6)  # still "Not Sent" in the queue after this = a failure
EMAIL_KINDS = ("trial_ending_email", "inactivity_warning_email")
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


def _mark_failed(ev: Any, payload: dict[str, Any], error: str, now: Any) -> None:
	ev.retry_count = int(ev.retry_count or 0) + 1
	ev.status = "Failed"
	# Backoff while retries remain; once they are used up, the notice is tried again from scratch after REARM_AFTER.
	delay = REARM_AFTER if ev.retry_count >= MAX_RETRIES else timedelta(minutes=min(60 * ev.retry_count, 360))
	ev.next_retry_at = now + delay
	payload["error"] = error[:300]


def _deliver(event_name: str, now: Any = None) -> str:
	"""Queue the email stored on an event row. Returns the event's new status: Pending (queued, not yet confirmed),
	Failed or Skipped. Never "Sent": only `confirm_delivery` may say that."""
	now = now or lc.utcnow()
	ev = frappe.get_doc("Space Lifecycle Event", event_name)
	payload = json.loads(ev.details or "{}")
	if not frappe.get_single("Space Settings").email_notifications_enabled:
		ev.status = "Skipped"
		payload["skipped_reason"] = "Space Settings > Email notifications is off"
	else:
		try:
			queued = frappe.sendmail(
				recipients=[ev.recipient], subject=payload["subject"], message=payload["html"], delayed=True,
				add_unsubscribe_link=0, reference_doctype="Space Lifecycle Event", reference_name=ev.name,
			)
		except Exception as exc:
			_mark_failed(ev, payload, str(exc), now)
			frappe.log_error(title=f"Space lifecycle email failed: {ev.kind}")
		else:
			queue_name = getattr(queued, "name", None) if queued else None
			if not queue_name:
				# Frappe returns nothing when it dropped the mail (recipient unsubscribed, invalid address).
				_mark_failed(ev, payload, "nothing was queued (recipient unsubscribed or address invalid)", now)
			else:
				ev.status = "Pending"
				ev.next_retry_at = None
				payload["email_queue"] = queue_name
				payload["queued_at"] = str(now)
				payload.pop("error", None)
	ev.details = json.dumps(payload, default=str)
	ev.save(ignore_permissions=True)
	return ev.status


def confirm_delivery(now: Any = None) -> dict[str, int]:
	"""Hourly: turn a queued ("Pending") notice into "Sent" only when the Email Queue row says it was sent; a row that
	ended in Error, vanished or sat unsent for hours becomes "Failed" (and is retried)."""
	now = now or lc.utcnow()
	counts = {"sent": 0, "failed": 0, "waiting": 0}
	for name in frappe.get_all("Space Lifecycle Event", filters={"status": "Pending", "kind": ("in", EMAIL_KINDS)}, pluck="name"):
		frappe.db.get_value("Space Lifecycle Event", name, "modified", for_update=True)
		ev = frappe.get_doc("Space Lifecycle Event", name)
		if ev.status != "Pending":
			continue
		payload = json.loads(ev.details or "{}")
		queue_name = payload.get("email_queue")
		queue = frappe.db.get_value("Email Queue", queue_name, ["status", "error"], as_dict=True) if queue_name else None
		if queue and queue.status == "Sent":
			ev.status = "Sent"
			payload["delivered_at"] = str(now)
			counts["sent"] += 1
		elif queue is None:
			_mark_failed(ev, payload, "the Email Queue entry is gone", now)
			counts["failed"] += 1
		elif queue.status in ("Error", "Partially Sent"):
			_mark_failed(ev, payload, f"email {queue.status}: {queue.error or ''}", now)
			counts["failed"] += 1
		else:
			queued_at = lc.as_utc(payload.get("queued_at"))
			if queued_at and now - queued_at > QUEUED_STALE:
				_mark_failed(ev, payload, f"still {queue.status} after {QUEUED_STALE}", now)
				counts["failed"] += 1
			else:
				counts["waiting"] += 1
				continue
		ev.details = json.dumps(payload, default=str)
		ev.save(ignore_permissions=True)
	return counts


def send_once(
	*, kind: str, subscription: str, site: str | None, dedupe_key: str, recipient: str | None,
	subject: str, heading: str, body: str, rows: list[tuple[str, str]], advice: str = "", now: Any = None,
) -> str:
	"""Queue + send one customer email per key. Returns the event status (Pending / Failed / Skipped), "duplicate"
	when this key is already in progress or done, or "no_recipient". A notice that was Skipped (email was off) or that
	failed all its retries is rebuilt and sent again (its content -- dates -- refreshed), instead of blocking forever."""
	if not recipient:
		return "no_recipient"
	now = now or lc.utcnow()
	payload = {"subject": subject, "html": build_html(heading, body, rows, advice)}
	name = lc.record_event(
		kind, subscription=subscription, site=site, dedupe_key=dedupe_key, status="Pending",
		recipient=recipient, details=payload,
	)
	if name is not None:
		return _deliver(name, now)
	ev = lc.event_by_key(dedupe_key)
	if ev is None:
		return "duplicate"
	due_again = lc.as_utc(ev.next_retry_at)
	exhausted = ev.status == "Failed" and int(ev.retry_count or 0) >= MAX_RETRIES and (due_again is None or now >= due_again)
	if ev.status == "Skipped" or exhausted:
		frappe.db.get_value("Space Lifecycle Event", ev.name, "modified", for_update=True)
		ev.reload()
		ev.recipient = recipient
		ev.retry_count = 0
		ev.status = "Pending"
		ev.details = json.dumps(payload, default=str)
		ev.save(ignore_permissions=True)
		return _deliver(ev.name, now)
	return "duplicate"


def retry_failed(now: Any = None) -> int:
	"""Hourly: queue again every Failed email that is due and has retries left. Returns how many were tried."""
	now = now or lc.utcnow()
	rows = frappe.get_all(
		"Space Lifecycle Event",
		filters={"status": "Failed", "kind": ("in", EMAIL_KINDS), "retry_count": ("<", MAX_RETRIES), "next_retry_at": ("<=", now)},
		pluck="name", limit_page_length=50,
	)
	for name in rows:
		frappe.db.get_value("Space Lifecycle Event", name, "modified", for_update=True)
		if frappe.db.get_value("Space Lifecycle Event", name, "status") != "Failed":
			continue  # another worker got it
		_deliver(name, now)
	return len(rows)


def warning_key(subscription: str, cycle: str) -> str:
	return f"inactivity_warning:{subscription}:{cycle}"


def warning_state(subscription: str, cycle: str) -> str:
	"""State of this episode's warning email for the decision table: none / sent / pending / stalled."""
	ev = lc.event_by_key(warning_key(subscription, cycle))
	if ev is None:
		return "none"
	return {"Sent": "sent", "Pending": "pending"}.get(ev.status, "stalled")
