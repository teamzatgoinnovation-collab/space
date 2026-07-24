"""Space API v2 — additive portal / ops surface. Does not replace v1."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, today

from space.api.v1.response import customer_for_user, fail, ok, require_roles
from space.jobs import monitoring as monitoring_jobs
from space.jobs.backup import delete_backup_record, enqueue_backup, enqueue_restore
from space.services import audit, deployment, rate_limit, server_pool
from space.services.deployment import sanitize_job_dict


def _rl():
	rate_limit.check_rate_limit("v2")


def _ensure_site_access(doc, write: bool = False):
	roles = set(frappe.get_roles())
	if frappe.session.user == "Administrator" or roles.intersection(
		{"System Manager", "Space Admin", "Space Operator", "Support Engineer", "Billing Manager"}
	):
		return
	cust = customer_for_user()
	if not cust or doc.customer != cust:
		audit.log_audit("site_access_denied", result="Denied", ref_doctype="Space Site", ref_name=doc.name)
		frappe.throw(_("Not permitted"), frappe.PermissionError)


@frappe.whitelist()
def portal_dashboard():
	"""Customer dashboard aggregate."""
	_rl()
	cust = customer_for_user()
	roles = set(frappe.get_roles())
	is_admin = frappe.session.user == "Administrator" or roles.intersection(
		{"System Manager", "Space Admin", "Space Operator"}
	)
	filters = {"status": ("!=", "Deleted")}
	if cust and not is_admin:
		filters["customer"] = cust

	sites = frappe.get_all(
		"Space Site",
		filters=filters,
		fields=["name", "site_name", "domain", "status", "plan", "storage_used_mb", "job", "ssl_status"],
		order_by="modified desc",
	)
	sub_filters = {}
	if cust and not is_admin:
		sub_filters["customer"] = cust
	subs = frappe.get_all(
		"Space Subscription",
		filters=sub_filters,
		fields=["name", "plan", "status", "payment_status", "start_date", "end_date", "renewal_date", "trial_ends_on"],
		order_by="modified desc",
		limit_page_length=20,
	)
	notif_filters = {"is_read": 0}
	if cust and not is_admin:
		notif_filters["customer"] = cust
	notifs = frappe.get_all(
		"Space Notification",
		filters=notif_filters,
		fields=["name", "title", "event_type", "message", "creation"],
		order_by="creation desc",
		limit_page_length=10,
	)
	usage = sum(float(s.storage_used_mb or 0) for s in sites)
	return ok(
		{
			"sites": sites,
			"subscriptions": subs,
			"notifications": notifs,
			"usage": {"storage_mb": usage, "site_count": len(sites)},
			"plan": subs[0] if subs else None,
		}
	)


@frappe.whitelist()
def admin_dashboard():
	_rl()
	require_roles("Space Admin", "Space Operator", "System Manager", "Readonly Auditor")
	return ok(monitoring_jobs.dashboard_summary())


@frappe.whitelist()
def list_jobs(site: str | None = None, limit: int = 50):
	_rl()
	filters = {}
	if site:
		doc = frappe.get_doc("Space Site", site)
		_ensure_site_access(doc)
		filters["site"] = site
	else:
		cust = customer_for_user()
		roles = set(frappe.get_roles())
		if cust and not roles.intersection({"System Manager", "Space Admin", "Space Operator", "Support Engineer"}):
			sites = frappe.get_all("Space Site", filters={"customer": cust}, pluck="name")
			filters["site"] = ("in", sites or ["__none__"])
	rows = frappe.get_all(
		"Space Deployment Job",
		filters=filters,
		fields=[
			"name",
			"site",
			"server",
			"job_type",
			"status",
			"progress",
			"estimated_minutes",
			"started_at",
			"finished_at",
			"can_retry",
			"can_cancel",
			"can_rollback",
		],
		order_by="creation desc",
		limit_page_length=min(int(limit or 50), 200),
	)
	return ok(rows)


@frappe.whitelist()
def get_job_detail(name: str):
	_rl()
	job = frappe.get_doc("Space Deployment Job", name)
	site = frappe.get_doc("Space Site", job.site)
	_ensure_site_access(site)
	return ok(sanitize_job_dict(job.as_dict()))


@frappe.whitelist()
def retry_job(name: str):
	_rl()
	job = frappe.get_doc("Space Deployment Job", name)
	site = frappe.get_doc("Space Site", job.site)
	_ensure_site_access(site, write=True)
	with audit.AuditTimer("retry_job", api_method="space.api.v2.space.retry_job", ref_doctype="Space Deployment Job", ref_name=name):
		return ok(job.retry())


@frappe.whitelist()
def cancel_job(name: str):
	_rl()
	job = frappe.get_doc("Space Deployment Job", name)
	site = frappe.get_doc("Space Site", job.site)
	_ensure_site_access(site, write=True)
	return ok(deployment.cancel_job(name))


@frappe.whitelist()
def list_backups(site: str | None = None):
	_rl()
	filters = {"status": ("!=", "Deleted")}
	if site:
		doc = frappe.get_doc("Space Site", site)
		_ensure_site_access(doc)
		filters["site"] = site
	else:
		cust = customer_for_user()
		roles = set(frappe.get_roles())
		if cust and not roles.intersection({"System Manager", "Space Admin", "Space Operator"}):
			sites = frappe.get_all("Space Site", filters={"customer": cust}, pluck="name")
			filters["site"] = ("in", sites or ["__none__"])
	rows = frappe.get_all(
		"Space Backup",
		filters=filters,
		fields=[
			"name",
			"site",
			"backup_type",
			"status",
			"file_size_mb",
			"started_at",
			"finished_at",
			"is_restore_point",
			"job",
		],
		order_by="creation desc",
		limit_page_length=100,
	)
	return ok(rows)


@frappe.whitelist()
def backup_now(site: str):
	_rl()
	doc = frappe.get_doc("Space Site", site)
	_ensure_site_access(doc, write=True)
	with audit.AuditTimer("backup_now", api_method="space.api.v2.space.backup_now", ref_doctype="Space Site", ref_name=site):
		return ok(enqueue_backup(site, backup_type="Manual"))


@frappe.whitelist()
def restore_backup(name: str):
	_rl()
	backup = frappe.get_doc("Space Backup", name)
	site = frappe.get_doc("Space Site", backup.site)
	_ensure_site_access(site, write=True)
	require_roles("Space Admin", "Space Operator", "System Manager", "Support Engineer")
	return ok(enqueue_restore(name))


@frappe.whitelist()
def delete_backup(name: str):
	_rl()
	backup = frappe.get_doc("Space Backup", name)
	site = frappe.get_doc("Space Site", backup.site)
	_ensure_site_access(site, write=True)
	return ok(delete_backup_record(name))


@frappe.whitelist()
def list_domains(site: str | None = None):
	_rl()
	filters = {}
	if site:
		doc = frappe.get_doc("Space Site", site)
		_ensure_site_access(doc)
		filters["site"] = site
	else:
		cust = customer_for_user()
		roles = set(frappe.get_roles())
		if cust and not roles.intersection({"System Manager", "Space Admin", "Space Operator"}):
			sites = frappe.get_all("Space Site", filters={"customer": cust}, pluck="name")
			filters["site"] = ("in", sites or ["__none__"])
	rows = frappe.get_all(
		"Space Domain",
		filters=filters,
		fields=["name", "site", "domain", "is_primary", "ssl_status", "dns_status", "verified_at"],
		order_by="modified desc",
	)
	return ok(rows)


@frappe.whitelist()
def attach_domain(site: str, domain: str, primary: int = 0):
	_rl()
	doc = frappe.get_doc("Space Site", site)
	_ensure_site_access(doc, write=True)
	domain = (domain or "").strip().lower()
	if frappe.db.exists("Space Domain", domain):
		return fail("Domain already exists", "EXISTS")
	d = frappe.get_doc(
		{
			"doctype": "Space Domain",
			"site": site,
			"domain": domain,
			"is_primary": 1 if int(primary or 0) else 0,
			"dns_status": "Pending",
			"ssl_status": "Pending",
		}
	).insert(ignore_permissions=True)
	d.attach()
	return ok(d.as_dict())


@frappe.whitelist()
def verify_domain(name: str):
	_rl()
	d = frappe.get_doc("Space Domain", name)
	site = frappe.get_doc("Space Site", d.site)
	_ensure_site_access(site, write=True)
	return ok(d.verify())


@frappe.whitelist()
def detach_domain(name: str):
	_rl()
	d = frappe.get_doc("Space Domain", name)
	site = frappe.get_doc("Space Site", d.site)
	_ensure_site_access(site, write=True)
	return ok(d.detach())


@frappe.whitelist()
def list_invoices():
	_rl()
	filters = {}
	cust = customer_for_user()
	roles = set(frappe.get_roles())
	if cust and not roles.intersection({"System Manager", "Space Admin", "Billing Manager"}):
		filters["customer"] = cust
	rows = frappe.get_all(
		"Space Invoice",
		filters=filters,
		fields=[
			"name",
			"customer",
			"subscription",
			"status",
			"payment_status",
			"period_start",
			"period_end",
			"due_date",
			"amount",
			"currency",
		],
		order_by="creation desc",
		limit_page_length=100,
	)
	return ok(rows)


@frappe.whitelist()
def list_usage(site: str | None = None):
	_rl()
	filters = {}
	if site:
		doc = frappe.get_doc("Space Site", site)
		_ensure_site_access(doc)
		filters["site"] = site
	else:
		cust = customer_for_user()
		roles = set(frappe.get_roles())
		if cust and not roles.intersection({"System Manager", "Space Admin", "Billing Manager"}):
			filters["customer"] = cust
	rows = frappe.get_all(
		"Space Usage",
		filters=filters,
		fields=[
			"name",
			"site",
			"customer",
			"period_start",
			"period_end",
			"storage_mb",
			"database_mb",
			"cpu_hours",
			"ram_mb_avg",
		],
		order_by="period_end desc",
		limit_page_length=50,
	)
	return ok(rows)


@frappe.whitelist()
def list_payment_history():
	_rl()
	filters = {}
	cust = customer_for_user()
	roles = set(frappe.get_roles())
	if cust and not roles.intersection({"System Manager", "Space Admin", "Billing Manager"}):
		filters["customer"] = cust
	rows = frappe.get_all(
		"Space Payment History",
		filters=filters,
		fields=["name", "customer", "invoice", "amount", "currency", "status", "method", "paid_on", "reference"],
		order_by="creation desc",
		limit_page_length=100,
	)
	return ok(rows)


@frappe.whitelist()
def list_notifications(unread_only: int = 0):
	_rl()
	filters = {}
	if int(unread_only or 0):
		filters["is_read"] = 0
	cust = customer_for_user()
	roles = set(frappe.get_roles())
	if cust and not roles.intersection({"System Manager", "Space Admin", "Space Operator"}):
		filters["customer"] = cust
	rows = frappe.get_all(
		"Space Notification",
		filters=filters,
		fields=["name", "title", "event_type", "message", "is_read", "creation", "ref_doctype", "ref_name"],
		order_by="creation desc",
		limit_page_length=50,
	)
	return ok(rows)


@frappe.whitelist()
def mark_notification_read(name: str):
	_rl()
	doc = frappe.get_doc("Space Notification", name)
	cust = customer_for_user()
	roles = set(frappe.get_roles())
	if cust and doc.customer != cust and not roles.intersection({"System Manager", "Space Admin"}):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	doc.is_read = 1
	doc.save(ignore_permissions=True)
	return ok({"name": name, "is_read": 1})


@frappe.whitelist()
def get_profile():
	_rl()
	cust = customer_for_user()
	if not cust:
		user = frappe.get_doc("User", frappe.session.user)
		return ok({"user": user.name, "email": user.email, "full_name": user.full_name, "customer": None})
	c = frappe.get_doc("Space Customer", cust)
	return ok(
		{
			"user": frappe.session.user,
			"customer": c.as_dict(),
		}
	)


@frappe.whitelist()
def metrics(server: str | None = None, site: str | None = None, limit: int = 48):
	_rl()
	require_roles("Space Admin", "Space Operator", "System Manager", "Readonly Auditor", "Support Engineer")
	return ok(monitoring_jobs.metrics_history(server=server, site=site, limit=limit))


@frappe.whitelist()
def server_pool_status():
	_rl()
	require_roles("Space Admin", "Space Operator", "System Manager", "Readonly Auditor")
	return ok(server_pool.capacity_summary())


@frappe.whitelist()
def search_audit(action: str | None = None, user: str | None = None, limit: int = 50):
	_rl()
	require_roles("Space Admin", "System Manager", "Readonly Auditor")
	filters = {}
	if action:
		filters["action"] = ("like", f"%{action}%")
	if user:
		filters["user"] = user
	rows = frappe.get_all(
		"Space Audit Log",
		filters=filters,
		fields=["name", "user", "action", "api_method", "ip_address", "result", "duration_ms", "creation", "ref_doctype", "ref_name"],
		order_by="creation desc",
		limit_page_length=min(int(limit or 50), 200),
	)
	return ok(rows)


@frappe.whitelist()
def create_site_v2(
	site_name: str,
	plan: str,
	admin_password: str,
	customer: str | None = None,
	preferred_server: str | None = None,
):
	"""v2 create with server pool selection; mirrors v1 response shape."""
	_rl()
	require_roles("Space Admin", "Space Operator", "Space Customer", "System Manager")
	from space.utils.site_naming import build_domain, validate_slug

	slug = validate_slug(site_name)
	domain = build_domain(slug)
	if frappe.db.exists("Space Site", slug) or frappe.db.exists("Space Site", {"domain": domain}):
		return fail("Site already exists", "EXISTS")

	cust = customer or customer_for_user()
	if not cust:
		if frappe.session.user in (None, "Guest"):
			return fail("Login required", "AUTH", 401)
		email = frappe.db.get_value("User", frappe.session.user, "email") or frappe.session.user
		cust_name = frappe.session.user
		if not frappe.db.exists("Space Customer", cust_name):
			frappe.get_doc(
				{
					"doctype": "Space Customer",
					"customer_name": cust_name,
					"email": email,
					"user": frappe.session.user,
					"status": "Active",
				}
			).insert(ignore_permissions=True)
		cust = cust_name

	server = server_pool.select_server(preferred_server)
	plan_doc = frappe.get_doc("Space Plan", plan)
	trial_days = int(plan_doc.trial_days or 0)
	payment_status = "Trial" if trial_days > 0 and float(plan_doc.monthly_price or 0) > 0 else "Free"
	status = "Trial" if payment_status == "Trial" else "Active"

	sub = frappe.get_doc(
		{
			"doctype": "Space Subscription",
			"customer": cust,
			"plan": plan,
			"start_date": today(),
			"end_date": add_days(today(), 365),
			"renewal_date": add_days(today(), 365),
			"trial_ends_on": add_days(today(), trial_days) if trial_days else None,
			"grace_period_days": 3,
			"status": status,
			"payment_status": payment_status,
			"renewal": 0,
		}
	).insert(ignore_permissions=True)

	site = frappe.get_doc(
		{
			"doctype": "Space Site",
			"site_name": slug,
			"customer": cust,
			"server": server,
			"plan": plan,
			"subscription": sub.name,
			"status": "Draft",
			"admin_password": admin_password,
		}
	).insert(ignore_permissions=True)

	# primary domain row
	frappe.get_doc(
		{
			"doctype": "Space Domain",
			"site": site.name,
			"domain": site.domain,
			"is_primary": 1,
			"dns_status": "Verified",
			"ssl_status": "Wildcard",
			"verified_at": frappe.utils.now_datetime(),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	from space.jobs.provision import enqueue_create_site

	result = enqueue_create_site(site.name)
	audit.log_audit(
		"create_site_v2",
		api_method="space.api.v2.space.create_site_v2",
		ref_doctype="Space Site",
		ref_name=site.name,
		after={"server": server, "plan": plan},
	)
	return ok({"site": site.name, "domain": site.domain, "job": result.get("job"), "server": server, "api": "v2"})
