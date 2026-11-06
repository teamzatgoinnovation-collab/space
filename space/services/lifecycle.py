"""Subscription lifecycle: 30-day trial, paid months, read-only states and the inactivity clock.

`Space Subscription.lifecycle_status` is the authoritative state of a customer's ERP site; `transition()` is the
only writer. Dates are stored in UTC (naive datetimes, labelled "(UTC)" on the form) and compared in UTC, so the
site's display timezone never changes when a trial ends.

Everything with decisions in it is a pure function over plain values (`remaining_days`, `inactive_days`,
`inactivity_decision`, `is_paid`, `next_paid_until`) so the rules are tested without a database; the functions
that touch documents are thin around them.

Deleting sites is not done here: the scheduler (space/jobs/lifecycle.py) decides, space_cloud performs.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import frappe
from frappe.utils import get_datetime

TRIAL, ACTIVE, EXPIRED, SUSPENDED = "TRIAL", "ACTIVE", "EXPIRED", "SUSPENDED"
PENDING_PAYMENT, DELETION_SCHEDULED, DELETED = "PENDING_PAYMENT", "DELETION_SCHEDULED", "DELETED"
STATUSES = (TRIAL, ACTIVE, EXPIRED, SUSPENDED, PENDING_PAYMENT, DELETION_SCHEDULED, DELETED)

READ_ONLY = {EXPIRED, SUSPENDED, PENDING_PAYMENT, DELETION_SCHEDULED, DELETED}
RESTORABLE = {TRIAL, ACTIVE, EXPIRED, SUSPENDED, PENDING_PAYMENT}

ALLOWED: dict[str, set[str]] = {
	TRIAL: {ACTIVE, EXPIRED, SUSPENDED, DELETION_SCHEDULED},
	ACTIVE: {PENDING_PAYMENT, SUSPENDED, DELETION_SCHEDULED, EXPIRED},
	EXPIRED: {ACTIVE, TRIAL, SUSPENDED, DELETION_SCHEDULED},
	PENDING_PAYMENT: {ACTIVE, EXPIRED, SUSPENDED, DELETION_SCHEDULED},
	SUSPENDED: {ACTIVE, TRIAL, EXPIRED, PENDING_PAYMENT, DELETION_SCHEDULED},
	DELETION_SCHEDULED: RESTORABLE | {DELETED},
	DELETED: set(),
}

# New state -> legacy columns, kept in step so older code (billing reports, list views) still reads sensibly.
LEGACY = {
	TRIAL: ("Trial", "Trial"),
	ACTIVE: ("Active", "Paid"),
	EXPIRED: ("Expired", "Expired"),
	PENDING_PAYMENT: ("Active", "Unpaid"),
	SUSPENDED: ("Suspended", "Suspended"),
	DELETION_SCHEDULED: ("Suspended", "Suspended"),
	DELETED: ("Cancelled", "Expired"),
}

DEFAULTS = {
	"trial_days_default": 30,
	"payment_grace_days": 3,
	"inactivity_warning_days": 10,
	"inactivity_delete_days": 15,
}


# --- pure rules --------------------------------------------------------------------------------------------------

def utcnow() -> datetime:
	"""Naive UTC, the representation every lifecycle Datetime field is stored in."""
	return datetime.now(timezone.utc).replace(tzinfo=None)


def as_utc(value: Any) -> datetime | None:
	if not value:
		return None
	dt = get_datetime(value)
	if dt.tzinfo is not None:
		dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
	return dt


def remaining_days(ends_at: Any, now: datetime | None = None) -> int | None:
	"""Whole days left, rounded up and never negative (29 d 23 h left reads 30; the last hour reads 1; over reads 0)."""
	end = as_utc(ends_at)
	if end is None:
		return None
	now = now or utcnow()
	return max(0, math.ceil((end - now).total_seconds() / 86400))


def inactive_days(last_activity: Any, baseline: Any, now: datetime | None = None) -> float:
	"""Days since the last meaningful human activity; a site that never had any counts from `baseline`
	(when its trial started)."""
	now = now or utcnow()
	since = as_utc(last_activity) or as_utc(baseline)
	if since is None:
		return 0.0
	return max(0.0, (now - since).total_seconds() / 86400)


def is_paid(paid_until: Any, now: datetime | None = None) -> bool:
	end = as_utc(paid_until)
	return bool(end and (now or utcnow()) < end)


def next_paid_until(current_paid_until: Any, now: datetime | None = None, months: int = 1) -> datetime:
	"""A verified payment buys `months` of 30 days on top of whatever is left (never from the past)."""
	now = now or utcnow()
	base = as_utc(current_paid_until)
	start = base if base and base > now else now
	return start + timedelta(days=30 * max(1, int(months)))


def inactivity_decision(
	*,
	inactive: float,
	warn_after: int,
	delete_after: int,
	status: str,
	warned_for_cycle: bool,
	scheduled_at: Any,
	now: datetime,
	paid: bool,
	held: bool,
	excluded: bool,
	protected: bool,
	activity_after_warning: bool,
	manual: bool = False,
) -> str:
	"""What the inactivity clock says right now. One of:

	none      nothing to do
	warn      send the day-10 warning and schedule deletion
	cancel    a scheduled deletion must be called off (activity, payment, protection)
	wait      deletion is scheduled but not due yet
	delete    the scheduled date passed and every re-check still holds
	"""
	shield = held or excluded or protected
	if status == DELETION_SCHEDULED:
		due = as_utc(scheduled_at)
		if shield or paid or due is None:
			return "cancel"
		if manual:  # an admin's schedule does not depend on the inactivity clock, only on the shields above
			return "wait" if now < due else "delete"
		if activity_after_warning or inactive < warn_after:
			return "cancel"
		if now < due or inactive < delete_after:
			return "wait"
		return "delete"
	if shield or paid or status in (ACTIVE, DELETED):
		return "none"
	if inactive >= warn_after and not warned_for_cycle:
		return "warn"
	return "none"


# --- settings ----------------------------------------------------------------------------------------------------

def settings() -> dict[str, Any]:
	out: dict[str, Any] = dict(DEFAULTS)
	out.update({"lifecycle_enforcement_enabled": 0, "auto_deletion_enabled": 0, "protected_domains": ""})
	try:
		doc = frappe.get_single("Space Settings")
		for key in out:
			value = doc.get(key)
			if value not in (None, ""):
				out[key] = value
	except Exception:
		frappe.log_error(title="Space lifecycle: settings unreadable, using defaults")
	for key in DEFAULTS:
		out[key] = int(out[key] or DEFAULTS[key])
	return out


def protected_domains() -> set[str]:
	"""Domains that are never deleted: the portal's own site and anything listed in Space Settings."""
	listed = {d.strip().lower() for d in (settings().get("protected_domains") or "").splitlines() if d.strip()}
	listed.add((frappe.local.site or "").lower())
	return {d for d in listed if d}


