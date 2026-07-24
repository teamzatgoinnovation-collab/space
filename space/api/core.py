"""Generic Space core API — customers, subscriptions, jobs, activity."""

from __future__ import annotations

import frappe
from frappe import _

from space.api.v1.response import customer_for_user, fail, ok, require_roles
from space.services import rate_limit


@frappe.whitelist()
def me():
	"""Current Space Customer linked to session user (if any)."""
	rate_limit.check_rate_limit("core_me")
	cust = customer_for_user()
	if not cust:
		return ok({"customer": None, "user": frappe.session.user})
	doc = frappe.get_doc("Space Customer", cust)
	return ok(
		{
			"customer": doc.name,
			"company": doc.company,
			"email": doc.email,
			"phone": doc.phone,
			"status": doc.status,
			"user": frappe.session.user,
		}
	)


@frappe.whitelist()
def list_subscriptions():
	rate_limit.check_rate_limit("core_subs")
	filters = {}
	cust = customer_for_user()
	roles = set(frappe.get_roles())
	if cust and not roles.intersection({"System Manager", "Space Admin", "Space Operator", "Billing Manager"}):
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
			"auto_renew",
		],
		order_by="modified desc",
	)
	return ok({"subscriptions": rows})


@frappe.whitelist()
def list_jobs(limit: int = 50):
	"""List generic Space Job envelopes."""
	rate_limit.check_rate_limit("core_jobs")
	require_roles("System Manager", "Space Admin", "Space Operator", "Readonly Auditor")
	rows = frappe.get_all(
		"Space Job",
		fields=[
			"name",
			"job_type",
			"status",
			"progress",
			"provider",
			"resource",
			"reference_doctype",
			"reference_name",
			"started_at",
			"finished_at",
		],
		order_by="creation desc",
		limit_page_length=int(limit or 50),
	)
	return ok({"jobs": rows})


@frappe.whitelist()
def get_job(name: str):
	require_roles("System Manager", "Space Admin", "Space Operator", "Readonly Auditor", "Space Customer")
	if not frappe.db.exists("Space Job", name):
		return fail(_("Job not found"))
	doc = frappe.get_doc("Space Job", name)
	return ok(doc.as_dict())


@frappe.whitelist()
def list_activity(limit: int = 50, reference: str | None = None):
	rate_limit.check_rate_limit("core_activity")
	require_roles("System Manager", "Space Admin", "Space Operator", "Readonly Auditor")
	filters = {}
	if reference:
		filters["reference_name"] = reference
	rows = frappe.get_all(
		"Space Activity Log",
		filters=filters,
		fields=["name", "subject", "reference_doctype", "reference_name", "user", "creation"],
		order_by="creation desc",
		limit_page_length=int(limit or 50),
	)
	return ok({"activity": rows})


@frappe.whitelist()
def list_providers():
	require_roles("System Manager", "Space Admin", "Space Operator", "Cloud Infra Admin")
	rows = frappe.get_all(
		"Space Provider",
		fields=["name", "title", "provider_type", "status", "is_default"],
		order_by="is_default desc, name asc",
	)
	return ok({"providers": rows})


@frappe.whitelist()
def registry_summary():
	"""Inspect registered provider/resource/job hooks (admin)."""
	require_roles("System Manager", "Space Admin")
	from space.registry import get_api_namespaces, get_dashboard_cards, get_job_handlers, get_provider_types, get_resource_types

	return ok(
		{
			"provider_types": get_provider_types(),
			"resource_types": get_resource_types(),
			"job_handlers": get_job_handlers(),
			"dashboard_cards": get_dashboard_cards(),
			"api_namespaces": get_api_namespaces(),
		}
	)
