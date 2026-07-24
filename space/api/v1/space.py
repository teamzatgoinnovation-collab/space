"""Space REST API for portal and clients."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, today

from space.api.v1.response import customer_for_user, fail, ok, require_roles
from space.jobs import monitoring as monitoring_jobs
from space.jobs.lifecycle import enqueue_delete_site, enqueue_resume_site, enqueue_suspend_site
from space.jobs.provision import enqueue_create_site
from space.services import audit, bench_client, rate_limit, server_pool
from space.utils.site_naming import build_domain, validate_slug


@frappe.whitelist(allow_guest=True)
def list_catalog():
	"""Public catalog: plans + bench apps + pool settings."""
	rate_limit.check_rate_limit("catalog")
	plans = []
	for p in frappe.get_all(
		"Space Plan",
		filters={"is_active": 1},
		fields=[
			"name",
			"code",
			"title",
			"monthly_price",
			"yearly_price",
			"storage_mb",
			"ram_mb",
			"cpu_limit",
			"max_users",
			"trial_days",
			"features",
			"sort_order",
		],
		order_by="sort_order asc",
	):
		doc = frappe.get_doc("Space Plan", p.name)
		feat = p.get("features") or ""
		features = (
			[x.strip() for x in feat.splitlines() if x.strip()]
			if isinstance(feat, str)
			else list(feat or [])
		)
		plans.append(
			{
				**p,
				"features": features,
				"allowed_apps": [
					{"package": r.app_package, "title": r.app_title} for r in doc.allowed_apps
				],
				"mock_price": f"${p.monthly_price:g} / mo" if p.monthly_price else "Free",
				"priceCents": int(float(p.monthly_price or 0) * 100),
				"dueTodayCents": 0,
				"ramLimitMb": p.ram_mb,
				"diskLimitMb": p.storage_mb,
			}
		)

	apps = []
	try:
		server = bench_client.get_server()
		for pkg in bench_client.list_bench_apps(server.name):
			if pkg == "zatgo_space":
				continue
			apps.append(
				{
					"package": pkg,
					"title": pkg.replace("_", " ").title(),
					"required": pkg == "frappe",
				}
			)
	except Exception:
		apps = [
			{"package": "frappe", "title": "Frappe", "required": True},
			{"package": "erpnext", "title": "ERPNext", "required": False},
		]

	settings = frappe.get_single("Space Settings")
	suffix = settings.domain_suffix or "zatgo.online"
	return ok(
		{
			"domainSuffix": suffix,
			"plans": plans,
			"apps": apps,
			"pool": {
				"ramPoolMb": settings.ram_pool_mb or 0,
				"diskPoolMb": settings.disk_pool_mb or 0,
			},
			"billing": {"mode": "status_only", "message": "Space Phase 2 — status-only billing (gateway disabled)"},
			"controlPlane": "space",
			"apiVersions": ["v1", "v2"],
		}
	)


@frappe.whitelist()
def list_sites():
	filters = {"status": ("!=", "Deleted")}
	cust = customer_for_user()
	if cust and "Space Admin" not in frappe.get_roles() and frappe.session.user != "Administrator":
		filters["customer"] = cust
	rows = frappe.get_all(
		"Space Site",
		filters=filters,
		fields=[
			"name",
			"site_name",
			"domain",
			"status",
			"customer",
			"server",
			"plan",
			"storage_used_mb",
			"ssl_status",
			"job",
		],
		order_by="modified desc",
	)
	return ok(rows)


@frappe.whitelist()
def get_site(name: str):
	doc = frappe.get_doc("Space Site", name)
	_ensure_site_access(doc)
	return ok(doc.as_dict())


@frappe.whitelist()
def create_site(
	site_name: str,
	plan: str,
	admin_password: str,
	customer: str | None = None,
	apps: str | None = None,
):
	"""Create Space Site doc and enqueue provision. apps = comma-separated optional."""
	require_roles("Space Admin", "Space Operator", "Space Customer", "System Manager")
	slug = validate_slug(site_name)
	domain = build_domain(slug)
	if frappe.db.exists("Space Site", slug) or frappe.db.exists("Space Site", {"domain": domain}):
		return fail("Site already exists", "EXISTS")

	cust = customer or customer_for_user()
	if not cust:
		# Auto-create customer for portal user
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

	server = server_pool.select_server()

	# Subscription (free / trial status-only)
	plan_doc = frappe.get_doc("Space Plan", plan)
	trial_days = int(plan_doc.trial_days or 0)
	payment_status = "Trial" if trial_days > 0 and float(plan_doc.monthly_price or 0) > 0 else "Free"
	sub_status = "Trial" if payment_status == "Trial" else "Active"
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
			"status": sub_status,
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
	)
	# optional app override via child table from plan is default; apps param reserved
	site.insert(ignore_permissions=True)
	frappe.db.commit()

	result = enqueue_create_site(site.name)
	return ok({"site": site.name, "domain": site.domain, "job": result.get("job")})


@frappe.whitelist()
def suspend_site(name: str):
	doc = frappe.get_doc("Space Site", name)
	_ensure_site_access(doc, write=True)
	return ok(enqueue_suspend_site(name))


@frappe.whitelist()
def resume_site(name: str):
	doc = frappe.get_doc("Space Site", name)
	_ensure_site_access(doc, write=True)
	return ok(enqueue_resume_site(name))


@frappe.whitelist()
def delete_site(name: str):
	doc = frappe.get_doc("Space Site", name)
	_ensure_site_access(doc, write=True)
	return ok(enqueue_delete_site(name))


@frappe.whitelist()
def get_job(name: str):
	job = frappe.get_doc("Space Deployment Job", name)
	site = frappe.get_doc("Space Site", job.site)
	_ensure_site_access(site)
	return ok(job.as_dict())


@frappe.whitelist()
def monitoring_summary():
	require_roles("Space Admin", "Space Operator", "System Manager")
	return ok(monitoring_jobs.dashboard_summary())


@frappe.whitelist()
def list_subscriptions():
	filters = {}
	cust = customer_for_user()
	roles = frappe.get_roles()
	if cust and "Space Admin" not in roles and frappe.session.user != "Administrator":
		filters["customer"] = cust
	rows = frappe.get_all(
		"Space Subscription",
		filters=filters,
		fields=[
			"name",
			"customer",
			"plan",
			"status",
			"payment_status",
			"start_date",
			"end_date",
			"renewal",
			"renewal_date",
			"trial_ends_on",
			"grace_period_days",
		],
		order_by="modified desc",
	)
	return ok(rows)


def _ensure_site_access(doc, write: bool = False):
	roles = set(frappe.get_roles())
	if frappe.session.user == "Administrator" or roles.intersection(
		{"System Manager", "Space Admin", "Space Operator", "Support Engineer"}
	):
		return
	cust = customer_for_user()
	if not cust or doc.customer != cust:
		audit.log_audit("site_access_denied", result="Denied", ref_doctype="Space Site", ref_name=getattr(doc, "name", None))
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if write and "Space Customer" not in roles:
		# customers can trigger lifecycle on own sites in Phase 1
		pass