# --- events (audit trail + dedupe) -------------------------------------------------------------------------------

def record_event(
	kind: str,
	*,
	subscription: str | None = None,
	site: str | None = None,
	dedupe_key: str | None = None,
	status: str = "Done",
	recipient: str | None = None,
	actor: str | None = None,
	details: Any = None,
	retry_count: int = 0,
	next_retry_at: Any = None,
) -> str | None:
	"""Insert an audit/dedupe row. With a `dedupe_key` the database's unique constraint is the arbiter: a second
	insert of the same key returns None instead of raising, so "at most once" holds across workers."""
	doc = frappe.get_doc(
		{
			"doctype": "Space Lifecycle Event",
			"subscription": subscription,
			"site": site,
			"kind": kind,
			"status": status,
			"dedupe_key": dedupe_key,
			"recipient": recipient,
			"actor": actor or frappe.session.user,
			"details": details if isinstance(details, str) or details is None else json.dumps(details, default=str),
			"retry_count": retry_count,
			"next_retry_at": next_retry_at,
		}
	)
	frappe.db.savepoint("lifecycle_event")
	try:
		doc.insert(ignore_permissions=True)
	except frappe.UniqueValidationError:
		frappe.db.rollback(save_point="lifecycle_event")
		return None
	return doc.name


# --- subscription access -----------------------------------------------------------------------------------------

def subscription_for_site(site: str) -> str | None:
	return frappe.db.get_value("Space Site", site, "subscription") or frappe.db.get_value(
		"Space Subscription", {"customer": frappe.db.get_value("Space Site", site, "customer")}, "name"
	)


def lock(name: str) -> None:
	"""Row lock for the rest of the transaction: two workers (or a worker and an admin click) serialise here."""
	frappe.db.get_value("Space Subscription", name, "modified", for_update=True)


def is_excluded(site: str | None) -> bool:
	return bool(site and frappe.db.exists("Space Deletion Exclusion", site))


