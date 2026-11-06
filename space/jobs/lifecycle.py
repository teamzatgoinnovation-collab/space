"""Hourly lifecycle scheduler: trial / payment expiry, the inactivity warning and the scheduled deletion.

Idempotent by construction: every subscription is handled under a row lock, every email and every deletion has a
unique dedupe key, and a run only acts on what is due -- running it twice (or from two workers) changes nothing
the second time. Deletion is a dry run unless Space Settings > "Delete inactive sites automatically" is ticked.
"""

from __future__ import annotations

from datetime import timedelta

import frappe

from space.services import lifecycle as lc
from space.services import lifecycle_mail as mail

NOT_LIVE_SITE_STATUSES = ("Draft", "Provisioning", "Failed", "Deleted")


def run_hourly() -> dict[str, int]:
	counts = {"processed": 0, "errors": 0, "emails_retried": 0}
	names = frappe.get_all(
		"Space Subscription",
		filters={"lifecycle_status": ("in", [s for s in lc.STATUSES if s != lc.DELETED])},
		pluck="name",
	)
	for name in names:
		try:
			process_subscription(name)
			frappe.db.commit()
			counts["processed"] += 1
		except Exception:
			frappe.db.rollback()
			counts["errors"] += 1
			frappe.log_error(title=f"Space lifecycle: {name} failed")
	try:
		counts["emails_retried"] = mail.retry_failed()
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Space lifecycle: email retry failed")
	return counts


def refresh_activity_all() -> int:
	"""Daily: read each live tenant's last human activity into the subscription (one `bench execute` per site)."""
	n = 0
	for name in frappe.get_all("Space Subscription", filters={"lifecycle_status": ("not in", ("", lc.DELETED))}, pluck="name"):
		site = lc.site_of(name)
		if site and refresh_activity(name, site):
			n += 1
		frappe.db.commit()
	return n


def refresh_activity(subscription: str, site: str) -> bool:
	try:
		fetch = frappe.get_attr("space_cloud.services.tenant_sync.get_last_activity")
	except Exception:
		return False
	if frappe.db.get_value("Space Site", site, "status") != "Active":
		return False
	try:
		last = fetch(site)
	except Exception:
		frappe.log_error(title=f"Space lifecycle: activity read failed for {site}")
		return False
	current = lc.as_utc(frappe.db.get_value("Space Subscription", subscription, "last_activity_at"))
	if last is not None and (current is None or last > current):
		frappe.db.set_value("Space Subscription", subscription, "last_activity_at", last)
	frappe.db.set_value("Space Subscription", subscription, "activity_checked_at", lc.utcnow())
	return True


def _shield(name: str, site: str, domain: str | None) -> dict[str, bool]:
	doc_hold = bool(frappe.db.get_value("Space Subscription", name, "deletion_hold"))
	return {
		"held": doc_hold,
		"excluded": lc.is_excluded(site) or lc.is_excluded(domain),
		"protected": bool(domain and domain.lower() in lc.protected_domains()),
	}


def process_subscription(name: str, now=None, *, recheck: bool = True) -> str:
	"""One subscription, one pass. Returns what happened (for tests and logs)."""
	now = now or lc.utcnow()
	lc.lock(name)
	doc = frappe.get_doc("Space Subscription", name)
	status = doc.get("lifecycle_status")
	if not status or status == lc.DELETED:
		return "skip"
	site = lc.site_of(name)
	site_row = frappe.db.get_value("Space Site", site, ["domain", "status", "customer"], as_dict=True) if site else None
	if not site_row:
		return "no_site"
	if site_row.status in NOT_LIVE_SITE_STATUSES:
		return "site_not_live"
	s = lc.settings()
	grace = timedelta(days=s["payment_grace_days"])
	trial_end, paid_until = lc.as_utc(doc.get("trial_ends_at")), lc.as_utc(doc.get("paid_until"))
	paid = lc.is_paid(paid_until, now)

	# 1. time-based state changes
	if status == lc.TRIAL and trial_end and now >= trial_end and not paid:
		lc.transition(name, lc.EXPIRED, reason="trial ended")
		return "expired"
	if status == lc.ACTIVE and paid_until and now > paid_until + grace:
		lc.transition(name, lc.PENDING_PAYMENT, reason="payment overdue")
		return "pending_payment"

	# 2. trial ending reminder (3 days before; once)
	if status == lc.TRIAL and trial_end and not paid and 0 < lc.remaining_days(trial_end, now) <= 3:
		_trial_ending_email(doc, site, site_row)

	# 3. inactivity clock. A site whose activity could not be read recently is "unknown", never "inactive":
	# nothing is warned or deleted on a guess.
	checked = lc.as_utc(doc.get("activity_checked_at"))
	needs_activity = status in (lc.TRIAL, lc.EXPIRED, lc.PENDING_PAYMENT, lc.SUSPENDED) and not paid
	if needs_activity and (checked is None or now - checked > timedelta(days=3)):
		return "activity_unknown"
	shield = _shield(name, site, site_row.domain)
	last = lc.as_utc(doc.get("last_activity_at"))
	cycle = (last.isoformat() if last else "never")
	warned_at = lc.as_utc(doc.get("inactivity_warning_sent_at"))
	if status == lc.DELETION_SCHEDULED and recheck:
		if not refresh_activity(name, site):
			return "recheck_unavailable"  # cannot confirm it is still inactive: keep waiting, never delete blind
		doc.reload()
		last = lc.as_utc(doc.get("last_activity_at"))
	decision = lc.inactivity_decision(
		inactive=lc.inactive_days(last, doc.get("trial_started_at"), now),
		warn_after=s["inactivity_warning_days"], delete_after=s["inactivity_delete_days"],
		status=status, warned_for_cycle=mail.warning_sent(name, cycle),
		scheduled_at=doc.get("deletion_scheduled_at"), now=now, paid=paid, **shield,
		activity_after_warning=bool(last and warned_at and last > warned_at),
		manual=bool(doc.get("deletion_manual")),
	)
	if decision == "warn":
		return _warn_and_schedule(doc, site, site_row, cycle, now, s)
	if decision == "cancel":
		return _cancel_deletion(doc, site, "re-check: activity, payment or protection")
	if decision == "delete":
		return _delete(doc, site, now, s)
	return decision


