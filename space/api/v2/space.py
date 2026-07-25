"""Backward-compatible Space API v2. See api/v1/space.py for the pattern."""

from __future__ import annotations

from space.registry import make_compat_delegates

_DELEGATED = (
	"admin_dashboard",
	"attach_domain",
	"backup_now",
	"cancel_job",
	"create_site_v2",
	"delete_backup",
	"detach_domain",
	"get_job_detail",
	"get_profile",
	"list_backups",
	"list_domains",
	"list_invoices",
	"list_jobs",
	"list_notifications",
	"list_payment_history",
	"list_usage",
	"mark_notification_read",
	"metrics",
	"portal_dashboard",
	"restore_backup",
	"retry_job",
	"search_audit",
	"server_pool_status",
	"verify_domain",
)

globals().update(make_compat_delegates("v2", _DELEGATED))