def transition(
	subscription: str,
	new_status: str,
	*,
	reason: str = "",
	actor: str | None = None,
	push: bool = True,
	**fields: Any,
) -> bool:
	"""Move a subscription to `new_status` (the only writer of `lifecycle_status`). Returns False when it is
	already there (idempotent) and raises on a move the table does not allow. Extra `fields` are written in the
	same save (dates, hold flags ...)."""
	if new_status not in STATUSES:
		frappe.throw(f"Unknown lifecycle status {new_status}")
	lock(subscription)
	doc = frappe.get_doc("Space Subscription", subscription)
	old = doc.get("lifecycle_status") or ""
	if old == new_status and not fields:
		return False
	if old and old != new_status and new_status not in ALLOWED.get(old, set()):
		frappe.throw(f"Lifecycle move {old} -> {new_status} is not allowed")

	if new_status == DELETION_SCHEDULED and old != DELETION_SCHEDULED:
		doc.pre_deletion_status = old
	if old == DELETION_SCHEDULED and new_status != DELETION_SCHEDULED:
		doc.deletion_scheduled_at = None
		doc.inactivity_warning_sent_at = fields.pop("inactivity_warning_sent_at", None) if "inactivity_warning_sent_at" in fields else None
		doc.pre_deletion_status = None

	doc.lifecycle_status = new_status
	legacy_status, legacy_payment = LEGACY[new_status]
	doc.status, doc.payment_status = legacy_status, legacy_payment
	for key, value in fields.items():
		doc.set(key, value)
	doc.flags.ignore_validate_update_after_submit = True
	doc.save(ignore_permissions=True)

	if old != new_status:
		record_event(
			"transition",
			subscription=subscription,
			site=frappe.db.get_value("Space Site", {"subscription": subscription}, "name"),
			actor=actor,
			details={"from": old, "to": new_status, "reason": reason},
		)
	if push:
		push_state(subscription)
	return True


# --- state ----------------------------------------------------------------------------------------------------

def site_of(subscription: str) -> str | None:
	return frappe.db.get_value("Space Site", {"subscription": subscription}, "name")


def state_payload(subscription: str) -> dict[str, Any]:
	"""What the tenant site is told (subscription_gate.py reads it from site_config)."""
	s = settings()
	doc = frappe.get_doc("Space Subscription", subscription)
	site = site_of(subscription)
	domain = frappe.db.get_value("Space Site", site, "domain") if site else None
	status = doc.get("lifecycle_status") or TRIAL
	portal = f"https://space.{(domain or '').split('.', 1)[1]}" if domain and "." in domain else None
	return {
		"enforcement_on": 1 if s["lifecycle_enforcement_enabled"] else 0,
		"status": status,
		"trial_ends_at": _iso(doc.get("trial_ends_at")),
		"paid_until": _iso(doc.get("paid_until")),
		"site_url": f"https://{domain}" if domain else None,
		"portal_url": portal,
		"synced_at": datetime.now(timezone.utc).isoformat(),
	}


def _iso(value: Any) -> str | None:
	dt = as_utc(value)
	return dt.replace(tzinfo=timezone.utc).isoformat() if dt else None


def push_state(subscription: str) -> bool:
	"""Tell the tenant its state (no-op without space_cloud, a site, or a managed domain). Never raises:
	a failed push is logged and the hourly reconcile tries again."""
	site = site_of(subscription)
	if not site:
		return False
	try:
		pusher = frappe.get_attr("space_cloud.services.tenant_sync.push_state")
	except Exception:
		return False
	try:
		return bool(pusher(site, state_payload(subscription)))
	except Exception:
		frappe.log_error(title=f"Space lifecycle: push to {site} failed")
		return False


# --- operations ------------------------------------------------------------------------------------------------

