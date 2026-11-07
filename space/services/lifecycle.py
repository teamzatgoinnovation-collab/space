"""Subscription lifecycle: 30-day trial, paid months, read-only states and the inactivity clock.

`Space Subscription.lifecycle_status` is the authoritative state of a customer's ERP site; `transition()` is the
only writer. Dates are stored in UTC (naive datetimes, labelled "(UTC)" on the form) and compared in UTC, so the
site's display timezone never changes when a trial ends.

Everything with decisions in it is a pure function over plain values (`remaining_days`, `inactive_days`,
`inactivity_decision`, `is_paid`, `next_paid_until`, `state_hash`) so the rules are tested without a database; the
functions that touch documents are thin around them.

Deleting sites is not done here: the scheduler (space/jobs/lifecycle.py) decides, space_cloud performs.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import frappe
from frappe.utils import get_datetime

TRIAL, ACTIVE, EXPIRED, SUSPENDED = "TRIAL", "ACTIVE", "EXPIRED", "SUSPENDED"
PENDING_PAYMENT, DELETION_SCHEDULED, DELETED = "PENDING_PAYMENT", "DELETION_SCHEDULED", "DELETED"
STATUSES = (TRIAL, ACTIVE, EXPIRED, SUSPENDED, PENDING_PAYMENT, DELETION_SCHEDULED, DELETED)

READ_ONLY = {EXPIRED, SUSPENDED, PENDING_PAYMENT, DELETION_SCHEDULED, DELETED}
RESTORABLE = {TRIAL, ACTIVE, EXPIRED, SUSPENDED, PENDING_PAYMENT}

# DELETED is terminal and reachable from every state: a customer or an admin can delete a site at any time, and the
# subscription must then say so (a deleted site must never keep showing as ACTIVE or accept payments).
ALLOWED: dict[str, set[str]] = {
	TRIAL: {ACTIVE, EXPIRED, SUSPENDED, DELETION_SCHEDULED, DELETED},
	ACTIVE: {PENDING_PAYMENT, SUSPENDED, DELETION_SCHEDULED, EXPIRED, TRIAL, DELETED},  # TRIAL: free -> paid plan
	EXPIRED: {ACTIVE, TRIAL, SUSPENDED, DELETION_SCHEDULED, DELETED},
	PENDING_PAYMENT: {ACTIVE, EXPIRED, SUSPENDED, DELETION_SCHEDULED, TRIAL, DELETED},
	SUSPENDED: {ACTIVE, TRIAL, EXPIRED, PENDING_PAYMENT, DELETION_SCHEDULED, DELETED},
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

LIVE_SITE_STATUSES = ("Active", "Suspended")

# Billing period an admin sets per site. One verified payment buys one period.
MONTHLY, YEARLY = "Monthly", "Yearly"
PERIOD_MONTHS = {MONTHLY: 1, YEARLY: 12}
PUSH_HEARTBEAT = timedelta(hours=24)


# --- pure rules --------------------------------------------------------------------------------------------------

def commit() -> None:
	"""Commit between units of work in jobs; a no-op under the test runner, whose per-test rollback must be able to
	undo everything (a commit in a test leaks its fixtures into the shared site)."""
	if not (getattr(frappe, "in_test", False) or frappe.flags.get("in_test")):
		frappe.db.commit()


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
	"""A verified payment buys `months` on top of whatever is left (never from the past). A month is 30 days; every
	full 12 months is a 365-day year, so a yearly payment lasts a real year."""
	now = now or utcnow()
	base = as_utc(current_paid_until)
	start = base if base and base > now else now
	months = max(1, int(months))
	return start + timedelta(days=365 * (months // 12) + 30 * (months % 12))


def period_of(value: Any) -> str:
	"""The billing period of a subscription row / value; anything unknown is Monthly."""
	period = value.get("billing_period") if hasattr(value, "get") else value
	return period if period in PERIOD_MONTHS else MONTHLY


def plan_prices(plan: str | None) -> tuple[float, float]:
	"""(monthly_price, yearly_price) of a plan."""
	if not plan:
		return 0.0, 0.0
	row = frappe.db.get_value("Space Plan", plan, ["monthly_price", "yearly_price"], as_dict=True) or {}
	return float(row.get("monthly_price") or 0), float(row.get("yearly_price") or 0)


def state_hash(payload: dict[str, Any]) -> str:
	"""Identity of what a tenant has been told: everything except the push timestamp."""
	body = {k: v for k, v in payload.items() if k != "synced_at"}
	return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def inactivity_decision(
	*,
	inactive: float,
	warn_after: int,
	delete_after: int,
	status: str,
	warning: str,
	scheduled_at: Any,
	now: datetime,
	paid: bool,
	held: bool,
	excluded: bool,
	protected: bool,
	activity_after_warning: bool,
	manual: bool = False,
	auto_enabled: bool = True,
) -> str:
	"""What the inactivity clock says right now. `warning` is the state of this episode's warning email: "none"
	(not sent yet), "sent" (confirmed delivered), "pending" (queued, delivery not confirmed) or "stalled"
	(failed or skipped, being retried). One of:

	none          nothing to do
	dry_run_warn  would warn, but automatic deletion is off: only logged, the customer is not contacted
	warn          send (or re-send) the warning email (deletion is NOT scheduled yet)
	wait_warning  the warning is queued: wait for the Email Queue to confirm it was sent
	schedule      the warning was delivered: schedule the deletion
	cancel        a scheduled deletion must be called off (activity, payment, protection)
	wait          deletion is scheduled but not due yet
	delete        the scheduled date passed and every re-check still holds
	"""
	shield = held or excluded or protected
	if status == DELETION_SCHEDULED:
		due = as_utc(scheduled_at)
		if shield or due is None:
			return "cancel"
		if manual:
			# An admin's schedule does not depend on the inactivity clock or on being unpaid when it was made (a
			# payment AFTER it moves the subscription out of DELETION_SCHEDULED by itself); only the shields count.
			return "wait" if now < due else "delete"
		if paid or activity_after_warning or inactive < warn_after:
			return "cancel"
		if now < due or inactive < delete_after:
			return "wait"
		return "delete"
	if shield or paid or status in (ACTIVE, DELETED) or inactive < warn_after:
		return "none"
	if not auto_enabled:
		return "dry_run_warn"
	if warning in ("none", "stalled"):
		return "warn"  # send_once decides: first send, re-arm a Skipped / exhausted notice, or "duplicate" while retries run
	if warning == "sent":
		return "schedule"
	return "wait_warning"  # queued, delivery not confirmed yet


# --- settings ----------------------------------------------------------------------------------------------------

def settings() -> dict[str, Any]:
	out: dict[str, Any] = dict(DEFAULTS)
	out.update({"lifecycle_enforcement_enabled": 0, "auto_deletion_enabled": 0, "protected_domains": ""})
	try:
		doc = frappe.get_single("Space Settings")
		for key in out:
			value = doc.get(key)
			if value not in (None, ""):  # a configured 0 is a value (payment grace 0 = no grace), not "unset"
				out[key] = value
	except Exception:
		frappe.log_error(title="Space lifecycle: settings unreadable, using defaults")
	for key, default in DEFAULTS.items():
		try:
			out[key] = int(out[key])
		except (TypeError, ValueError):
			out[key] = default
	out["trial_days_default"] = max(1, min(out["trial_days_default"], 365))
	out["payment_grace_days"] = max(0, min(out["payment_grace_days"], 30))
	out["inactivity_warning_days"] = max(1, min(out["inactivity_warning_days"], 365))
	out["inactivity_delete_days"] = max(out["inactivity_warning_days"] + 1, min(out["inactivity_delete_days"], 730))
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


def event_by_key(dedupe_key: str) -> Any:
	name = frappe.db.get_value("Space Lifecycle Event", {"dedupe_key": dedupe_key}, "name")
	return frappe.get_doc("Space Lifecycle Event", name) if name else None


def warning_cycle(subscription: str, last_activity: Any) -> str:
	"""Key of ONE inactivity episode: the last activity seen plus how many times a deletion was cancelled or a
	payment arrived. Either resets the episode, so a customer who was warned, then cancelled, then went quiet again
	is warned again (a key that only counted activity would never re-arm)."""
	last = as_utc(last_activity)
	epoch = frappe.db.count(
		"Space Lifecycle Event", {"subscription": subscription, "kind": ("in", ("deletion_cancelled", "payment"))}
	)
	return f"{last.isoformat() if last else 'never'}|{epoch}"


# --- subscription access -----------------------------------------------------------------------------------------

def subscription_for_site(site: str) -> str | None:
	"""The subscription a site is linked to -- and nothing else. (An earlier version fell back to the customer's
	first subscription, which could suspend, charge or delete the WRONG site.)"""
	return frappe.db.get_value("Space Site", site, "subscription") or None


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
	same save (dates, hold flags ...) and win over the resets below."""
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
		# Leaving a scheduled deletion, by ANY route (payment, activity, hold, admin): the whole episode is over.
		# A flag left behind (deletion_manual) would make the next automatic cycle skip its activity re-check.
		doc.deletion_scheduled_at = None
		doc.inactivity_warning_sent_at = None
		doc.pre_deletion_status = None
		doc.deletion_manual = 0

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
		"billing_period": period_of(doc),
		"grace_days": s["payment_grace_days"],
		"site_url": f"https://{domain}" if domain else None,
		"portal_url": portal,
		"manage_url": _manage_url(portal, site),
		"synced_at": datetime.now(timezone.utc).isoformat(),
	}


