"""Backward-compatible Space API v1 — delegates to space_cloud.

Portal and existing clients keep calling space.api.v1.space.*;
implementations live in space_cloud.api.v1.space.
"""

from __future__ import annotations

try:
	from space_cloud.api.v1.space import (  # noqa: F401
		create_site,
		delete_site,
		get_job,
		get_site,
		list_catalog,
		list_sites,
		list_subscriptions,
		monitoring_summary,
		resume_site,
		suspend_site,
	)
except ImportError as e:  # pragma: no cover - space_cloud not installed
	raise ImportError(
		"space_cloud is required for Space portal APIs. "
		"Install the space_cloud app on this site."
	) from e
