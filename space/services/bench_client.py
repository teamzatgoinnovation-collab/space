"""
SSH / docker exec client for the shared Frappe bench.

Phase 1: when Space runs ON the same droplet as the bench, prefer local
`docker exec`. Otherwise SSH to the Space Server IP.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from typing import Any

import frappe

PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
SITE_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,250}[a-z0-9])?$")


class BenchError(Exception):
	def __init__(self, message: str, stdout: str = "", stderr: str = "", code: int = 1):
		super().__init__(message)
		self.stdout = stdout
		self.stderr = stderr
		self.code = code


def _assert_site(site: str) -> str:
	s = (site or "").strip().lower()
	if not SITE_RE.match(s):
		raise BenchError(f"Invalid site name: {site}")
	return s


def _assert_pkg(pkg: str) -> str:
	p = (pkg or "").strip()
	if not PACKAGE_RE.match(p):
		raise BenchError(f"Invalid package: {pkg}")
	return p


def get_server(server_name: str | None = None) -> Any:
	if server_name:
		return frappe.get_doc("Space Server", server_name)
	name = frappe.db.get_value("Space Server", {"is_default": 1, "status": "Active"}, "name")
	if not name:
		name = frappe.db.get_value("Space Server", {"status": "Active"}, "name")
	if not name:
		frappe.throw("No active Space Server configured")
	return frappe.get_doc("Space Server", name)


def _in_bench_container() -> bool:
	"""True when this process already runs inside the Frappe bench container."""
	if os.environ.get("SPACE_BENCH_INPROCESS") == "1":
		return True
	# frappe_docker backend has bench but typically no docker CLI
	if os.path.isdir("/home/frappe/frappe-bench/sites") and not os.path.exists("/usr/bin/docker"):
		try:
			r = subprocess.run(["which", "bench"], capture_output=True, text=True, timeout=3)
			return r.returncode == 0
		except Exception:
			return False
	return False


def _same_host(server) -> bool:
	"""True if we can docker-exec from the host (Space site on the bench host)."""
	if os.environ.get("SPACE_BENCH_LOCAL") == "1":
		return True
	ip = (server.ip_address or "").strip()
	if ip in ("127.0.0.1", "localhost"):
		return True
	try:
		r = subprocess.run(
			["docker", "inspect", server.backend_container, "--format", "{{.Id}}"],
			capture_output=True,
			text=True,
			timeout=5,
		)
		return r.returncode == 0 and bool(r.stdout.strip())
	except Exception:
		return False


def run_on_bench(
	server_name: str | None,
	argv: list[str],
	*,
	timeout_s: int = 3600,
) -> dict[str, Any]:
	server = get_server(server_name)
	for a in argv:
		if not isinstance(a, str) or "\0" in a:
			raise BenchError("Invalid argv token")

	# Control plane site lives on the same bench → run bench commands in-process
	if _in_bench_container():
		return _run(list(argv), timeout_s, cwd="/home/frappe/frappe-bench")

	container = server.backend_container or "frappe_docker-backend-1"

	if _same_host(server):
		cmd = ["docker", "exec", "-w", "/home/frappe/frappe-bench", container, *argv]
		return _run(cmd, timeout_s)

	return _run_ssh(
		server,
		["docker", "exec", "-w", "/home/frappe/frappe-bench", container, *argv],
		timeout_s,
	)


def _run(cmd: list[str], timeout_s: int, cwd: str | None = None) -> dict[str, Any]:
	try:
		p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, cwd=cwd)
	except subprocess.TimeoutExpired as e:
		raise BenchError("Command timed out", stdout=e.stdout or "", stderr=e.stderr or "", code=124) from e
	ok = p.returncode == 0
	result = {
		"ok": ok,
		"code": p.returncode,
		"stdout": p.stdout or "",
		"stderr": p.stderr or "",
		"command": " ".join(shlex.quote(c) for c in cmd),
	}
	if not ok:
		raise BenchError(
			(p.stderr or p.stdout or "Command failed")[:500],
			stdout=p.stdout or "",
			stderr=p.stderr or "",
			code=p.returncode,
		)
	return result


def _run_ssh(server, remote_argv: list[str], timeout_s: int) -> dict[str, Any]:
	remote = " ".join(shlex.quote(a) for a in remote_argv)
	key_path = None
	try:
		key_material = server.get_password("private_key") if server.auth_method == "Private Key" else None
	except Exception:
		key_material = None

	ssh_base = [
		"ssh",
		"-o",
		"BatchMode=yes",
		"-o",
		"StrictHostKeyChecking=accept-new",
		"-o",
		f"ConnectTimeout=15",
		"-p",
		str(server.ssh_port or 22),
	]
	if key_material:
		fd, key_path = tempfile.mkstemp(prefix="space-ssh-", text=True)
		os.write(fd, key_material.encode() if isinstance(key_material, str) else key_material)
		os.close(fd)
		os.chmod(key_path, 0o600)
		ssh_base += ["-i", key_path]

	ssh_base += [f"{server.ssh_user}@{server.ip_address}", "--", remote]
	try:
		return _run(ssh_base, timeout_s)
	finally:
		if key_path and os.path.exists(key_path):
			os.unlink(key_path)


def test_server_connection(server_name: str) -> dict[str, Any]:
	r = run_on_bench(server_name, ["bench", "--version"], timeout_s=30)
	return {"ok": True, "version": (r["stdout"] or "").strip()[:200]}


def restart_backend(server_name: str) -> dict[str, Any]:
	server = get_server(server_name)
	container = server.backend_container
	if _same_host(server):
		return _run(["docker", "restart", container], 120)
	return _run_ssh(server, ["docker", "restart", container], 120)


def list_sites(server_name: str | None = None) -> list[str]:
	r = run_on_bench(server_name, ["ls", "-1", "sites"], timeout_s=30)
	noise = {
		"apps",
		"assets",
		"common_site_config.json",
		"apps.txt",
		"apps.json",
		"currentsite.txt",
	}
	out = []
	for line in (r["stdout"] or "").splitlines():
		name = line.strip()
		if name and name not in noise and not name.endswith((".json", ".txt")) and SITE_RE.match(name):
			out.append(name)
	return out


def list_bench_apps(server_name: str | None = None) -> list[str]:
	r = run_on_bench(server_name, ["ls", "-1", "apps"], timeout_s=30)
	return [l.strip() for l in (r["stdout"] or "").splitlines() if l.strip() and PACKAGE_RE.match(l.strip())]


def _resolve_db_root_password(explicit: str | None = None) -> str:
	if explicit:
		return explicit
	for key in ("MYSQL_ROOT_PASSWORD", "MARIADB_ROOT_PASSWORD", "DO_DB_ROOT_PASSWORD"):
		val = (os.environ.get(key) or "").strip()
		if val:
			return val
	try:
		# Prefer Space Settings encrypted password when workers lack compose env
		settings = frappe.get_single("Space Settings")
		try:
			pwd = settings.get_password("db_root_password")
			if pwd:
				return pwd
		except Exception:
			pass
	except Exception:
		pass
	try:
		# common_site_config / site_config via connected site
		pwd = (frappe.conf.get("db_root_password") or "").strip()
		if pwd:
			return pwd
	except Exception:
		pass
	raise BenchError(
		"DB root password not configured. Set MYSQL_ROOT_PASSWORD on workers "
		"or Space Settings.db_root_password / common_site_config db_root_password."
	)


def new_site(
	server_name: str | None,
	site: str,
	admin_password: str,
	*,
	install_erpnext: bool = True,
	db_root_password: str | None = None,
) -> dict[str, Any]:
	site = _assert_site(site)
	if not admin_password or len(admin_password) < 8:
		raise BenchError("Admin password must be at least 8 characters")
	db_root = _resolve_db_root_password(db_root_password)
	args = [
		"bench",
		"new-site",
		site,
		"--mariadb-user-host-login-scope=%",
		"--db-root-password",
		db_root,
		"--admin-password",
		admin_password,
	]
	if install_erpnext:
		args += ["--install-app", "erpnext"]
	return run_on_bench(server_name, args, timeout_s=60 * 60)


def install_app(server_name: str | None, site: str, pkg: str) -> dict[str, Any]:
	return run_on_bench(
		server_name,
		["bench", "--site", _assert_site(site), "install-app", _assert_pkg(pkg)],
		timeout_s=30 * 60,
	)


def uninstall_app(server_name: str | None, site: str, pkg: str) -> dict[str, Any]:
	return run_on_bench(
		server_name,
		["bench", "--site", _assert_site(site), "uninstall-app", _assert_pkg(pkg), "--yes"],
		timeout_s=30 * 60,
	)


def clear_cache(server_name: str | None, site: str) -> dict[str, Any]:
	return run_on_bench(
		server_name,
		["bench", "--site", _assert_site(site), "clear-cache"],
		timeout_s=120,
	)


def migrate_site(server_name: str | None, site: str) -> dict[str, Any]:
	return run_on_bench(
		server_name,
		["bench", "--site", _assert_site(site), "migrate"],
		timeout_s=60 * 60,
	)


def set_maintenance(server_name: str | None, site: str, on: bool) -> dict[str, Any]:
	val = "1" if on else "0"
	return run_on_bench(
		server_name,
		["bench", "--site", _assert_site(site), "set-maintenance-mode", val],
		timeout_s=60,
	)


def drop_site(server_name: str | None, site: str, db_root_password: str | None = None) -> dict[str, Any]:
	site = _assert_site(site)
	db_root = db_root_password or os.environ.get("MYSQL_ROOT_PASSWORD") or os.environ.get("DO_DB_ROOT_PASSWORD")
	args = ["bench", "drop-site", site, "--force"]
	if db_root:
		args += ["--db-root-password", db_root]
	return run_on_bench(server_name, args, timeout_s=30 * 60)


def list_apps_on_site(server_name: str | None, site: str) -> list[str]:
	r = run_on_bench(server_name, ["bench", "--site", _assert_site(site), "list-apps"], timeout_s=60)
	apps = []
	for line in (r["stdout"] or "").splitlines():
		trimmed = line.strip()
		if not trimmed or trimmed.lower().startswith("app") or set(trimmed) <= {"-"}:
			continue
		pkg = trimmed.split()[0]
		if PACKAGE_RE.match(pkg):
			apps.append(pkg)
	return list(dict.fromkeys(apps))


def get_site_disk_mb(server_name: str | None, site: str) -> int:
	r = run_on_bench(server_name, ["du", "-sm", f"sites/{_assert_site(site)}"], timeout_s=30)
	first = (r["stdout"] or "").strip().split()[0] if r["stdout"] else "0"
	try:
		return max(0, int(first))
	except ValueError:
		return 0


def get_backend_mem(server_name: str | None = None) -> dict[str, Any]:
	server = get_server(server_name)
	container = server.backend_container
	if _same_host(server):
		r = _run(
			["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container],
			20,
		)
	else:
		r = _run_ssh(
			server,
			["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container],
			20,
		)
	raw = (r["stdout"] or "").strip()
	parts = [p.strip() for p in raw.split("/")]
	return {"raw": raw, "used": parts[0] if parts else "", "limit": parts[1] if len(parts) > 1 else ""}
