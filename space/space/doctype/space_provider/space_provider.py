# Copyright (c) 2026, ZatGo Innovation and contributors
# License: MIT

import frappe
from frappe.model.document import Document


class SpaceProvider(Document):
	@frappe.whitelist()
	def test_connection(self):
		from space.registry import resolve_provider

		driver = resolve_provider(self.provider_type)
		inst = driver(provider_name=self.name)
		return inst.connect()

	@frappe.whitelist()
	def health_check(self):
		from space.registry import resolve_provider

		driver = resolve_provider(self.provider_type)
		inst = driver(provider_name=self.name)
		return inst.health_check()
