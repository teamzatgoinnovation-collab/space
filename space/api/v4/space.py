"""Backward-compatible Space API v4. See api/v1/space.py for the pattern."""

from __future__ import annotations

from space.registry import make_compat_delegates

_DELEGATED = (
	"acknowledge_alert",
	"capacity_forecast",
	"create_site_v4",
	"docker_logs",
	"docker_overview",
	"docker_restart",
	"enqueue_migration",
	"failover_stub",
	"heartbeat_now",
	"infra_status",
	"list_alerts",
	"list_clusters",
	"list_migrations",
	"list_nodes",
	"list_regions",
	"observability_query",
	"rotate_secret",
	"run_dr_test",
	"start_maintenance",
)

globals().update(make_compat_delegates("v4", _DELEGATED))