def _manage_url(portal: str | None, site: str | None) -> str | None:
	"""Where the customer changes plan / pays: their site's page in the customer portal (the tenant only shows
	information and links here; plan changes happen in the portal)."""
	if not portal or not site:
		return portal
	slug = frappe.db.get_value("Space Site", site, "site_name") or site
	return f"{portal}/sites/{slug}"


def _iso(value: Any) -> str | None:
	dt = as_utc(value)
	return dt.replace(tzinfo=timezone.utc).isoformat() if dt else None


def push_state(subscription: str) -> bool:
	"""Tell the tenant its state (no-op without space_cloud or a live site). Never raises: a failed push is logged
	and left for `reconcile_pushes`, which re-sends whatever the tenant has not been told yet. On success the hash of
	what was sent is stored, so the reconcile can tell "up to date" from "never arrived"."""
	site = site_of(subscription)
	if not site:
		return False
	try:
		pusher = frappe.get_attr("space_cloud.services.tenant_sync.push_state")
	except Exception:
		return False
	try:
		payload = state_payload(subscription)
		pushed = bool(pusher(site, payload))
	except Exception:
		frappe.log_error(title=f"Space lifecycle: push to {site} failed")
		return False
	if pushed:
		frappe.db.set_value(
			"Space Subscription", subscription,
			{"tenant_state_hash": state_hash(payload), "tenant_state_pushed_at": utcnow()},
			update_modified=False,
		)
	return pushed


