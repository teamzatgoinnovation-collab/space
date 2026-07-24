"""SSL monitoring (wildcard / LetsEncrypt status)."""

from __future__ import annotations

import frappe
from frappe.utils import add_days, getdate, now_datetime, today

from space.services import notifications


def check_ssl_all():
	"""Daily SSL check for servers and custom domains."""
	for server_name in frappe.get_all("Space Server", filters={"status": ("!=", "Offline")}, pluck="name"):
		try:
			check_server_ssl(server_name)
		except Exception:
			frappe.log_error(title=f"SSL check failed for server {server_name}")

	for domain_name in frappe.get_all("Space Domain", pluck="name"):
		try:
			check_domain_ssl(domain_name)
		except Exception:
			frappe.log_error(title=f"SSL check failed for domain {domain_name}")


def check_server_ssl(server_name: str):
	server = frappe.get_doc("Space Server", server_name)
	mode = server.ssl_mode or "Wildcard"
	# Phase 2: wildcard assumed valid on droplet; track expiry if set
	server.ssl_last_checked = now_datetime()
	if mode == "Wildcard":
		# keep existing expiry or set +90 days placeholder when missing
		if not server.ssl_expires_on:
			server.ssl_expires_on = add_days(today(), 90)
	if server.ssl_expires_on and getdate(server.ssl_expires_on) < getdate(today()):
		notifications.notify(
			title=f"SSL expired on server {server.name}",
			event_type="ssl_expired",
			message=f"Server {server.name} SSL expired on {server.ssl_expires_on}",
			ref_doctype="Space Server",
			ref_name=server.name,
		)
	server.save(ignore_permissions=True)
	frappe.db.commit()


def check_domain_ssl(domain_name: str):
	dom = frappe.get_doc("Space Domain", domain_name)
	# If attached to site using wildcard suffix, mark Wildcard
	settings = frappe.get_single("Space Settings")
	suffix = (settings.domain_suffix or "zatgo.online").strip()
	if dom.domain.endswith("." + suffix) or dom.domain == suffix:
		dom.ssl_status = "Wildcard"
	elif dom.ssl_status in (None, "", "Unknown", "Pending"):
		dom.ssl_status = "Pending"
	# Expired detection if previously Valid/LetsEncrypt with no renewal — status-only
	if dom.ssl_status == "Expired":
		notifications.notify(
			title=f"SSL expired: {dom.domain}",
			event_type="ssl_expired",
			message=f"Domain {dom.domain} SSL is expired",
			ref_doctype="Space Domain",
			ref_name=dom.name,
		)
	dom.save(ignore_permissions=True)
	frappe.db.commit()