def start_trial(subscription: str, *, started_at: datetime | None = None, force: bool = False) -> bool:
	"""Start the free trial when provisioning succeeds. Idempotent: a second call does nothing."""
	lock(subscription)
	doc = frappe.get_doc("Space Subscription", subscription)
	if doc.get("trial_started_at") and not force:
		return False
	started = started_at or utcnow()
	plan = doc.get("plan")
	if plan and not float(frappe.db.get_value("Space Plan", plan, "monthly_price") or 0):
		# A plan that costs nothing has no trial to run out: it stays ACTIVE and unrestricted.
		doc.trial_started_at = started
		doc.save(ignore_permissions=True)
		transition(subscription, ACTIVE, reason="free plan", push=False)
		push_state(subscription)
		return True
	days = settings()["trial_days_default"]
	doc.trial_started_at = started
	doc.trial_ends_at = started + timedelta(days=days)
	doc.trial_ends_on = (started + timedelta(days=days)).date()
	doc.flags.ignore_validate_update_after_submit = True
	doc.save(ignore_permissions=True)
	record_event(
		"trial_started", subscription=subscription, site=site_of(subscription),
		dedupe_key=f"trial_started:{subscription}:{started.isoformat() if force else ''}",
		details={"days": days, "ends_at": str(doc.trial_ends_at)},
	)
	transition(subscription, TRIAL, reason="trial started", push=False)
	push_state(subscription)
	return True


def extend_trial(subscription: str, days: int, *, reason: str, actor: str | None = None) -> None:
	days = int(days)
	if days < 1 or days > 365:
		frappe.throw("Extend by 1 to 365 days")
	lock(subscription)
	doc = frappe.get_doc("Space Subscription", subscription)
	base = max(as_utc(doc.get("trial_ends_at")) or utcnow(), utcnow())
	end = base + timedelta(days=days)
	status = doc.get("lifecycle_status")
	if status in (EXPIRED, SUSPENDED, PENDING_PAYMENT, DELETION_SCHEDULED):
		transition(subscription, TRIAL, reason=f"trial extended: {reason}", actor=actor, push=False,
			trial_ends_at=end, trial_ends_on=end.date())
	else:
		doc.trial_ends_at, doc.trial_ends_on = end, end.date()
		doc.save(ignore_permissions=True)
	record_event("admin_action", subscription=subscription, site=site_of(subscription), actor=actor,
		details={"action": "extend_trial", "days": days, "reason": reason, "ends_at": str(end)})
	push_state(subscription)


def record_payment(subscription: str, *, provider: str, payment_id: str, amount: float, currency: str,
		months: int = 1, actor: str | None = None) -> dict[str, Any]:
	"""A verified payment: one month more of ACTIVE. Idempotent on (provider, payment_id) through the event
	table's unique key, so a retried request or a double click cannot add a second month."""
	lock(subscription)
	key = f"payment:{provider}:{payment_id}"
	first = record_event(
		"payment", subscription=subscription, site=site_of(subscription), dedupe_key=key, actor=actor,
		details={"provider": provider, "payment_id": payment_id, "amount": amount, "currency": currency},
	)
	doc = frappe.get_doc("Space Subscription", subscription)
	if first is None:
		return {"applied": False, "paid_until": _iso(doc.get("paid_until"))}
	until = next_paid_until(doc.get("paid_until"), months=months)
	if (doc.get("lifecycle_status") or TRIAL) == DELETED:
		frappe.throw("This site was deleted")
	transition(subscription, ACTIVE, reason=f"payment {provider}:{payment_id}", actor=actor, push=False,
		paid_until=until, next_billing_date=until.date(), renewal_date=until.date(), end_date=until.date(),
		deletion_hold=doc.get("deletion_hold"))
	frappe.get_doc({
		"doctype": "Space Payment History", "customer": doc.customer, "subscription": subscription,
		"amount": amount, "currency": currency, "status": "Succeeded", "method": provider, "reference": payment_id,
	}).insert(ignore_permissions=True) if _payment_history_ready() else None
	push_state(subscription)
	return {"applied": True, "paid_until": _iso(until)}


def _payment_history_ready() -> bool:
	try:
		meta = frappe.get_meta("Space Payment History")
		return all(meta.has_field(f) for f in ("customer", "amount", "status", "reference"))
	except Exception:
		return False