def reconcile_pushes(now: datetime | None = None, *, max_pushes: int = 25, deadline: float | None = None) -> dict[str, int]:
	"""Hourly: make sure every managed tenant has been told its CURRENT state. A tenant is re-pushed when what it
	was last told differs from the desired state (a transition whose push failed, a payment, the enforcement switch or
	the grace setting changed in Space Settings) and, as a heartbeat, once a day. Bounded by a count and a deadline."""
	now = now or utcnow()
	counts = {"pushed": 0, "failed": 0, "unchanged": 0}
	rows = frappe.get_all(
		"Space Subscription",
		filters={"lifecycle_status": ("in", [s for s in STATUSES if s != DELETED])},
		fields=["name", "tenant_state_hash", "tenant_state_pushed_at"],
		order_by="tenant_state_pushed_at asc",
	)
	for row in rows:
		if counts["pushed"] + counts["failed"] >= max_pushes or (deadline is not None and time.monotonic() > deadline):
			break
		site = site_of(row.name)
		if not site or frappe.db.get_value("Space Site", site, "status") not in LIVE_SITE_STATUSES:
			continue
		pushed_at = as_utc(row.tenant_state_pushed_at)
		stale = pushed_at is None or now - pushed_at > PUSH_HEARTBEAT
		if state_hash(state_payload(row.name)) == row.tenant_state_hash and not stale:
			counts["unchanged"] += 1
			continue
		if push_state(row.name):
			counts["pushed"] += 1
		else:
			counts["failed"] += 1
		commit()
	for key, value in reconcile_protected(max_pushes=max(0, max_pushes - counts["pushed"] - counts["failed"])).items():
		counts[key] += value
	return counts


