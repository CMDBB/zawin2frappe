"""Put a fresh ZaWin backup into the local SQL Server.

The practice's ZaWin server drops a nightly full backup, zipped, onto a Windows
share. Everything downstream — the extract, the build, the planning sheet — is
only ever as current as whichever of those has been restored, so refreshing it
is a routine step and not a one-off setup chore. It used to be a paragraph of
README prose and three hand-edited `sqlcmd` invocations; this is that
paragraph, executable.

**This runs on the host, not inside bench.** The share is a Windows drive
mapped into WSL (`Z:` -> `/mnt/z`) and the backup file has to land somewhere
the SQL Server *container* can read, and the Frappe container can see neither.
So the restore is a standalone `zawin` subcommand and the Frappe-side rebuild
is a separate step it can chain to.

    uv run zawin restore --list
    uv run zawin restore                       # pick from the share
    uv run zawin restore --latest --yes --then-build

Configuration, all optional, from the environment or `.env`:

    ZAWIN_BACKUP_DIR         where the nightly zips land       (/mnt/z)
    ZAWIN_RESTORE_WORKDIR    host dir the SQL container reads  (auto-detected)
    ZAWIN_RESTORE_SERVER_DIR that dir's path inside it         (auto-detected)
    ZAWIN_MSSQL_CONTAINER    which container to inspect        (auto-detected)
    ZAWIN_BUILD_COMMAND      what `--then-build` should run
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pymssql

from .db import _load_dotenv, connection_settings

log = logging.getLogger(__name__)

#: Windows share, mounted into WSL. Not in fstab on this machine, so it is
#: mounted by hand and can simply be absent after a WSL restart — which looks
#: exactly like "there are no backups" unless we say otherwise.
DEFAULT_BACKUP_DIR = "/mnt/z"

#: Filename timestamps: ZaWin_xx202608122153.zip
STAMP = re.compile(r"(20\d{2})(\d{2})(\d{2})(\d{2})(\d{2})")


@dataclass(frozen=True)
class Backup:
	"""One nightly backup on the share."""

	path: Path
	size: int
	taken_at: datetime.datetime

	@property
	def label(self) -> str:
		return f"{self.path.name}  {self.taken_at:%Y-%m-%d %H:%M}  {_human(self.size)}"


def _human(size: int) -> str:
	value = float(size)
	for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
		if value < 1024 or unit == "TiB":
			return f"{value:,.1f} {unit}" if unit != "B" else f"{int(value)} B"
		value /= 1024
	return f"{value:,.1f} TiB"


def _taken_at(path: Path) -> datetime.datetime:
	"""When the backup was made: from the filename, else the mtime."""
	match = STAMP.search(path.name)
	if match:
		year, month, day, hour, minute = (int(g) for g in match.groups())
		try:
			return datetime.datetime(year, month, day, hour, minute)
		except ValueError:
			pass
	return datetime.datetime.fromtimestamp(path.stat().st_mtime)


# ---------------------------------------------------------------------------
# The share
# ---------------------------------------------------------------------------


def _env(key: str, default: str | None = None) -> str | None:
	"""Read a setting, honouring `.env`.

	`connection_settings()` loads `.env` as a side effect, but the restore
	reads its own settings before it ever opens a connection — so it has to ask
	for the file itself rather than rely on something else having done it.
	"""
	_load_dotenv()
	return os.environ.get(key, default)


def backup_dir() -> Path:
	return Path(_env("ZAWIN_BACKUP_DIR", DEFAULT_BACKUP_DIR) or DEFAULT_BACKUP_DIR)


def available(directory: Path | None = None) -> list[Backup]:
	"""Backups on the share, newest first."""
	directory = directory or backup_dir()
	if not directory.is_dir():
		raise FileNotFoundError(
			f"{directory} is not mounted. The backups live on the Windows Z: drive, "
			f"which WSL does not mount automatically:\n"
			f"    sudo mkdir -p {directory} && sudo mount -t drvfs Z: {directory}"
		)
	found = [
		Backup(path=path, size=path.stat().st_size, taken_at=_taken_at(path))
		for path in directory.iterdir()
		if path.is_file() and path.suffix.lower() in {".zip", ".bak"}
	]
	return sorted(found, key=lambda b: b.taken_at, reverse=True)


# ---------------------------------------------------------------------------
# Where the SQL Server container can read from
# ---------------------------------------------------------------------------


def _docker_ps() -> list[dict]:
	try:
		out = subprocess.run(
			["docker", "ps", "--format", "{{json .}}"],
			capture_output=True,
			text=True,
			check=True,
			timeout=30,
		).stdout
	except OSError, subprocess.SubprocessError:
		return []
	return [json.loads(line) for line in out.splitlines() if line.strip()]


def find_container() -> str | None:
	"""Which container is the SQL Server we are configured to talk to.

	`MSSQL_HOST` is usually the container's *name*, because the setting exists
	for the Frappe container's benefit and containers address each other that
	way. Failing that, match on a published port. Either way the answer is the
	container this particular restore is about, rather than "some mssql image".
	"""
	if name := _env("ZAWIN_MSSQL_CONTAINER"):
		return name
	running = _docker_ps()
	settings = connection_settings()
	host = str(settings["server"])
	for row in running:
		if row.get("Names") == host:
			return host
	port = str(settings["port"])
	for row in running:
		if f":{port}->" in row.get("Ports", ""):
			return row.get("Names")
	return None


def _reachable(host: str, port: int) -> bool:
	try:
		with socket.create_connection((host, port), timeout=1):
			return True
	except OSError:
		return False


def _published_port(container: str, internal: int) -> int | None:
	try:
		out = subprocess.run(
			["docker", "port", container, f"{internal}/tcp"],
			capture_output=True,
			text=True,
			check=True,
			timeout=30,
		).stdout
	except OSError, subprocess.SubprocessError:
		return None
	for line in out.splitlines():
		_, _, tail = line.rpartition(":")
		if tail.strip().isdigit():
			return int(tail.strip())
	return None


def host_connection_settings() -> dict:
	"""The same database, addressed from this machine rather than from bench.

	`site_config.json` names the container and its *internal* port, which is
	right for the Frappe container and useless here: on the host that name does
	not resolve. So when the configured address is unreachable, the same
	container's published port is looked up and used instead. This is the whole
	reason the restore is not a bench command — the two processes cannot even
	reach the database the same way.
	"""
	settings = dict(connection_settings())
	if _reachable(str(settings["server"]), int(settings["port"])):
		return settings
	container = find_container()
	published = _published_port(container, int(settings["port"])) if container else None
	if published is None:
		raise RuntimeError(
			f"cannot reach SQL Server at {settings['server']}:{settings['port']} from this "
			"machine, and no published port was found. Set MSSQL_HOST/MSSQL_PORT in .env to "
			"an address this host can reach."
		)
	log.debug("%s:%s unreachable here; using 127.0.0.1:%s", settings["server"], settings["port"], published)
	settings["server"] = "127.0.0.1"
	settings["port"] = published
	return settings


def staging_dirs() -> tuple[Path, str]:
	"""Where to put the .bak: (host path, the same place inside the container).

	Read off the container's bind mounts, so there is nothing to keep in sync
	by hand. `ZAWIN_RESTORE_WORKDIR` / `ZAWIN_RESTORE_SERVER_DIR` override.
	"""
	host = _env("ZAWIN_RESTORE_WORKDIR")
	server = _env("ZAWIN_RESTORE_SERVER_DIR")
	if host and server:
		return Path(host), server

	container = find_container()
	if not container:
		raise RuntimeError(
			"cannot find the SQL Server container. Set ZAWIN_RESTORE_WORKDIR (a host "
			"directory it bind-mounts) and ZAWIN_RESTORE_SERVER_DIR (that directory's "
			"path inside it), or ZAWIN_MSSQL_CONTAINER."
		)
	try:
		out = subprocess.run(
			["docker", "inspect", container, "--format", "{{json .Mounts}}"],
			capture_output=True,
			text=True,
			check=True,
			timeout=30,
		).stdout
	except (OSError, subprocess.SubprocessError) as exc:
		raise RuntimeError(f"cannot inspect container {container!r}: {exc}") from exc

	binds = [m for m in json.loads(out) if m.get("Type") == "bind" and m.get("RW")]
	if len(binds) != 1:
		raise RuntimeError(
			f"container {container!r} has {len(binds)} writable bind mounts; "
			"set ZAWIN_RESTORE_WORKDIR and ZAWIN_RESTORE_SERVER_DIR to say which to use."
		)
	return Path(host or binds[0]["Source"]), server or binds[0]["Destination"]


# ---------------------------------------------------------------------------
# Unpacking
# ---------------------------------------------------------------------------


def unpack(backup: Backup, into: Path, *, reuse: bool = True) -> Path:
	"""Extract the .bak out of a nightly zip. Returns the .bak's host path."""
	into.mkdir(parents=True, exist_ok=True)
	if backup.path.suffix.lower() == ".bak":
		return backup.path

	with zipfile.ZipFile(backup.path) as archive:
		members = [m for m in archive.infolist() if m.filename.lower().endswith(".bak")]
		if not members:
			raise RuntimeError(f"{backup.path.name} contains no .bak file")
		if len(members) > 1:
			raise RuntimeError(f"{backup.path.name} contains {len(members)} .bak files")
		member = members[0]
		target = into / Path(member.filename).name

		if reuse and target.exists() and target.stat().st_size == member.file_size:
			log.info("reusing %s (already unpacked, %s)", target.name, _human(member.file_size))
			return target

		free = shutil.disk_usage(into).free
		if free < member.file_size * 1.05:
			raise RuntimeError(f"{into} has {_human(free)} free; unpacking needs {_human(member.file_size)}")

		log.info("unpacking %s -> %s (%s)", backup.path.name, target, _human(member.file_size))
		started = time.monotonic()
		with archive.open(member) as source, open(target, "wb") as sink:
			shutil.copyfileobj(source, sink, length=8 * 1024 * 1024)
		log.info("unpacked in %.0fs", time.monotonic() - started)
	return target


