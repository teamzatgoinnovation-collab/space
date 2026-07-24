# Copyright (c) 2026, ZatGo Innovation and contributors
# License: MIT

import frappe
from frappe.model.document import Document


class SpaceSite(Document):
	def before_insert(self):
		from space.utils.site_naming import build_domain, validate_slug
		validate_slug(self.site_name)
		self.domain = build_domain(self.site_name)
		if not self.status:
			self.status = "Draft"

	def validate(self):
		from space.utils.site_naming import build_domain, validate_slug
		if self.site_name:
			validate_slug(self.site_name)
			self.domain = build_domain(self.site_name)

	@frappe.whitelist()
	def create_site(self):
		from space.jobs.provision import enqueue_create_site
		return enqueue_create_site(self.name)

	@frappe.whitelist()
	def suspend_site(self):
		from space.jobs.lifecycle import enqueue_suspend_site
		return enqueue_suspend_site(self.name)

	@frappe.whitelist()
	def resume_site(self):
		from space.jobs.lifecycle import enqueue_resume_site
		return enqueue_resume_site(self.name)

	@frappe.whitelist()
	def delete_site(self):
		from space.jobs.lifecycle import enqueue_delete_site
		return enqueue_delete_site(self.name)

	@frappe.whitelist()
	def open_site(self):
		return {"url": f"https://{self.domain}"}