# --- operations ------------------------------------------------------------------------------------------------

def is_free_plan(plan: str | None) -> bool:
	"""A plan that costs nothing has no trial to run out and nothing to pay: it is ACTIVE and never restricted."""
	return bool(plan) and not float(frappe.db.get_value("Space Plan", plan, "monthly_price") or 0)


def start_trial(subscription: str, *, started_at: datetime | None = None, force: bool = False) -> bool:
	"""Start the free trial when provisioning succeeds. Idempotent: a second call does nothing."""
	lock(subscription)
	doc = frappe.get_doc("Space Subscription", subscription)
	if doc.get("trial_started_at") and not force:
		return False
	started = started_at or utcnow()
	plan = doc.get("plan")
	if is_free_plan(plan):
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
	status = doc.get("lifecycle_status")
	if status == DELETED:
		frappe.throw("This site was deleted")
	if status == SUSPENDED:
		frappe.throw("This site is suspended: reactivate it before extending its trial")
	base = max(as_utc(doc.get("trial_ends_at")) or utcnow(), utcnow())
	end = base + timedelta(days=days)
	if status in (EXPIRED, PENDING_PAYMENT, DELETION_SCHEDULED):
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
	"""A verified payment: one billing period (a month, or a year for a Yearly site -- set by an admin, never by the
	caller, so a client cannot under- or over-credit) more of ACTIVE. Idempotent on (provider, payment_id) through
	the event table's unique key, so a retried request or a double click cannot add a second period.

	A deleted site refuses the payment BEFORE anything is recorded (a charge with no Space record is the worst
	outcome); a SUSPENDED subscription (only an admin suspends) keeps its suspension: the months are credited, the
	site stays suspended until an admin reactivates it."""
	lock(subscription)
	doc = frappe.get_doc("Space Subscription", subscription)
	status = doc.get("lifecycle_status") or TRIAL
	if status == DELETED:
		frappe.throw("This site was deleted")
	key = f"payment:{provider}:{payment_id}"
	first = record_event(
		"payment", subscription=subscription, site=site_of(subscription), dedupe_key=key, actor=actor,
		details={"provider": provider, "payment_id": payment_id, "amount": amount, "currency": currency},
	)
	if first is None:
		return {"applied": False, "paid_until": _iso(doc.get("paid_until"))}
	months = PERIOD_MONTHS[period_of(doc)]
	until = next_paid_until(doc.get("paid_until"), months=months)
	money = {"paid_until": until, "next_billing_date": until.date(), "renewal_date": until.date(), "end_date": until.date()}
	suspended = status == SUSPENDED
	if suspended:
		for field, value in money.items():
			doc.set(field, value)
		doc.save(ignore_permissions=True)
	else:
		transition(subscription, ACTIVE, reason=f"payment {provider}:{payment_id}", actor=actor, push=False, **money)
	if _payment_history_ready():
		frappe.get_doc({
			"doctype": "Space Payment History", "customer": doc.customer, "subscription": subscription,
			"amount": amount, "currency": currency, "status": "Succeeded", "method": provider, "reference": payment_id,
		}).insert(ignore_permissions=True)
	push_state(subscription)
	return {"applied": True, "paid_until": _iso(until), "suspended": suspended, "months": months}