# ---------------------------------------------------------------------------
# The restore itself
# ---------------------------------------------------------------------------


def _connect(database: str = "master"):
	settings = host_connection_settings()
	settings["database"] = database
	# RESTORE cannot run inside a transaction, and an 8 GB one takes minutes.
	return pymssql.connect(**settings, autocommit=True, timeout=0, login_timeout=30)


def current() -> dict | None:
	"""Which backup the live database was restored from, per SQL Server's own log."""
	database = str(connection_settings()["database"])
	sql = """
		SELECT TOP 1 bs.backup_finish_date, bmf.physical_device_name, rh.restore_date
		FROM msdb.dbo.restorehistory rh
		JOIN msdb.dbo.backupset bs ON bs.backup_set_id = rh.backup_set_id
		JOIN msdb.dbo.backupmediafamily bmf ON bmf.media_set_id = bs.media_set_id
		WHERE rh.destination_database_name = %s
		ORDER BY rh.restore_date DESC
	"""
	try:
		with _connect() as conn:
			cursor = conn.cursor(as_dict=True)
			cursor.execute(sql, (database,))
			return cursor.fetchone()
	except Exception as exc:  # msdb is wiped whenever the container is recreated
		log.debug("no restore history: %s", exc)
		return None


def _watch(conn_factory, stop: threading.Event) -> None:
	"""Log the server's own progress, so a ten-minute restore is not silent."""
	sql = """
		SELECT percent_complete, estimated_completion_time / 1000 AS seconds_left
		FROM sys.dm_exec_requests WHERE command LIKE 'RESTORE%'
	"""
	while not stop.wait(15):
		try:
			with conn_factory() as conn:
				cursor = conn.cursor()
				cursor.execute(sql)
				row = cursor.fetchone()
			if row:
				log.info("  restoring: %.0f%% (about %ds left)", row[0], row[1])
		except Exception:  # progress is a courtesy, never a failure
			return


