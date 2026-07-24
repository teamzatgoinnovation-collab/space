# Copyright (c) 2026, ZatGo Innovation and contributors
# License: MIT

import secrets

import frappe
from frappe.model.document import Document


class SpaceDomain(Document):
	def before_insert(self):
		if not self.verification_token:
			self.verification_token = secrets.token_urlsafe(16)
		self.domain = (self.domain or "").strip().lower()

	def validate(self):
		self.domain = (self.domain or "").strip().lower()

	@frappe.whitelist()
	def verify(self):
		"""Mark DNS verified when TXT/CNAME check would pass — Phase 2 status check."""
		# Best-effort: if domain resolves to same suffix / or already primary site domain
		site = frappe.get_doc("Space Site", self.site)
		ok = False
		if self.domain == (site.domain or "").lower():
			ok = True
			self.dns_status = "Verified"
			self.ssl_status = self.ssl_status if self.ssl_status not in (None, "", "Unknown") else "Wildcard"
		else:
			# Custom domain — pending until operator confirms
			self.dns_status = "Pending"
			self.verification_token = self.verification_token or secrets.token_urlsafe(16)
		if ok:
			self.verified_at = frappe.utils.now_datetime()
		self.save()
		return {"ok": ok, "dns_status": self.dns_status, "token": self.verification_token}

	@frappe.whitelist()
	def attach(self):
		"""Attach domain to site (set primary if requested)."""
		if self.is_primary:
			frappe.db.sql(
				"update `tabSpace Domain` set is_primary=0 where site=%s and name!=%s",
				(self.site, self.name),
			)
		self.dns_status = self.dns_status or "Pending"
		self.save()
		return {"ok": True}

	@frappe.whitelist()
	def detach(self):
		self.dns_status = "Unknown"
		self.is_primary = 0
		self.save()
		return {"ok": True}