def _payment_history_ready() -> bool:
	try:
		meta = frappe.get_meta("Space Payment History")
		return all(meta.has_field(f) for f in ("customer", "amount", "status", "reference"))
	except Exception:
		return False


def on_plan_changed(subscription: str, old_plan: str | None, new_plan: str | None) -> None:
	"""A plan change must not leave the lifecycle behind: free -> paid starts a fresh trial on the paid plan (it was
	ACTIVE with nothing to pay and would otherwise never be charged or restricted); paid -> free ends the clock."""
	doc = frappe.get_doc("Space Subscription", subscription)
	status = doc.get("lifecycle_status")
	if not status or status in (DELETED, DELETION_SCHEDULED, SUSPENDED):
		return
	was_free, now_free = is_free_plan(old_plan), is_free_plan(new_plan)
	if was_free and not now_free and status == ACTIVE and not is_paid(doc.get("paid_until")):
		started = utcnow()
		days = settings()["trial_days_default"]
		transition(subscription, TRIAL, reason=f"plan changed {old_plan} -> {new_plan}: fresh trial on the paid plan",
			trial_started_at=started, trial_ends_at=started + timedelta(days=days), trial_ends_on=(started + timedelta(days=days)).date())
	elif not was_free and now_free and status in (TRIAL, EXPIRED, PENDING_PAYMENT):
		transition(subscription, ACTIVE, reason=f"plan changed {old_plan} -> {new_plan}: nothing to pay")


def backfill_missing(now: datetime | None = None) -> dict[str, int]:
	"""Give every existing subscription without a lifecycle state one: a fresh 30-day trial that starts now (an
	ACTIVE state for a plan that costs nothing, or for a legacy subscription that is paid up to a future end date).
	Idempotent -- a subscription that has a state is left alone.

	Skipped on purpose: subscriptions with no live site (deleted, failed, still provisioning: provisioning starts
	its own trial) and any subscription that has even ONE protected site -- the real company sites that Space
	merely tracks (Space Settings > Protected Domains) must never get a trial clock. Pushes nothing to tenants:
	enforcement is a separate, explicit switch."""
	now = now or utcnow()
	protected = protected_domains()
	counts = {"trial": 0, "active_free": 0, "active_paid": 0, "skipped_protected": 0, "skipped_not_live": 0}
	pending = frappe.get_all(
		"Space Subscription", filters={"lifecycle_status": ("in", ("", None))},
		fields=["name", "plan", "payment_status", "end_date"],
	)
	for sub in pending:
		sites = frappe.get_all("Space Site", filters={"subscription": sub.name}, fields=["name", "domain", "status"])
		if any((s.domain or "").lower() in protected or (s.name or "").lower() in protected for s in sites):
			counts["skipped_protected"] += 1
			continue
		if not any(s.status in LIVE_SITE_STATUSES for s in sites):
			counts["skipped_not_live"] += 1
			continue
		price = float(frappe.db.get_value("Space Plan", sub.plan, "monthly_price") or 0) if sub.plan else 0
		legacy_end = get_datetime(sub.end_date).date() if sub.end_date else None
		fields: dict[str, Any] = {"trial_started_at": now}
		if sub.payment_status == "Paid" and price and legacy_end and legacy_end > now.date():
			# A customer who really paid keeps what they paid for: ACTIVE until the legacy end date.
			until = datetime.combine(legacy_end, datetime.min.time())
			fields.update(paid_until=until, next_billing_date=legacy_end)
			state, bucket = ACTIVE, "active_paid"
		elif price:
			days = settings()["trial_days_default"]
			fields.update(trial_ends_at=now + timedelta(days=days), trial_ends_on=(now + timedelta(days=days)).date())
			state, bucket = TRIAL, "trial"
		else:
			state, bucket = ACTIVE, "active_free"
		frappe.db.set_value("Space Subscription", sub.name, fields)
		transition(sub.name, state, reason="rollout backfill", push=False)
		counts[bucket] += 1
		record_event("trial_started", subscription=sub.name, site=sites[0].name if sites else None,
			dedupe_key=f"backfill:{sub.name}", details={"note": "rollout backfill", "state": state})
	return counts


