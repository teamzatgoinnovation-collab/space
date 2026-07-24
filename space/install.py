"""Seed core roles, settings, plans, and default provider after install/migrate."""

from __future__ import annotations

import json

import frappe


ROLES = (
	"Space Admin",
	"Space Operator",
	"Space Customer",
	"Billing Manager",
	"Support Engineer",
	"Readonly Auditor",
	"Marketplace Manager",
	"Cloud Infra Admin",
)

DEFAULT_PLANS = [
	{
		"code": "basic",
		"title": "Basic",
		"monthly_price": 0,
		"yearly_price": 0,
		"max_users": 5,
		"storage_mb": 5120,
		"cpu_limit": 1,
		"ram_mb": 1024,
		"trial_days": 14,
		"features": "1 site\nERPNext core\n1 GB RAM\n5 GB disk\nCommunity support",
		"is_active": 1,
		"sort_order": 1,
		"apps": ["frappe", "erpnext"],
	},
	{
		"code": "pro",
		"title": "Pro",
		"monthly_price": 49,
		"yearly_price": 490,
		"max_users": 25,
		"storage_mb": 15360,
		"cpu_limit": 2,
		"ram_mb": 3072,
		"trial_days": 14,
		"features": "1 site\nERPNext + apps\n3 GB RAM\n15 GB disk\nPriority support",
		"is_active": 1,
		"sort_order": 2,
		"apps": ["frappe", "erpnext", "hrms"],
	},
	{
		"code": "enterprise",
		"title": "Enterprise",
		"monthly_price": 199,
		"yearly_price": 1990,
		"max_users": 100,
		"storage_mb": 30720,
		"cpu_limit": 4,
		"ram_mb": 5120,
		"trial_days": 30,
		"features": "Multi-site ready\nCustom apps\n5 GB RAM\n30 GB disk\nDedicated onboarding",
		"is_active": 1,
		"sort_order": 3,
		"apps": ["frappe", "erpnext", "hrms", "crm"],
	},
]

DEFAULT_TICKET_CATEGORIES = [
	"Provisioning",
	"Billing",
	"Marketplace",
	"Performance",
	"Security",
	"Other",
]

CORE_NUMBER_CARDS = [
	{
		"name": "Space Total Customers",
		"label": "Customers",
		"type": "Document Type",
		"document_type": "Space Customer",
		"function": "Count",
		"filters_json": "[]",
	},
	{
		"name": "Space Trials",
		"label": "Trials",
		"type": "Document Type",
		"document_type": "Space Subscription",
		"function": "Count",
		"filters_json": '[["Space Subscription","status","=","Trial"]]',
	},
	{
		"name": "Space Providers",
		"label": "Providers",
		"type": "Document Type",
		"document_type": "Space Provider",
		"function": "Count",
		"filters_json": "[]",
	},
	{
		"name": "Space Jobs Failed",
		"label": "Failed Core Jobs",
		"type": "Document Type",
		"document_type": "Space Job",
		"function": "Count",
		"filters_json": '[["Space Job","status","=","Failed"]]',
	},
]


def after_install():
	try:
		_seed_all()
	except Exception:
		frappe.log_error(title="Space after_install seed failed")


def after_migrate():
	try:
		_seed_all()
	except Exception:
		frappe.log_error(title="Space after_migrate seed failed")
		frappe.db.rollback()
		try:
			_ensure_roles()
			_seed_settings()
			_seed_plans()
			_seed_default_provider()
			frappe.db.commit()
		except Exception:
			frappe.log_error(title="Space after_migrate core seed failed")
			frappe.db.rollback()


def _seed_all():
	_ensure_roles()
	_seed_settings()
	_seed_plans()
	_seed_default_provider()
	_seed_ticket_categories()
	_seed_core_number_cards()
	frappe.db.commit()


def _ensure_roles():
	for role in ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)


def _seed_settings():
	if not frappe.db.exists("DocType", "Space Settings"):
		return
	doc = frappe.get_single("Space Settings")
	changed = False
	defaults = {
		"domain_suffix": "zatgo.online",
		"portal_base_url": "https://portal.zatgo.online",
		"ram_pool_mb": 10240,
		"disk_pool_mb": 102400,
		"reserved_slugs": "space,portal,erp,www,mail,api",
		"allowed_origins": "https://portal.zatgo.online",
		"backup_schedule": "Daily",
		"backup_retention_days": 14,
		"monitoring_interval_minutes": 60,
		"server_selection": "Preferred",
		"default_plan": "basic",
		"default_apps": "frappe,erpnext",
		"ssh_default_user": "root",
		"ssh_default_port": 22,
		"deployment_timeout_minutes": 120,
		"estimated_create_minutes": 15,
		"rate_limit_per_minute": 120,
	}
	for key, val in defaults.items():
		if not doc.get(key):
			doc.set(key, val)
			changed = True
	if changed:
		doc.save(ignore_permissions=True)


def _seed_plans():
	if not frappe.db.exists("DocType", "Space Plan"):
		return
	for plan in DEFAULT_PLANS:
		apps = list(plan.get("apps") or [])
		payload = {k: v for k, v in plan.items() if k != "apps"}
		name = payload["code"]
		if frappe.db.exists("Space Plan", name):
			doc = frappe.get_doc("Space Plan", name)
			for k, v in payload.items():
				doc.set(k, v)
			doc.set("allowed_apps", [])
			for pkg in apps:
				doc.append("allowed_apps", {"app_package": pkg, "app_title": pkg.replace("_", " ").title()})
			doc.save(ignore_permissions=True)
		else:
			doc = frappe.get_doc(
				{
					"doctype": "Space Plan",
					**payload,
					"allowed_apps": [
						{"app_package": pkg, "app_title": pkg.replace("_", " ").title()} for pkg in apps
					],
				}
			)
			doc.insert(ignore_permissions=True)


def _seed_default_provider():
	if not frappe.db.exists("DocType", "Space Provider"):
		return
	name = "docker-bench-primary"
	if frappe.db.exists("Space Provider", name):
		return
	frappe.get_doc(
		{
			"doctype": "Space Provider",
			"provider_name": name,
			"title": "DigitalOcean Docker Bench",
			"provider_type": "docker_bench",
			"status": "Active",
			"is_default": 1,
			"capabilities": "bench\ndocker\nssh\nsites",
			"config_json": json.dumps({"server": "primary-do", "host": "157.230.8.164"}),
		}
	).insert(ignore_permissions=True)


def _seed_ticket_categories():
	if not frappe.db.exists("DocType", "Space Ticket Category"):
		return
	for cat in DEFAULT_TICKET_CATEGORIES:
		if frappe.db.exists("Space Ticket Category", cat):
			continue
		frappe.get_doc({"doctype": "Space Ticket Category", "category_name": cat}).insert(
			ignore_permissions=True
		)


def _seed_core_number_cards():
	if not frappe.db.exists("DocType", "Number Card"):
		return
	for spec in CORE_NUMBER_CARDS:
		wanted = spec["name"]
		if frappe.db.exists("Number Card", wanted):
			doc = frappe.get_doc("Number Card", wanted)
			for k, v in spec.items():
				if k != "name":
					doc.set(k, v)
			doc.is_public = 1
			doc.module = "Space"
			doc.save(ignore_permissions=True)
		else:
			frappe.get_doc(
				{
					"doctype": "Number Card",
					**spec,
					"is_public": 1,
					"module": "Space",
					"show_percentage_stats": 0,
				}
			).insert(ignore_permissions=True)