def _trial_ending_email(doc, site: str, site_row) -> None:
	customer_email = frappe.db.get_value("Space Customer", site_row.customer, "email")
	ends = lc.as_utc(doc.get("trial_ends_at"))
	mail.send_once(
		kind="trial_ending_email", subscription=doc.name, site=site,
		dedupe_key=f"trial_ending:{doc.name}:{ends.isoformat() if ends else ''}", recipient=customer_email,
		subject=f"[ZatGo Space] Your free trial ends soon ({site_row.domain})",
		heading="Your free trial ends in 3 days or less",
		body="After the trial your site stays viewable but becomes read-only. Choose a monthly plan to keep working.",
		rows=[("Site", site_row.domain), ("Trial ends (UTC)", ends.strftime("%Y-%m-%d %H:%M") if ends else "-")],
	)


def _warn_and_schedule(doc, site: str, site_row, cycle: str, now, s) -> str:
	customer_email = frappe.db.get_value("Space Customer", site_row.customer, "email")
	scheduled = now + timedelta(days=max(1, s["inactivity_delete_days"] - s["inactivity_warning_days"]))
	last = lc.as_utc(doc.get("last_activity_at"))
	result = mail.send_once(
		kind="inactivity_warning_email", subscription=doc.name, site=site,
		dedupe_key=f"inactivity_warning:{doc.name}:{cycle}", recipient=customer_email,
		subject=f"[ZatGo Space] {site_row.domain} will be deleted on {scheduled:%Y-%m-%d} unless you sign in",
		heading="Your site is scheduled for deletion",
		body=(f"We have seen no activity on this site for {s['inactivity_warning_days']} days. If it stays inactive it "
		      f"will be deleted in {s['inactivity_delete_days'] - s['inactivity_warning_days']} days."),
		rows=[
			("Site name", site), ("Site URL", f"https://{site_row.domain}"),
			("Last activity (UTC)", last.strftime("%Y-%m-%d %H:%M") if last else "none recorded"),
			("Scheduled deletion (UTC)", scheduled.strftime("%Y-%m-%d %H:%M")),
		],
		advice=("To keep the site, sign in and use it, or choose a monthly plan. If you want to keep your data, "
		        "download a backup from your ZatGo Space dashboard now: a deleted site cannot be recovered."),
		now=now,
	)
	if result != "Sent":
		return f"warning_{result}"  # deletion is only ever scheduled behind a warning that was actually sent
	lc.transition(
		doc.name, lc.DELETION_SCHEDULED, reason="inactivity warning sent",
		inactivity_warning_sent_at=now, deletion_scheduled_at=scheduled,
	)
	lc.record_event("deletion_scheduled", subscription=doc.name, site=site,
		dedupe_key=f"deletion_scheduled:{doc.name}:{scheduled.isoformat()}", details={"at": str(scheduled)})
	return "warned"


def _cancel_deletion(doc, site: str, reason: str) -> str:
	target = doc.get("pre_deletion_status") or lc.EXPIRED
	paid = lc.is_paid(doc.get("paid_until"))
	if paid:
		target = lc.ACTIVE
	lc.transition(doc.name, target if target in lc.RESTORABLE else lc.EXPIRED, reason=f"deletion cancelled: {reason}")
	lc.record_event("deletion_cancelled", subscription=doc.name, site=site, details={"reason": reason})
	return "cancelled"


def _delete(doc, site: str, now, s) -> str:
	key = f"{doc.name}:{lc.as_utc(doc.get('deletion_scheduled_at')).isoformat()}"
	if not s["auto_deletion_enabled"]:
		lc.record_event("deletion_dry_run", subscription=doc.name, site=site, dedupe_key=f"dry:{key}",
			details={"would_delete": site, "note": "auto deletion is off in Space Settings"})
		return "dry_run"
	if lc.record_event("deletion_started", subscription=doc.name, site=site, dedupe_key=f"delete:{key}",
			details={"source": "inactivity"}) is None:
		return "already_started"
	enqueue = frappe.get_attr("space_cloud.jobs.lifecycle.enqueue_delete_site")
	enqueue(site, source="inactivity")
	return "deleting"
