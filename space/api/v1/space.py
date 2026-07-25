"""Backward-compatible Space API v1.

Portal and existing clients keep calling space.api.v1.space.*; the actual
implementation lives in whichever vertical app registers itself via the
space_v1_compat_module hook (space_cloud today — see
space.registry.resolve_compat_function). Space core resolves that module
at call time instead of importing it directly: a generic control-plane
framework must not have a static dependency on one of its own verticals.
"""

from __future__ import annotations

from space.registry import make_compat_delegates

_DELEGATED = (
	"create_site",
	"delete_site",
	"get_job",
	"get_site",
	"list_catalog",
	"list_sites",
	"list_subscriptions",
	"monitoring_summary",
	"resume_site",
	"suspend_site",
)

globals().update(make_compat_delegates("v1", _DELEGATED))