def view(subscription: str, now: datetime | None = None) -> dict[str, Any]:
	"""One customer's picture of a site (dashboard + admin card)."""
	now = now or utcnow()
	doc = frappe.get_doc("Space Subscription", subscription)
	site = site_of(subscription)
	site_doc = frappe.db.get_value("Space Site", site, ["domain", "status", "plan"], as_dict=True) if site else None
	monthly, yearly = plan_prices(doc.plan)
	period = period_of(doc)
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
		"monthly_price": monthly,
		"yearly_price": yearly,
		"billing_period": period,
		"period_price": yearly if period == YEARLY else monthly,
		"protected": False,
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


# --- protected sites (display only) ----------------------------------------------------------------------------

def is_protected_site(site: str) -> bool:
	"""A real company site that Space merely tracks (Space Settings > Protected Domains)."""
	domain = (frappe.db.get_value("Space Site", site, "domain") or "").lower()
	return bool(domain and domain in protected_domains()) or site.lower() in protected_domains()


def protected_view(site: str) -> dict[str, Any]:
	"""What a protected site shows: a neutral ACTIVE badge. Display only -- no state is written, no trial clock
	starts, nothing is ever restricted or deleted (the shields in the delete job are unchanged)."""
	row = frappe.db.get_value("Space Site", site, ["domain", "status", "plan", "subscription"], as_dict=True) or {}
	sub = frappe.get_doc("Space Subscription", row.subscription) if row.get("subscription") else None
	plan = (sub.plan if sub else None) or row.get("plan")
	monthly, yearly = plan_prices(plan)
	period = period_of(sub) if sub else MONTHLY
	return {
		"subscription": row.get("subscription"), "site": site,
		"site_url": f"https://{row.domain}" if row.get("domain") else None, "site_status": row.get("status"),
		"lifecycle_status": ACTIVE, "plan": plan, "monthly_price": monthly, "yearly_price": yearly,
		"billing_period": period, "period_price": yearly if period == YEARLY else monthly, "protected": True,
		"trial_started_at": None, "trial_ends_at": None, "remaining_days": None, "paid_until": None,
		"next_billing_date": None, "last_activity_at": None, "inactive_days": 0, "deletion_scheduled_at": None,
		"deletion_hold": False, "read_only": False,
	}


