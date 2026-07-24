"""Unit tests for Space Phase 2 pure helpers (no frappe runtime required)."""

from __future__ import annotations

import unittest


class TestCapacity(unittest.TestCase):
	def test_capacity_ok(self):
		def capacity_ok(row):
			max_sites = row.get("max_sites") or 50
			active = row.get("active_sites") or 0
			return active < max_sites

		self.assertTrue(capacity_ok({"active_sites": 1, "max_sites": 10}))
		self.assertFalse(capacity_ok({"active_sites": 10, "max_sites": 10}))


class TestRedact(unittest.TestCase):
	def test_redacts_passwords(self):
		SENSITIVE = {"password", "admin_password", "api_key", "secret"}

		def redact(obj):
			if isinstance(obj, dict):
				return {
					k: ("***" if str(k).lower() in SENSITIVE or str(k).lower().endswith("_password") else redact(v))
					for k, v in obj.items()
				}
			if isinstance(obj, list):
				return [redact(x) for x in obj]
			return obj

		out = redact({"admin_password": "secret", "site": "demo", "nested": {"api_key": "x"}})
		self.assertEqual(out["admin_password"], "***")
		self.assertEqual(out["nested"]["api_key"], "***")
		self.assertEqual(out["site"], "demo")


class TestSanitize(unittest.TestCase):
	def test_sanitize_sensitive_log(self):
		def sanitize(d):
			out = dict(d)
			val = out.get("output")
			if isinstance(val, str) and "PRIVATE KEY" in val:
				out["output"] = "[redacted sensitive output]"
			return out

		d = sanitize({"output": "BEGIN RSA PRIVATE KEY\nabc", "progress": 10})
		self.assertIn("redacted", d["output"])
		self.assertEqual(d["progress"], 10)


class TestExceptions(unittest.TestCase):
	def test_hierarchy(self):
		from space.utils.exceptions import SpaceCapacityError, SpaceError

		self.assertTrue(issubclass(SpaceCapacityError, SpaceError))


class TestOkShape(unittest.TestCase):
	def test_ok_shape(self):
		def ok(data=None, **kwargs):
			out = {"ok": True}
			if data is not None:
				out["data"] = data
			out.update(kwargs)
			return out

		self.assertEqual(ok({"a": 1}), {"ok": True, "data": {"a": 1}})
		self.assertEqual(ok(), {"ok": True})


if __name__ == "__main__":
	unittest.main()
