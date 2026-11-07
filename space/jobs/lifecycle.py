"""Hourly lifecycle scheduler: trial / payment expiry, the inactivity warning and the scheduled deletion.

Idempotent by construction: every subscription is handled under a row lock, every email and every deletion has a
unique dedupe key, and a run only acts on what is due -- running it twice (or from two workers) changes nothing
the second time.

The inactivity sequence, in order, each step only after the previous one is CONFIRMED:
  1. 10 days without human activity -> ONE warning email is queued (or, with automatic deletion off, only a
     "would warn" event is logged and nobody is contacted);
  2. the Email Queue row says the warning was Sent -> deletion is scheduled for 5 days later;
  3. at the due time everything is re-checked (activity, payment, hold, exclusion, protected domain, same customer,
     warning delivered) -> any failure cancels or waits;
  4. only then, and only with "Delete inactive sites automatically" ticked, the deletion job is queued -- and the
     job repeats the same re-check at the instant it runs.
Anything that cannot be confirmed (activity unreadable, warning undelivered) means NO deletion.
"""

from __future__ import annotations

import time
from datetime import timedelta

import frappe

from space.services import lifecycle as lc
from space.services import lifecycle_mail as mail

NOT_LIVE_SITE_STATUSES = ("Draft", "Provisioning", "Failed", "Deleted")
HOURLY_BUDGET_S = 20 * 60  # these run on the long queue (25 min timeout); stop well before it
DAILY_BUDGET_S = 20 * 60
ACTIVITY_MAX_AGE = timedelta(days=3)
RECHECK_WINDOW = timedelta(hours=1)  # re-read the tenant's activity only when a deletion is due within this


def _trunc(dt):
	"""Scheduled dates are second-precise so the dedupe keys built from them are stable across the database."""
	return dt.replace(microsecond=0)


def run_hourly(budget_s: int = HOURLY_BUDGET_S) -> dict[str, int]:
	deadline = time.monotonic() + budget_s
	counts = {"processed": 0, "errors": 0, "skipped_for_time": 0, "emails_confirmed": 0, "emails_retried": 0, "pushed": 0}
	try:
		counts["emails_confirmed"] = mail.confirm_delivery()["sent"]
		counts["emails_retried"] = mail.retry_failed()
		lc.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Space lifecycle: email confirmation failed")
	names = frappe.get_all(
		"Space Subscription",
		filters={"lifecycle_status": ("in", [s for s in lc.STATUSES if s != lc.DELETED])},
		pluck="name", order_by="modified asc",
	)
	for i, name in enumerate(names):
		if time.monotonic() > deadline:
			counts["skipped_for_time"] = len(names) - i
			frappe.log_error(title=f"Space lifecycle: hourly budget used up, {counts['skipped_for_time']} subscriptions left for the next run")
			break
		try:
			process_subscription(name)
			lc.commit()
			counts["processed"] += 1
		except Exception:
			frappe.db.rollback()
			counts["errors"] += 1
			frappe.log_error(title=f"Space lifecycle: {name} failed")
	try:
		counts["pushed"] = lc.reconcile_pushes(deadline=deadline)["pushed"]
		lc.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Space lifecycle: tenant state reconcile failed")
	return counts


def refresh_activity_all(budget_s: int = DAILY_BUDGET_S) -> int:
	"""Daily: read each live tenant's last human activity into the subscription (one `bench execute` per site),
	least recently checked first, within a time budget."""
	deadline = time.monotonic() + budget_s
	n = 0
	for name in frappe.get_all(
		"Space Subscription", filters={"lifecycle_status": ("not in", ("", lc.DELETED))}, pluck="name",
		order_by="activity_checked_at asc",
	):
		if time.monotonic() > deadline:
			frappe.log_error(title="Space lifecycle: daily activity refresh budget used up")
			break
		site = lc.site_of(name)
		if site and refresh_activity(name, site):
			n += 1
		lc.commit()
	return n


