"""Backward-compatible Space API v3 — delegates to space_cloud.

Portal and existing clients keep calling space.api.v3.space.*;
implementations live in space_cloud.api.v3.space.
"""

from __future__ import annotations

try:
	from space_cloud.api.v3.space import (  # noqa: F401
		analytics_summary,
		create_api_token,
		create_license,
		create_ticket,
		deactivate_license,
		get_app,
		global_search,
		install_app,
		list_apps,
		list_install_history,
		list_licenses,
		list_tickets,
		rate_app,
		rebuild_assets,
		register_webhook,
		remove_app,
		renew_license,
		reply_ticket,
		revoke_api_token,
		test_webhook,
		update_app,
	)
except ImportError as e:  # pragma: no cover - space_cloud not installed
	raise ImportError(
		"space_cloud is required for Space portal APIs. "
		"Install the space_cloud app on this site."
	) from e
