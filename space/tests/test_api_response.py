"""API response shape — pure helper mirror for CI without frappe."""

from __future__ import annotations

import unittest


class TestResponseHelpers(unittest.TestCase):
	def test_ok_shape(self):
		def ok(data=None, **kwargs):
			out = {"ok": True}
			if data is not None:
				out["data"] = data
			out.update(kwargs)
			return out

		self.assertEqual(ok({"a": 1}), {"ok": True, "data": {"a": 1}})


if __name__ == "__main__":
	unittest.main()
