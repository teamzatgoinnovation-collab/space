"""Lifecycle API: what a customer sees of their sites' trial / subscription, and the admin controls.

Thin layer over space/services/lifecycle.py. Customers may only see their own sites; every write (admin or the
portal's payment recording) needs Space Admin / System Manager and leaves an audit row with the actor.
"""

from __future__ import annotations

from datetime import timedelta

import frappe

from space.api.v1.response import customer_for_user, fail, ok, require_roles
from space.services import audit
from space.services import lifecycle as lc
from space.services import lifecycle_mail as mail

ADMIN_ROLES = ("Space Admin", "System Manager")


def _is_admin() -> bool:
	return frappe.session.user == "Administrator" or bool(set(frappe.get_roles()) & {"Space Admin", "System Manager"})


def _site_subscription(site: str) -> str:
	if not frappe.db.exists("Space Site", site):
		frappe.throw("Site not found", frappe.DoesNotExistError)
	sub = lc.subscription_for_site(site)
	if not sub:
		frappe.throw("This site has no subscription")
	return sub


def _ensure_access(site: str) -> str:
	sub = _site_subscription(site)
	if _is_admin():
		return sub
	owner = frappe.db.get_value("Space Site", site, "customer")
	if not owner or owner != customer_for_user():
		audit.log_audit("lifecycle_access_denied", result="Denied", ref_doctype="Space Site", ref_name=site)
		frappe.throw("Not permitted", frappe.PermissionError)
	return sub


def _admin(action: str, site: str, **details) -> str:
	require_roles(*ADMIN_ROLES)
	sub = _site_subscription(site)
	audit.log_audit(action, ref_doctype="Space Site", ref_name=site, details=str(details))
	return sub


# --- customer ----------------------------------------------------------------------------------------------------

def _own_site_or_admin(site: str) -> None:
	if not frappe.db.exists("Space Site", site):
		frappe.throw("Site not found", frappe.DoesNotExistError)
	if _is_admin():
		return
	owner = frappe.db.get_value("Space Site", site, "customer")
	if not owner or owner != customer_for_user():
		audit.log_audit("lifecycle_access_denied", result="Denied", ref_doctype="Space Site", ref_name=site)
		frappe.throw("Not permitted", frappe.PermissionError)


@frappe.whitelist()
def get_lifecycle(site: str):
	_own_site_or_admin(site)
	sub = lc.subscription_for_site(site)
	if sub and frappe.db.get_value("Space Subscription", sub, "lifecycle_status"):
		return ok(lc.view(sub))
	if lc.is_protected_site(site):
		return ok(lc.protected_view(site))  # a real company site: neutral ACTIVE badge, no clock
	if not sub:
		frappe.throw("This site has no subscription")
	return ok(lc.view(sub))


@frappe.whitelist()
def list_lifecycle():
	"""One call for the dashboard: the lifecycle picture of every site the caller owns (all, for an admin). Protected
	company sites are included with a neutral ACTIVE badge, so every site shows one."""
	filters = {} if _is_admin() else {"customer": customer_for_user() or "__none__"}
	out = {}
	for site in frappe.get_all("Space Site", filters=filters, pluck="name"):
		sub = lc.subscription_for_site(site)
		if sub and frappe.db.get_value("Space Subscription", sub, "lifecycle_status"):
			out[site] = lc.view(sub)
		elif lc.is_protected_site(site):
			out[site] = lc.protected_view(site)
	return ok(out)


# --- payments (the portal verifies the provider, then records here) ------------------------------------------------

@frappe.whitelist()
def record_payment(site: str, provider: str, payment_id: str, amount: float, currency: str, months: int = 1):
	"""Called by the portal's server with its service credential after IT verified the payment with the provider.
	Idempotent on (provider, payment_id)."""
	require_roles(*ADMIN_ROLES)
	if not (provider and payment_id):
		return fail("provider and payment_id are required", "INVALID_PAYMENT")
	sub = _site_subscription(site)
	result = lc.record_payment(sub, provider=provider, payment_id=str(payment_id), amount=float(amount),
		currency=currency, months=int(months), actor=frappe.session.user)
	return ok(result)


# --- admin -------------------------------------------------------------------------------------------------------

@frappe.whitelist()
def extend_trial(site: str, days: int, reason: str):
	sub = _admin("lifecycle_extend_trial", site, days=days, reason=reason)
	lc.extend_trial(sub, int(days), reason=reason, actor=frappe.session.user)
	return ok(lc.view(sub))


@frappe.whitelist()
def activate(site: str, months: int, reason: str):
	sub = _admin("lifecycle_activate", site, months=months, reason=reason)
	lc.admin_activate(sub, int(months), reason=reason, actor=frappe.session.user)
	return ok(lc.view(sub))


@frappe.whitelist()
def set_billing_period(site: str, period: str, reason: str):
	"""Admin: bill this site Monthly or Yearly (applies from the next payment)."""
	sub = _admin("lifecycle_set_billing_period", site, period=period, reason=reason)
	lc.set_billing_period(sub, period, reason=reason, actor=frappe.session.user)
	return ok(lc.view(sub))


@frappe.whitelist()
def suspend(site: str, reason: str):
	sub = _admin("lifecycle_suspend", site, reason=reason)
	lc.admin_suspend(sub, reason=reason, actor=frappe.session.user)
	return ok(lc.view(sub))


