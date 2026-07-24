"""Space core hooks contract — apps register providers, resources, and job handlers.

Other apps (e.g. space_cloud) contribute via hooks.py keys:

  space_provider_types = {"docker_bench": "pkg.module.Class"}
  space_resource_types = {"site": "pkg.module"}
  space_job_handlers = {"create_site": "pkg.module.fn"}
  space_dashboard_cards = ["Card Label", ...]
  space_api_namespaces = ["space_cloud.api.v1"]
"""

from __future__ import annotations

from typing import Any

import frappe


def get_provider_types() -> dict[str, str]:
	"""Map provider type key → dotted class path."""
	return _merge_hook_dicts("space_provider_types")


def get_resource_types() -> dict[str, str]:
	"""Map resource type key → dotted module/handler path."""
	return _merge_hook_dicts("space_resource_types")


def get_job_handlers() -> dict[str, str]:
	"""Map job_type key → dotted callable path."""
	return _merge_hook_dicts("space_job_handlers")


def get_dashboard_cards() -> list[str]:
	out: list[str] = []
	for entry in frappe.get_hooks("space_dashboard_cards") or []:
		if isinstance(entry, (list, tuple)):
			out.extend(str(x) for x in entry)
		else:
			out.append(str(entry))
	return out


def get_api_namespaces() -> list[str]:
	out: list[str] = []
	for entry in frappe.get_hooks("space_api_namespaces") or []:
		if isinstance(entry, (list, tuple)):
			out.extend(str(x) for x in entry)
		else:
			out.append(str(entry))
	return out


def resolve_provider(provider_type: str) -> Any:
	path = get_provider_types().get(provider_type)
	if not path:
		frappe.throw(f"Unknown Space provider type: {provider_type}")
	return frappe.get_attr(path)


def resolve_job_handler(job_type: str) -> Any:
	path = get_job_handlers().get(job_type)
	if not path:
		frappe.throw(f"Unknown Space job type: {job_type}")
	return frappe.get_attr(path)


def dispatch_job(job_type: str, **kwargs):
	"""Run a registered job handler synchronously (workers call this)."""
	handler = resolve_job_handler(job_type)
	return handler(**kwargs)


def _merge_hook_dicts(hook_name: str) -> dict[str, str]:
	"""Frappe returns dict hooks as {key: [value, ...]} or a list of dicts."""
	merged: dict[str, str] = {}
	raw = frappe.get_hooks(hook_name) or {}

	if isinstance(raw, dict):
		for key, val in raw.items():
			merged[str(key)] = _first_str(val)
		return {k: v for k, v in merged.items() if v}

	if isinstance(raw, (list, tuple)):
		for entry in raw:
			if isinstance(entry, dict):
				for key, val in entry.items():
					merged[str(key)] = _first_str(val)
	return {k: v for k, v in merged.items() if v}


def _first_str(val: Any) -> str:
	if isinstance(val, (list, tuple)):
		return str(val[0]) if val else ""
	return str(val) if val is not None else ""
