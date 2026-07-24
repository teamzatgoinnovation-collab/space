"""Seed roles, plans, settings, and default server after install/migrate."""

from __future__ import annotations

import frappe


ROLES = ("Space Admin", "Space Operator", "Space Customer")

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


def after_install():
	try:
		_seed_all()
	except Exception:
		frappe.log_error(title="Space after_install seed failed")


def after_migrate():
	_seed_all()


def _seed_all():
	_ensure_roles()
	_seed_settings()
	_seed_plans()
	_seed_default_server()
	_seed_number_cards()
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


def _seed_default_server():
	if not frappe.db.exists("DocType", "Space Server"):
		return
	name = "primary-do"
	if frappe.db.exists("Space Server", name):
		return
	frappe.get_doc(
		{
			"doctype": "Space Server",
			"server_name": name,
			"title": "DigitalOcean Primary",
			"ip_address": "157.230.8.164",
			"ssh_user": "root",
			"ssh_port": 22,
			"auth_method": "Private Key",
			"docker_host": "unix:///var/run/docker.sock",
			"backend_container": "frappe_docker-backend-1",
			"cpu_cores": 1,
			"ram_mb": 2048,
			"disk_mb": 49152,
			"status": "Active",
			"health": "Unknown",
			"is_default": 1,
		}
	).insert(ignore_permissions=True)


NUMBER_CARDS = [
	{
		"name": "Space Total Customers",
		"label": "Customers",
		"document_type": "Space Customer",
		"function": "Count",
		"filters_json": "[]",
	},
	{
		"name": "Space Total Servers",
		"label": "Servers",
		"document_type": "Space Server",
		"function": "Count",
		"filters_json": "[]",
	},
	{
		"name": "Space Total Sites",
		"label": "Sites",
		"document_type": "Space Site",
		"function": "Count",
		"filters_json": '[["Space Site","status","!=","Deleted"]]',
	},
	{
		"name": "Space Active Sites",
		"label": "Active Sites",
		"document_type": "Space Site",
		"function": "Count",
		"filters_json": '[["Space Site","status","=","Active"]]',
	},
	{
		"name": "Space Failed Jobs",
		"label": "Failed Jobs",
		"document_type": "Space Deployment Job",
		"function": "Count",
		"filters_json": '[["Space Deployment Job","status","=","Failed"]]',
	},
]


def _seed_number_cards():
	if not frappe.db.exists("DocType", "Number Card"):
		return
	card_names = []
	for spec in NUMBER_CARDS:
		card_names.append(spec["name"])
		if frappe.db.exists("Number Card", spec["name"]):
			doc = frappe.get_doc("Number Card", spec["name"])
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

	if not frappe.db.exists("Workspace", "Cloud Manager"):
		return
	ws = frappe.get_doc("Workspace", "Cloud Manager")
	ws.set("number_cards", [])
	for name in card_names:
		ws.append("number_cards", {"number_card_name": name, "label": name})
	# Ensure number cards appear in workspace content
	import json

	try:
		content = json.loads(ws.content or "[]")
	except Exception:
		content = []
	# Drop old number_card blocks then prepend fresh ones after header
	content = [b for b in content if b.get("type") != "number_card"]
	nc_blocks = [
		{
			"id": f"nc{i}",
			"type": "number_card",
			"data": {"number_card_name": name, "col": 2 if i < 4 else 4},
		}
		for i, name in enumerate(card_names)
	]
	# Insert after first header if present
	if content and content[0].get("type") == "header":
		content = [content[0]] + nc_blocks + content[1:]
	else:
		content = nc_blocks + content
	ws.content = json.dumps(content)
	ws.save(ignore_permissions=True)