def reconcile_protected(*, max_pushes: int = 10, now: datetime | None = None) -> dict[str, int]:
	"""Tell protected tenants (no lifecycle state of their own) their informational ACTIVE badge, at most once a
	day each. Enforcement is always 0 for them (tenant_sync forces it), so this can never restrict anything."""
	counts = {"pushed": 0, "failed": 0, "unchanged": 0}
	own = (frappe.local.site or "").lower()
	try:
		pusher = frappe.get_attr("space_cloud.services.tenant_sync.push_state")
	except Exception:
		return counts
	for site in frappe.get_all("Space Site", filters={"status": ("in", LIVE_SITE_STATUSES)}, fields=["name", "domain", "subscription"]):
		if (site.domain or "").lower() == own or not is_protected_site(site.name):
			continue
		if site.subscription and frappe.db.get_value("Space Subscription", site.subscription, "lifecycle_status"):
			continue
		if counts["pushed"] + counts["failed"] >= max_pushes:
			break
		view = protected_view(site.name)
		payload = {
			"enforcement_on": 0, "status": ACTIVE, "trial_ends_at": None, "paid_until": None,
			"billing_period": view["billing_period"], "grace_days": settings()["payment_grace_days"],
			"site_url": view["site_url"], "synced_at": datetime.now(timezone.utc).isoformat(),
			"portal_url": f"https://space.{(site.domain or '').split('.', 1)[1]}" if "." in (site.domain or "") else None,
		}
		payload["manage_url"] = _manage_url(payload["portal_url"], site.name)
		key = f"space_protected_push:{site.name}"
		digest = state_hash(payload)
		if frappe.cache().get_value(key) == digest:
			counts["unchanged"] += 1
			continue
		try:
			if pusher(site.name, payload):
				frappe.cache().set_value(key, digest, expires_in_sec=86400)
				counts["pushed"] += 1
			else:
				counts["failed"] += 1
		except Exception:
			frappe.log_error(title=f"Space lifecycle: protected push to {site.name} failed")
			counts["failed"] += 1
	return counts


# --- admin operations ------------------------------------------------------------------------------------------

def _refuse_deleted(subscription: str) -> None:
	if frappe.db.get_value("Space Subscription", subscription, "lifecycle_status") == DELETED:
		frappe.throw("This site was deleted")


def admin_activate(subscription: str, months: int, *, reason: str, actor: str | None = None) -> None:
	"""Admin grants paid months without a payment (comp, offline payment). Audited with the reason."""
	months = int(months)
	if months < 1 or months > 24:
		frappe.throw("Grant 1 to 24 months")
	lock(subscription)
	_refuse_deleted(subscription)
	until = next_paid_until(frappe.db.get_value("Space Subscription", subscription, "paid_until"), months=months)
	transition(subscription, ACTIVE, reason=f"admin activation: {reason}", actor=actor, push=False,
		paid_until=until, next_billing_date=until.date(), renewal_date=until.date(), end_date=until.date())
	record_event("admin_action", subscription=subscription, site=site_of(subscription), actor=actor,
		details={"action": "activate", "months": months, "reason": reason, "paid_until": str(until)})
	push_state(subscription)


def set_billing_period(subscription: str, period: str, *, reason: str, actor: str | None = None) -> str:
	"""Admin chooses Monthly or Yearly billing for a site. It applies from the NEXT payment: what was already paid
	for is untouched. Yearly needs a yearly price on the plan; a free plan has nothing to bill."""
	if period not in PERIOD_MONTHS:
		frappe.throw("Billing period must be Monthly or Yearly")
	lock(subscription)
	_refuse_deleted(subscription)
	doc = frappe.get_doc("Space Subscription", subscription)
	monthly, yearly = plan_prices(doc.get("plan"))
	if not monthly and not yearly:
		frappe.throw("This plan is free: there is nothing to bill")
	if period == YEARLY and yearly <= 0:
		frappe.throw("Set a yearly price on this plan first")
	old = period_of(doc)
	if old == period:
		return period
	doc.billing_period = period
	doc.flags.ignore_validate_update_after_submit = True
	doc.save(ignore_permissions=True)
	record_event("admin_action", subscription=subscription, site=site_of(subscription), actor=actor,
		details={"action": "set_billing_period", "from": old, "to": period, "reason": reason})
	push_state(subscription)
	return period


def admin_suspend(subscription: str, *, reason: str, actor: str | None = None) -> None:
	_refuse_deleted(subscription)
	transition(subscription, SUSPENDED, reason=f"admin suspension: {reason}", actor=actor)
	record_event("admin_action", subscription=subscription, site=site_of(subscription), actor=actor,
		details={"action": "suspend", "reason": reason})