def restore(bak_on_server: str, *, database: str | None = None) -> None:
	"""Replace `database` with the contents of a .bak the server can read."""
	database = database or str(connection_settings()["database"])

	with _connect() as conn:
		cursor = conn.cursor(as_dict=True)
		cursor.execute(f"RESTORE FILELISTONLY FROM DISK = N'{bak_on_server}'")
		files = cursor.fetchall()
		if not files:
			raise RuntimeError(f"{bak_on_server} lists no files — is it a full backup?")

		cursor.execute(
			"SELECT CAST(SERVERPROPERTY('InstanceDefaultDataPath') AS nvarchar(4000)) AS data,"
			" CAST(SERVERPROPERTY('InstanceDefaultLogPath') AS nvarchar(4000)) AS log"
		)
		paths = cursor.fetchone()

		moves = []
		for entry in files:
			suffix = ".ldf" if entry["Type"] == "L" else ".mdf"
			root = paths["log" if entry["Type"] == "L" else "data"]
			# Name the files after the *database*, not after the logical names in
			# the backup: the same restore then always lands on the same paths and
			# repeated restores cannot accumulate stray data files.
			stem = database if entry["Type"] != "L" else f"{database}_log"
			if len([f for f in files if f["Type"] == entry["Type"]]) > 1:
				stem = f"{stem}_{entry['LogicalName']}"
			moves.append(f"MOVE N'{entry['LogicalName']}' TO N'{root}{stem}{suffix}'")

		# Any open connection blocks an exclusive restore, and the extract may
		# well have left one. Kicking them off is safe: this database is a
		# read-only copy of the practice's, and nothing writes to it.
		log.info("taking %s offline", database)
		try:
			cursor.execute(f"ALTER DATABASE [{database}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
		except Exception as exc:
			log.debug("no existing database to close (%s)", exc)

		stop = threading.Event()
		watcher = threading.Thread(target=_watch, args=(_connect, stop), daemon=True)
		watcher.start()
		started = time.monotonic()
		try:
			log.info("restoring %s from %s", database, bak_on_server)
			cursor.execute(
				f"RESTORE DATABASE [{database}] FROM DISK = N'{bak_on_server}' "
				f"WITH REPLACE, RECOVERY, STATS = 5, " + ", ".join(moves)
			)
		finally:
			stop.set()
		log.info("restored in %.0fs", time.monotonic() - started)

		try:
			cursor.execute(f"ALTER DATABASE [{database}] SET MULTI_USER")
		except Exception as exc:
			log.warning("could not return %s to multi-user: %s", database, exc)


def horizon() -> dict:
	"""How far the restored agenda reaches. Counts and dates only — no content."""
	sql = """
		SELECT COUNT(*) AS rows_total,
		       MIN(Datum) AS first_date,
		       MAX(Datum) AS last_date,
		       SUM(CASE WHEN Datum >= CAST(GETDATE() AS date) THEN 1 ELSE 0 END) AS rows_ahead
		FROM TAGPLANTERMIN
	"""
	with _connect(str(connection_settings()["database"])) as conn:
		cursor = conn.cursor(as_dict=True)
		cursor.execute(sql)
		return cursor.fetchone()


def rebuild() -> int:
	"""Run whatever `ZAWIN_BUILD_COMMAND` says, so the restore can chain into it.

	Deliberately a command string rather than an in-process call: the build
	writes into a Frappe site, which lives in a different container from the
	one this runs in.
	"""
	command = _env("ZAWIN_BUILD_COMMAND")
	if not command:
		raise RuntimeError(
			"ZAWIN_BUILD_COMMAND is not set. Put the rebuild command in .env, e.g.\n"
			'    ZAWIN_BUILD_COMMAND="docker exec <frappe-container> '
			'bench --site <site> zawin-build"'
		)
	log.info("running: %s", command)
	return subprocess.run(command, shell=True, check=False).returncode
