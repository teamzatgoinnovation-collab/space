"""License key generate / renew / deactivate."""

from __future__ import annotations

import secrets

import frappe
from frappe.utils import add_days, today


def generate_license(customer: str, plan: str | None = None, site: str | None = None, apps: str = "", days: int = 365) -> str:
	key = "spc_" + secrets.token_urlsafe(24)
	doc = frappe.get_doc(
		{
			"doctype": "Space License",
			"license_key": key,
			"customer": customer,
			"plan": plan,
			"site": site,
			"apps": apps,
			"status": "Active",
			"issued_on": today(),
			"expires_on": add_days(today(), int(days or 365)),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def deactivate_license(name: str) -> dict:
	doc = frappe.get_doc("Space License", name)
	doc.status = "Deactivated"
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "name": name}


def renew_license(name: str, days: int = 365) -> dict:
	doc = frappe.get_doc("Space License", name)
	doc.expires_on = add_days(doc.expires_on or today(), int(days))
	doc.status = "Active"
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "expires_on": str(doc.expires_on)}


def expire_licenses():
	for name in frappe.get_all(
		"Space License",
		filters={"status": "Active", "expires_on": ("<", today())},
		pluck="name",
	):
		doc = frappe.get_doc("Space License", name)
		doc.status = "Expired"
		doc.save(ignore_permissions=True)
	frappe.db.commit()