def admin_reactivate(subscription: str, *, reason: str, actor: str | None = None) -> str:
	"""Back from SUSPENDED / EXPIRED / PENDING_PAYMENT to what the dates say: ACTIVE if paid (or free), TRIAL if the
	trial still runs, otherwise EXPIRED (read-only until someone pays or extends)."""
	_refuse_deleted(subscription)
	doc = frappe.get_doc("Space Subscription", subscription)
	if is_paid(doc.get("paid_until")) or is_free_plan(doc.get("plan")):
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
		actor=actor)
	record_event("deletion_cancelled", subscription=subscription, site=site_of(subscription), actor=actor,
		details={"reason": reason, "by": "admin"})
	return target


def set_hold(subscription: str, hold: bool, *, reason: str, actor: str | None = None) -> None:
	lock(subscription)
	_refuse_deleted(subscription)
	doc = frappe.get_doc("Space Subscription", subscription)
	doc.deletion_hold = 1 if hold else 0
	doc.deletion_hold_reason = reason if hold else None
	doc.save(ignore_permissions=True)
	record_event("admin_action", subscription=subscription, site=site_of(subscription), actor=actor,
		details={"action": "hold" if hold else "release_hold", "reason": reason})
	if hold and doc.get("lifecycle_status") == DELETION_SCHEDULED:
		cancel_deletion(subscription, reason="deletion hold", actor=actor)


# --- ownership + scheduled-deletion validity (shared by the scheduler and the delete job) --------------------------

class DeletionRefused(frappe.ValidationError):
	"""A scheduled deletion must not run (a re-check failed). Not an error: the site stays. `wait` means "not yet"
	(the warning is not confirmed delivered) rather than "cancel"."""

	wait = False


def assert_scheduled_deletion_valid(site_doc: Any, source: str, expected_scheduled_at: Any) -> None:
	"""The full re-check that must hold at the instant a scheduled deletion runs -- not only when it was queued:
	still scheduled for the SAME date; no hold, exclusion or protected domain; (automatic) still not paid; the
	customer who was warned is still the site's customer; the warning email was confirmed delivered."""
	sub = site_doc.get("subscription")
	if not sub:
		raise DeletionRefused("no subscription: refusing a scheduled deletion")
	row = frappe.db.get_value(
		"Space Subscription", sub,
		["lifecycle_status", "deletion_hold", "paid_until", "deletion_scheduled_at", "deletion_manual", "customer"],
		as_dict=True,
	)
	domain = (site_doc.get("domain") or "").lower()
	if domain in protected_domains():
		raise DeletionRefused(f"{domain} is protected")
	if row.lifecycle_status != DELETION_SCHEDULED:
		raise DeletionRefused("the subscription is no longer scheduled for deletion")
	scheduled = as_utc(row.deletion_scheduled_at)
	expected = as_utc(expected_scheduled_at)
	if expected is not None and (scheduled is None or abs((scheduled - expected).total_seconds()) > 1):
		raise DeletionRefused("the deletion was rescheduled")
	if row.deletion_hold or is_excluded(site_doc.name) or is_excluded(domain):
		raise DeletionRefused("the site is held or excluded from deletion")
	if source == "inactivity" and is_paid(row.paid_until):
		raise DeletionRefused("the subscription is paid")
	scheduled_event = event_by_key(f"deletion_scheduled:{sub}:{scheduled.isoformat()}") if scheduled else None
	info = json.loads(scheduled_event.details or "{}") if scheduled_event else {}
	warned_customer = info.get("customer")
	if not warned_customer or warned_customer != site_doc.get("customer") or warned_customer != row.customer:
		raise DeletionRefused("the customer changed since the warning: they were not warned")
	warning = frappe.db.get_value("Space Lifecycle Event", info.get("warning_event") or "", "status") if info.get("warning_event") else None
	if warning != "Sent":
		exc = DeletionRefused("the warning email was not confirmed delivered")
		exc.wait = True
		raise exc
