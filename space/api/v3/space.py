"""Backward-compatible Space API v3. See api/v1/space.py for the pattern."""

from __future__ import annotations

from space.registry import make_compat_delegates

_DELEGATED = (
	"analytics_summary",
	"create_api_token",
	"create_license",
	"create_ticket",
	"deactivate_license",
	"get_app",
	"global_search",
	"install_app",
	"list_apps",
	"list_install_history",
	"list_licenses",
	"list_tickets",
	"rate_app",
	"rebuild_assets",
	"register_webhook",
	"remove_app",
	"renew_license",
	"reply_ticket",
	"revoke_api_token",
	"test_webhook",
	"update_app",
)

globals().update(make_compat_delegates("v3", _DELEGATED))