@frappe.whitelist()
def reactivate(site: str, reason: str):
	sub = _admin("lifecycle_reactivate", site, reason=reason)
	lc.admin_reactivate(sub, reason=reason, actor=frappe.session.user)
	return ok(lc.view(sub))


@frappe.whitelist()
def cancel_deletion(site: str, reason: str):
	sub = _admin("lifecycle_cancel_deletion", site, reason=reason)
	lc.cancel_deletion(sub, reason=reason, actor=frappe.session.user)
	return ok(lc.view(sub))


@frappe.whitelist()
def set_hold(site: str, hold: int, reason: str):
	sub = _admin("lifecycle_hold", site, hold=hold, reason=reason)
	lc.set_hold(sub, bool(int(hold)), reason=reason, actor=frappe.session.user)
	return ok(lc.view(sub))


@frappe.whitelist()
def schedule_deletion(site: str, days: int, confirm_site: str, reason: str):
	"""Admin schedules a deletion. The admin must type the site name; the customer is emailed first and the schedule
	is refused if that email could not be queued. It is an explicit admin decision: it runs at the due time even with
	"Delete inactive sites automatically" off, and does not depend on the site's activity -- but the same shields apply
	(hold, exclusion, protected domain, same customer, the notice confirmed delivered), and a payment made after the
	schedule cancels it."""
	sub = _admin("lifecycle_schedule_deletion", site, days=days, reason=reason)
	if confirm_site != site:
		return fail("Type the site name to confirm", "CONFIRMATION_MISMATCH")
	days = int(days)
	if days < 1 or days > 90:
		return fail("Schedule 1 to 90 days ahead", "INVALID_DAYS")
	if frappe.db.get_value("Space Subscription", sub, "lifecycle_status") in (None, "", lc.DELETED):
		return fail("This site has no active lifecycle state", "NOT_SCHEDULABLE")
	row = frappe.db.get_value("Space Site", site, ["domain", "customer", "status"], as_dict=True)
	if (row.domain or "").lower() in lc.protected_domains():
		return fail("This site is protected", "PROTECTED")
	when = (lc.utcnow() + timedelta(days=days)).replace(microsecond=0)
	email = frappe.db.get_value("Space Customer", row.customer, "email")
	notice_key = f"manual_deletion:{sub}:{when.isoformat()}"
	result = mail.send_once(
		kind="inactivity_warning_email", subscription=sub, site=site,
		dedupe_key=notice_key, recipient=email,
		subject=f"[ZatGo Space] {row.domain} is scheduled for deletion on {when:%Y-%m-%d}",
		heading="Your site is scheduled for deletion",
		body="An administrator has scheduled this site for deletion.",
		rows=[("Site name", site), ("Site URL", f"https://{row.domain}"), ("Scheduled deletion (UTC)", when.strftime("%Y-%m-%d %H:%M"))],
		advice="Download a backup from your ZatGo Space dashboard now: a deleted site cannot be recovered.",
	)
	if result not in ("Pending", "Sent"):
		return fail(f"The customer could not be emailed ({result}); nothing was scheduled", "EMAIL_NOT_SENT")
	notice = lc.event_by_key(notice_key)
	lc.transition(sub, lc.DELETION_SCHEDULED, reason=f"admin schedule: {reason}", actor=frappe.session.user,
		deletion_scheduled_at=when, deletion_manual=1, inactivity_warning_sent_at=lc.utcnow())
	lc.record_event("deletion_scheduled", subscription=sub, site=site, actor=frappe.session.user,
		dedupe_key=f"deletion_scheduled:{sub}:{when.isoformat()}",
		details={"manual": True, "at": str(when), "customer": row.customer, "recipient": email,
		         "warning_event": notice.name if notice else None, "reason": reason})
	return ok(lc.view(sub))


@frappe.whitelist()
def add_exclusion(site: str, reason: str):
	_admin("lifecycle_add_exclusion", site, reason=reason)
	if not frappe.db.exists("Space Deletion Exclusion", site):
		customer = frappe.db.get_value("Space Site", site, "customer")
		frappe.get_doc({"doctype": "Space Deletion Exclusion", "site": site, "customer": customer, "reason": reason}).insert(ignore_permissions=True)
	sub = lc.subscription_for_site(site)
	if sub and frappe.db.get_value("Space Subscription", sub, "lifecycle_status") == lc.DELETION_SCHEDULED:
		lc.cancel_deletion(sub, reason="site excluded from automatic deletion", actor=frappe.session.user)
	return ok({"site": site, "excluded": True})


@frappe.whitelist()
def remove_exclusion(site: str):
	_admin("lifecycle_remove_exclusion", site)
	if frappe.db.exists("Space Deletion Exclusion", site):
		frappe.delete_doc("Space Deletion Exclusion", site, ignore_permissions=True)
	return ok({"site": site, "excluded": False})


@frappe.whitelist()
def list_exclusions():
	require_roles(*ADMIN_ROLES)
	return ok(frappe.get_all("Space Deletion Exclusion", fields=["site", "customer", "reason", "owner", "creation"], order_by="creation desc"))


@frappe.whitelist()
def list_events(site: str | None = None, limit: int = 100):
	require_roles(*ADMIN_ROLES, "Space Operator")
	filters = {"site": site} if site else {}
	return ok(frappe.get_all(
		"Space Lifecycle Event", filters=filters, order_by="creation desc", limit_page_length=min(int(limit), 500),
		fields=["name", "creation", "kind", "status", "site", "subscription", "recipient", "actor", "retry_count", "details"],
	))
