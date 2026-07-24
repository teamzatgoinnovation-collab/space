"""Backward-compatible Space API v4 — delegates to space_cloud.

Portal and existing clients keep calling space.api.v4.space.*;
implementations live in space_cloud.api.v4.space.
"""

from __future__ import annotations

try:
	from space_cloud.api.v4.space import (  # noqa: F401
		acknowledge_alert,
		capacity_forecast,
		create_site_v4,
		docker_logs,
		docker_overview,
		docker_restart,
		enqueue_migration,
		failover_stub,
		heartbeat_now,
		infra_status,
		list_alerts,
		list_clusters,
		list_migrations,
		list_nodes,
		list_regions,
		observability_query,
		rotate_secret,
		run_dr_test,
		start_maintenance,
	)
except ImportError as e:  # pragma: no cover - space_cloud not installed
	raise ImportError(
		"space_cloud is required for Space portal APIs. "
		"Install the space_cloud app on this site."
	) from e