def view(subscription: str, now: datetime | None = None) -> dict[str, Any]:
	"""One customer's picture of a site (dashboard + admin card)."""
	now = now or utcnow()
	doc = frappe.get_doc("Space Subscription", subscription)
	site = site_of(subscription)
	site_doc = frappe.db.get_value("Space Site", site, ["domain", "status", "plan"], as_dict=True) if site else None
	price = frappe.db.get_value("Space Plan", doc.plan, "monthly_price") if doc.plan else None
	s = settings()
	last = as_utc(doc.get("last_activity_at"))
	baseline = doc.get("trial_started_at")
	return {
		"subscription": subscription,
		"site": site,
		"site_url": f"https://{site_doc.domain}" if site_doc and site_doc.domain else None,
		"site_status": site_doc.status if site_doc else None,
		"lifecycle_status": doc.get("lifecycle_status") or None,
		"plan": doc.plan,
		"monthly_price": price,
		"trial_started_at": _iso(doc.get("trial_started_at")),
		"trial_ends_at": _iso(doc.get("trial_ends_at")),
		"remaining_days": remaining_days(doc.get("trial_ends_at"), now) if (doc.get("lifecycle_status") == TRIAL) else None,
		"paid_until": _iso(doc.get("paid_until")),
		"next_billing_date": str(doc.get("next_billing_date") or "") or None,
		"last_activity_at": _iso(last),
		"inactive_days": round(inactive_days(last, baseline, now), 1),
		"deletion_scheduled_at": _iso(doc.get("deletion_scheduled_at")),
		"deletion_hold": bool(doc.get("deletion_hold")),
		"read_only": (doc.get("lifecycle_status") in READ_ONLY) and bool(s["lifecycle_enforcement_enabled"]),
	}


def admin_activate(subscription: str, months: int, *, reason: str, actor: str | None = None) -> None:
	"""Admin grants paid months without a payment (comp, offline payment). Audited with the reason."""
	months = int(months)
	if months < 1 or months > 24:
		frappe.throw("Grant 1 to 24 months")
	lock(subscription)
	until = next_paid_until(frappe.db.get_value("Space Subscription", subscription, "paid_until"), months=months)
	transition(subscription, ACTIVE, reason=f"admin activation: {reason}", actor=actor, push=False,
		paid_until=until, next_billing_date=until.date(), renewal_date=until.date(), end_date=until.date(),
		deletion_manual=0)
	record_event("admin_action", subscription=subscription, site=site_of(subscription), actor=actor,
		details={"action": "activate", "months": months, "reason": reason, "paid_until": str(until)})
	push_state(subscription)


def admin_suspend(subscription: str, *, reason: str, actor: str | None = None) -> None:
	transition(subscription, SUSPENDED, reason=f"admin suspension: {reason}", actor=actor)
	record_event("admin_action", subscription=subscription, site=site_of(subscription), actor=actor,
		details={"action": "suspend", "reason": reason})


def admin_reactivate(subscription: str, *, reason: str, actor: str | None = None) -> str:
	"""Back from SUSPENDED / EXPIRED / PENDING_PAYMENT to what the dates say: ACTIVE if paid, TRIAL if the trial
	still runs, otherwise EXPIRED (read-only until someone pays or extends)."""
	doc = frappe.get_doc("Space Subscription", subscription)
	if is_paid(doc.get("paid_until")):
		target = ACTIVE
	elif as_utc(doc.get("trial_ends_at")) and as_utc(doc.get("trial_ends_at")) > utcnow():
		target = TRIAL
	else:
		target = EXPIRED
	transition(subscription, target, reason=f"admin reactivation: {reason}", actor=actor)
	record_event("admin_action", subscription=subscription, site=site_of(subscription), actor=actor,
		details={"action": "reactivate", "to": target, "reason": reason})
	return target


def cancel_deletion(subscription: str, *, reason: str, actor: str | None = None) -> str:
	doc = frappe.get_doc("Space Subscription", subscription)
	if doc.get("lifecycle_status") != DELETION_SCHEDULED:
		frappe.throw("Deletion is not scheduled")
	target = doc.get("pre_deletion_status") or EXPIRED
	if is_paid(doc.get("paid_until")):
		target = ACTIVE
	transition(subscription, target if target in RESTORABLE else EXPIRED, reason=f"deletion cancelled by admin: {reason}",
		actor=actor, deletion_manual=0)
	record_event("deletion_cancelled", subscription=subscription, site=site_of(subscription), actor=actor,
		details={"reason": reason, "by": "admin"})
	return target


def set_hold(subscription: str, hold: bool, *, reason: str, actor: str | None = None) -> None:
	lock(subscription)
	doc = frappe.get_doc("Space Subscription", subscription)
	doc.deletion_hold = 1 if hold else 0
	doc.deletion_hold_reason = reason if hold else None
	doc.save(ignore_permissions=True)
	record_event("admin_action", subscription=subscription, site=site_of(subscription), actor=actor,
		details={"action": "hold" if hold else "release_hold", "reason": reason})
	if hold and doc.get("lifecycle_status") == DELETION_SCHEDULED:
		cancel_deletion(subscription, reason="deletion hold", actor=actor)
