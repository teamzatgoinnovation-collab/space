"""Backward-compatible Space API v2 — delegates to space_cloud.

Portal and existing clients keep calling space.api.v2.space.*;
implementations live in space_cloud.api.v2.space.
"""

from __future__ import annotations

try:
	from space_cloud.api.v2.space import (  # noqa: F401
		admin_dashboard,
		attach_domain,
		backup_now,
		cancel_job,
		create_site_v2,
		delete_backup,
		detach_domain,
		get_job_detail,
		get_profile,
		list_backups,
		list_domains,
		list_invoices,
		list_jobs,
		list_notifications,
		list_payment_history,
		list_usage,
		mark_notification_read,
		metrics,
		portal_dashboard,
		restore_backup,
		retry_job,
		search_audit,
		server_pool_status,
		verify_domain,
	)
except ImportError as e:  # pragma: no cover - space_cloud not installed
	raise ImportError(
		"space_cloud is required for Space portal APIs. "
		"Install the space_cloud app on this site."
	) from e