def refresh_activity(subscription: str, site: str) -> bool:
	"""Read the tenant's last human activity. False means UNKNOWN (unreachable, errored): never "inactive"."""
	try:
		fetch = frappe.get_attr("space_cloud.services.tenant_sync.get_last_activity")
	except Exception:
		return False
	if frappe.db.get_value("Space Site", site, "status") not in lc.LIVE_SITE_STATUSES:
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
	return {
		"held": bool(frappe.db.get_value("Space Subscription", name, "deletion_hold")),
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

	# 2. trial reminder -- only while read-only enforcement is on (the mail says the site becomes read-only)
	if status == lc.TRIAL and s["lifecycle_enforcement_enabled"] and trial_end and not paid and 0 < lc.remaining_days(trial_end, now) <= 3:
		_trial_ending_email(doc, site, site_row, now)

	# 3. inactivity clock. A site whose activity could not be read recently is "unknown", never "inactive":
	# nothing is warned or deleted on a guess.
	manual = status == lc.DELETION_SCHEDULED and bool(doc.get("deletion_manual"))
	checked = lc.as_utc(doc.get("activity_checked_at"))
	needs_activity = status in (lc.TRIAL, lc.EXPIRED, lc.PENDING_PAYMENT, lc.SUSPENDED) and not paid
	if needs_activity and (checked is None or now - checked > ACTIVITY_MAX_AGE):
		return "activity_unknown"
	shield = _shield(name, site, site_row.domain)
	last = lc.as_utc(doc.get("last_activity_at"))
	warned_at = lc.as_utc(doc.get("inactivity_warning_sent_at"))
	if status == lc.DELETION_SCHEDULED and recheck and not manual:
		due = lc.as_utc(doc.get("deletion_scheduled_at"))
		if due and now >= due - RECHECK_WINDOW:
			if not refresh_activity(name, site):
				return "recheck_unavailable"  # cannot confirm it is still inactive: wait, never delete blind
			doc.reload()
			last = lc.as_utc(doc.get("last_activity_at"))
	cycle = lc.warning_cycle(name, last)
	decision = lc.inactivity_decision(
		inactive=lc.inactive_days(last, doc.get("trial_started_at"), now),
		warn_after=s["inactivity_warning_days"], delete_after=s["inactivity_delete_days"],
		status=status, warning=mail.warning_state(name, cycle) if status != lc.DELETION_SCHEDULED else "none",
		scheduled_at=doc.get("deletion_scheduled_at"), now=now, paid=paid, **shield,
		activity_after_warning=bool(last and warned_at and last > warned_at),
		manual=manual, auto_enabled=bool(s["auto_deletion_enabled"]),
	)
	if decision == "dry_run_warn":
		lc.record_event(
			"deletion_dry_run", subscription=name, site=site, dedupe_key=f"drywarn:{name}:{cycle}",
			details={"would": "warn", "note": "automatic deletion is off: nobody was contacted"},
		)
		return "dry_run_warn"
	if decision == "warn":
		return _warn(doc, site, site_row, cycle, now, s)
	if decision == "wait_warning":
		return "waiting_for_warning"
	if decision == "schedule":
		return _schedule(doc, site, site_row, cycle, now, s)
	if decision == "cancel":
		return _cancel_deletion(doc, site, "re-check: activity, payment or protection")
	if decision == "delete":
		return _delete(doc, site, now, s, manual)
	return decision  # none / wait


def _customer_email(site_row) -> str | None:
	return frappe.db.get_value("Space Customer", site_row.customer, "email")


def _trial_ending_email(doc, site: str, site_row, now) -> None:
	ends = lc.as_utc(doc.get("trial_ends_at"))
	mail.send_once(
		kind="trial_ending_email", subscription=doc.name, site=site,
		dedupe_key=f"trial_ending:{doc.name}:{ends.isoformat() if ends else ''}", recipient=_customer_email(site_row),
		subject=f"[ZatGo Space] Your free trial ends soon ({site_row.domain})",
		heading="Your free trial ends in 3 days or less",
		body="After the trial your site stays viewable but becomes read-only. Choose a monthly plan to keep working.",
		rows=[("Site", site_row.domain), ("Trial ends (UTC)", ends.strftime("%Y-%m-%d %H:%M") if ends else "-")],
		now=now,
	)


def _warn(doc, site: str, site_row, cycle: str, now, s) -> str:
	"""Step 1: queue the warning. Deletion is NOT scheduled here -- only once the warning is confirmed delivered."""
	days_left = max(1, s["inactivity_delete_days"] - s["inactivity_warning_days"])
	promised = now + timedelta(days=days_left)
	last = lc.as_utc(doc.get("last_activity_at"))
	result = mail.send_once(
		kind="inactivity_warning_email", subscription=doc.name, site=site,
		dedupe_key=mail.warning_key(doc.name, cycle), recipient=_customer_email(site_row),
		subject=f"[ZatGo Space] {site_row.domain} will be deleted on {promised:%Y-%m-%d} unless you sign in",
		heading="Your site is scheduled for deletion",
		body=(f"We have seen no activity on this site for {s['inactivity_warning_days']} days. If it stays inactive it "
		      f"will be deleted in {days_left} days."),
		rows=[
			("Site name", site), ("Site URL", f"https://{site_row.domain}"),
			("Last activity (UTC)", last.strftime("%Y-%m-%d %H:%M") if last else "none recorded"),
			("Scheduled deletion (UTC)", promised.strftime("%Y-%m-%d %H:%M")),
		],
		advice=("To keep the site, sign in and use it, or choose a monthly plan. If you want to keep your data, "
		        "download a backup from your ZatGo Space dashboard now: a deleted site cannot be recovered."),
		now=now,
	)
	return f"warning_{result}"


def _schedule(doc, site: str, site_row, cycle: str, now, s) -> str:
	"""Step 2: the warning was delivered -- schedule the deletion (never earlier than the date the email promised)."""
	warning = lc.event_by_key(mail.warning_key(doc.name, cycle))
	scheduled = _trunc(now + timedelta(days=max(1, s["inactivity_delete_days"] - s["inactivity_warning_days"])))
	lc.transition(
		doc.name, lc.DELETION_SCHEDULED, reason="inactivity warning delivered",
		inactivity_warning_sent_at=now, deletion_scheduled_at=scheduled, deletion_manual=0,
	)
	lc.record_event(
		"deletion_scheduled", subscription=doc.name, site=site,
		dedupe_key=f"deletion_scheduled:{doc.name}:{scheduled.isoformat()}",
		details={"at": str(scheduled), "customer": site_row.customer, "recipient": warning.recipient if warning else None,
		         "warning_event": warning.name if warning else None, "manual": False},
	)
	return "scheduled"


def _cancel_deletion(doc, site: str, reason: str) -> str:
	target = doc.get("pre_deletion_status") or lc.EXPIRED
	if lc.is_paid(doc.get("paid_until")):
		target = lc.ACTIVE
	lc.transition(doc.name, target if target in lc.RESTORABLE else lc.EXPIRED, reason=f"deletion cancelled: {reason}")
	lc.record_event("deletion_cancelled", subscription=doc.name, site=site, details={"reason": reason})
	return "cancelled"


def _delete(doc, site: str, now, s, manual: bool) -> str:
	"""Step 3: due. Re-check everything, then queue the deletion job (which repeats the checks when it runs)."""
	source = "admin_schedule" if manual else "inactivity"
	scheduled = _trunc(lc.as_utc(doc.get("deletion_scheduled_at")))
	if not manual and not s["auto_deletion_enabled"]:
		lc.record_event("deletion_dry_run", subscription=doc.name, site=site, dedupe_key=f"dry:{doc.name}:{scheduled.isoformat()}",
			details={"would_delete": site, "note": "auto deletion is off in Space Settings"})
		return "dry_run"
	site_doc = frappe.get_doc("Space Site", site)
	try:
		lc.assert_scheduled_deletion_valid(site_doc, source, scheduled)
	except lc.DeletionRefused as exc:
		if getattr(exc, "wait", False):  # the warning is not confirmed delivered yet: keep waiting, delete nothing
			lc.record_event("deletion_failed", subscription=doc.name, site=site, dedupe_key=f"blocked:{doc.name}:{scheduled.isoformat()}",
				details={"reason": str(exc)})
			return "waiting_for_warning"
		return _cancel_deletion(doc, site, str(exc))
	started = lc.record_event("deletion_started", subscription=doc.name, site=site,
		dedupe_key=f"delete:{doc.name}:{scheduled.isoformat()}", details={"source": source})
	if started is None:
		return "already_started"
	try:
		frappe.get_attr("space_cloud.jobs.lifecycle.enqueue_delete_site")(site, source=source, expected_scheduled_at=scheduled.isoformat())
	except Exception:
		frappe.log_error(title=f"Space lifecycle: could not queue the deletion of {site}")
		frappe.delete_doc("Space Lifecycle Event", started, ignore_permissions=True, force=True)  # let the next run retry
		return "enqueue_failed"
	return "deleting"
