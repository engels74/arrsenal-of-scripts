#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "requests",
#     "uptime-kuma-api",
# ]
# ///
#
# -----------------------------------------------------------------------------
# overengineered-backup-script.py
#
# A robust, automated backup and off-site upload script using Python.
#
# Features:
# - Creates local encrypted backups via a streaming tar -> pigz/gzip -> age
#   pipeline (no temporary uncompressed archive on disk).
# - Manages Docker services, with priority restart for critical containers.
# - Uploads backups to a cloud remote using rclone.
# - Sends detailed status notifications to a Discord webhook.
# - Optionally uploads full logs to PrivateBin for easy debugging.
# - Robust error handling, log rotation, concurrency locking, and a
#   built-in restore mode (supports legacy openssl-encrypted backups too).
#
# Usage:
#   sudo ./overengineered-backup-script.py [--config /etc/backup-script.toml]
#   ./overengineered-backup-script.py --dry-run --verbose
#   ./overengineered-backup-script.py --print-default-config > /etc/backup-script.toml
#   ./overengineered-backup-script.py restore <backup-file> --list
#   ./overengineered-backup-script.py restore <backup-file> --output-dir /restore/here
#
# Requirements:
# - Python 3.14+ (run via `uv run`; dependencies resolve automatically)
# - rclone, tar (GNU), pigz/gzip, age, docker
# - openssl (only needed to restore legacy .enc backups)
# - (Optional) privatebin (from gearnode/privatebin)
#
# Encryption key setup (one time):
#   age-keygen -o /root/.backup_age_key.txt && chmod 600 /root/.backup_age_key.txt
#   !! Copy the key somewhere safe OFF this machine - if the key is lost,
#   !! every backup encrypted with it is permanently unreadable.
# -----------------------------------------------------------------------------

import argparse
import atexit
import contextlib
import datetime
import functools
import grp
import hashlib
import json
import logging
import os
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from types import FrameType
from typing import IO, TYPE_CHECKING, BinaryIO, Callable, Self, TypedDict, cast

__version__ = "2.0.0"

# Optional dependency: only needed for Discord notifications. When running
# via `uv run` it is always present; a plain interpreter without it still
# works (notifications get disabled during pre-flight).
try:
    import requests  # pyright: ignore[reportMissingModuleSource]
except ImportError:
    requests = None

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Optional dependency: Uptime Kuma maintenance windows.
try:
    from uptime_kuma_api import UptimeKumaApi, MaintenanceStrategy  # pyright: ignore[reportMissingImports, reportUnknownVariableType]
except ImportError:
    UptimeKumaApi = None
    MaintenanceStrategy = None

if TYPE_CHECKING:
    from uptime_kuma_api import UptimeKumaApi as KumaApiT  # pyright: ignore[reportMissingImports]


# -----------------------------------------------------------------------------
# Custom Exceptions
# -----------------------------------------------------------------------------


class BackupError(Exception):
    """Base exception for backup-related errors."""


class ConfigError(BackupError):
    """Raised when the configuration file is invalid."""


class PreFlightError(BackupError):
    """Raised when pre-flight checks fail (clean exit, no finalization)."""


class BackupCreationError(BackupError):
    """Raised when backup creation fails."""


class BackupVerificationError(BackupError):
    """Raised when backup verification fails."""


class OperationTimeout(BackupError):
    """Raised when an operation times out."""


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path("/etc/backup-script.toml")

# Environment variables that override config-file secrets.
ENV_UPTIME_KUMA_PASSWORD = "BACKUP_UPTIME_KUMA_PASSWORD"
ENV_DISCORD_WEBHOOK_URL = "BACKUP_DISCORD_WEBHOOK_URL"

# Fixed operation timeouts (seconds).
BACKUP_CREATE_TIMEOUT = 7200
BACKUP_VERIFY_TIMEOUT = 1800
RESTORE_TIMEOUT = 7200


@dataclass(slots=True)
class Config:
    """All tunable settings. Defaults here are overridden by the TOML config
    file, which is in turn overridden by environment variables (secrets) and
    CLI flags."""

    # [paths]
    backup_root_dir: Path = Path("/dir/to/backups")
    log_root_dir: Path = Path("/home/user/scripts/logs/backupScript")
    lock_file: Path = Path("/tmp/overengineered-backup-script.lock")

    # [retention]
    retention_backups: int = 3
    retention_logs: int = 7

    # [backup]
    backup_sources: list[Path] = field(
        default_factory=lambda: [
            Path("/etc/logrotate.d"),
            Path("/root/.config"),
            Path("/root/.ssh"),
            Path("/home/aplex/.config"),
            Path("/home/aplex/.ssh"),
            Path("/home/aplex/scripts"),
            Path("/home/aplex/stacks"),
            Path("/data/scripts"),
            Path("/opt/docker-all"),
            Path("/etc/fail2ban"),
            Path("/etc/ssh"),
            Path("/var/lib/docker/volumes"),
        ]
    )
    backup_exclusions: list[Path] = field(
        default_factory=lambda: [Path("/opt/docker-all/miscSoftware/immich")]
    )
    backup_user: str = "aplex"
    backup_group: str = "aplex"
    compression_tool: str = "pigz"
    compression_level: int = 3

    # [encryption]
    age_identity_file: Path = Path("/root/.backup_age_key.txt")
    legacy_password_file: Path = Path("/root/.backup_password")

    # [docker]
    docker_enabled: bool = True
    docker_shutdown_method: str = "down"
    docker_stacks_dir: Path = Path("/home/user/stacks")
    docker_compose_timeout: int = 300
    docker_start_settle_delay: int = 30
    docker_force_stop_timeout: int = 30
    docker_shutdown_overall_timeout: int = 300
    docker_shutdown_max_retries: int = 3
    docker_shutdown_retry_delay: int = 10
    docker_verify_shutdown_interval: int = 2
    docker_kill_wait_time: int = 5
    plex_data_dir: Path = Path("/opt/docker-all/mediaServers/plex")
    plex_compose_file: Path = Path("/home/user/stacks/006-plex-media-server/compose.yaml")

    # [rclone]
    rclone_enabled: bool = True
    rclone_remote_dest: str = "pcrypt:backup"
    rclone_bandwidth_limit: str = "50M"
    rclone_filters: list[str] = field(default_factory=list)
    rclone_max_retries: int = 5
    rclone_retry_delay: int = 30
    rclone_retry_max_delay: int = 300
    rclone_overall_timeout: int = 7200

    # [discord]
    discord_webhook_url: str = ""
    discord_username: str = "Server Backup & Sync"
    discord_avatar_url: str = ""

    # [privatebin]
    privatebin_enabled: bool = True
    privatebin_cli_path: str = "privatebin"

    # [uptime_kuma]
    uptime_kuma_enabled: bool = True
    uptime_kuma_url: str = "https://uptimekuma.cccp.ps"
    uptime_kuma_username: str = "engels74"
    uptime_kuma_password: str = ""
    uptime_kuma_status_page_slug: str = "cccp-ps"

    # [limits]
    min_free_space_gb: int = 50
    min_free_space_percent: int = 10
    script_overall_timeout: int = 14400


# Maps TOML "[section] key" pairs to Config attribute names. Unknown sections
# or keys in the config file are a hard error (catches typos early).
_TOML_SCHEMA: dict[str, dict[str, str]] = {
    "paths": {
        "backup_root_dir": "backup_root_dir",
        "log_root_dir": "log_root_dir",
        "lock_file": "lock_file",
    },
    "retention": {
        "backups": "retention_backups",
        "logs": "retention_logs",
    },
    "backup": {
        "sources": "backup_sources",
        "exclusions": "backup_exclusions",
        "owner_user": "backup_user",
        "owner_group": "backup_group",
        "compression_tool": "compression_tool",
        "compression_level": "compression_level",
    },
    "encryption": {
        "age_identity_file": "age_identity_file",
        "legacy_password_file": "legacy_password_file",
    },
    "docker": {
        "enabled": "docker_enabled",
        "shutdown_method": "docker_shutdown_method",
        "stacks_dir": "docker_stacks_dir",
        "compose_timeout": "docker_compose_timeout",
        "start_settle_delay": "docker_start_settle_delay",
        "force_stop_timeout": "docker_force_stop_timeout",
        "shutdown_overall_timeout": "docker_shutdown_overall_timeout",
        "shutdown_max_retries": "docker_shutdown_max_retries",
        "shutdown_retry_delay": "docker_shutdown_retry_delay",
        "verify_shutdown_interval": "docker_verify_shutdown_interval",
        "kill_wait_time": "docker_kill_wait_time",
        "plex_data_dir": "plex_data_dir",
        "plex_compose_file": "plex_compose_file",
    },
    "rclone": {
        "enabled": "rclone_enabled",
        "remote_dest": "rclone_remote_dest",
        "bandwidth_limit": "rclone_bandwidth_limit",
        "filters": "rclone_filters",
        "max_retries": "rclone_max_retries",
        "retry_delay": "rclone_retry_delay",
        "retry_max_delay": "rclone_retry_max_delay",
        "overall_timeout": "rclone_overall_timeout",
    },
    "discord": {
        "webhook_url": "discord_webhook_url",
        "username": "discord_username",
        "avatar_url": "discord_avatar_url",
    },
    "privatebin": {
        "enabled": "privatebin_enabled",
        "cli_path": "privatebin_cli_path",
    },
    "uptime_kuma": {
        "enabled": "uptime_kuma_enabled",
        "url": "uptime_kuma_url",
        "username": "uptime_kuma_username",
        "password": "uptime_kuma_password",
        "status_page_slug": "uptime_kuma_status_page_slug",
    },
    "limits": {
        "min_free_space_gb": "min_free_space_gb",
        "min_free_space_percent": "min_free_space_percent",
        "script_overall_timeout": "script_overall_timeout",
    },
}

_PATH_LIST_ATTRS = {"backup_sources", "backup_exclusions"}


def _apply_toml(cfg: Config, data: dict[str, object], source: Path) -> None:
    """Apply a parsed TOML document onto a Config, validating every key."""
    for section, table in data.items():
        if section not in _TOML_SCHEMA:
            raise ConfigError(f"{source}: unknown section [{section}]")
        if not isinstance(table, dict):
            raise ConfigError(f"{source}: [{section}] must be a table")
        section_schema = _TOML_SCHEMA[section]
        for key, value in cast(dict[str, object], table).items():
            attr = section_schema.get(key)
            if attr is None:
                raise ConfigError(f"{source}: unknown key '{key}' in [{section}]")
            default = getattr(cfg, attr)
            if isinstance(default, Path):
                if not isinstance(value, str):
                    raise ConfigError(f"{source}: [{section}] {key} must be a string path")
                setattr(cfg, attr, Path(value))
            elif isinstance(default, bool):
                if not isinstance(value, bool):
                    raise ConfigError(f"{source}: [{section}] {key} must be a boolean")
                setattr(cfg, attr, value)
            elif isinstance(default, int):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ConfigError(f"{source}: [{section}] {key} must be an integer")
                setattr(cfg, attr, value)
            elif isinstance(default, str):
                if not isinstance(value, str):
                    raise ConfigError(f"{source}: [{section}] {key} must be a string")
                setattr(cfg, attr, value)
            elif isinstance(default, list):
                if not isinstance(value, list) or not all(
                    isinstance(v, str) for v in cast(list[object], value)
                ):
                    raise ConfigError(
                        f"{source}: [{section}] {key} must be an array of strings"
                    )
                str_values = cast(list[str], value)
                if attr in _PATH_LIST_ATTRS:
                    setattr(cfg, attr, [Path(v) for v in str_values])
                else:
                    setattr(cfg, attr, list(str_values))
            else:  # pragma: no cover - schema and dataclass out of sync
                raise ConfigError(f"{source}: unsupported config type for {attr}")


def load_config(path: Path | None) -> Config:
    """Load configuration: defaults -> TOML file -> environment secrets.

    A missing file at the default path is fine (defaults apply); a missing
    file at an explicitly given path is an error.
    """
    cfg = Config()
    config_path = path or DEFAULT_CONFIG_PATH

    if config_path.is_file():
        try:
            with open(config_path, "rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"Failed to parse {config_path}: {e}") from e
        _apply_toml(cfg, cast(dict[str, object], data), config_path)
        log.info(f"Loaded configuration from {config_path}")
    elif path is not None:
        raise ConfigError(f"Config file not found: {path}")
    else:
        log.info(
            f"No config file at {DEFAULT_CONFIG_PATH}; using built-in defaults. "
            f"Generate one with --print-default-config."
        )

    if kuma_password := os.environ.get(ENV_UPTIME_KUMA_PASSWORD):
        cfg.uptime_kuma_password = kuma_password
    if webhook_url := os.environ.get(ENV_DISCORD_WEBHOOK_URL):
        cfg.discord_webhook_url = webhook_url

    return cfg


def default_config_toml() -> str:
    """Render a fully-commented example config populated with the defaults."""
    c = Config()

    def path_array(items: list[Path]) -> str:
        if not items:
            return "[]"
        inner = ",\n    ".join(f'"{p}"' for p in items)
        return f"[\n    {inner},\n]"

    def str_array(items: list[str]) -> str:
        if not items:
            return "[]"
        inner = ", ".join(f'"{s}"' for s in items)
        return f"[{inner}]"

    def b(v: bool) -> str:
        return "true" if v else "false"

    return f"""\
# Configuration for overengineered-backup-script.py
# Default location: {DEFAULT_CONFIG_PATH} (override with --config PATH).
#
# Secrets can also be provided via environment variables, which take
# precedence over values in this file:
#   {ENV_UPTIME_KUMA_PASSWORD}
#   {ENV_DISCORD_WEBHOOK_URL}

[paths]
backup_root_dir = "{c.backup_root_dir}"
log_root_dir = "{c.log_root_dir}"
lock_file = "{c.lock_file}"

[retention]
# How many backups / log files to keep.
backups = {c.retention_backups}
logs = {c.retention_logs}

[backup]
# Directories to include in the backup.
sources = {path_array(c.backup_sources)}
# Directories to exclude.
exclusions = {path_array(c.backup_exclusions)}
# Ownership applied to the finished backup file.
owner_user = "{c.backup_user}"
owner_group = "{c.backup_group}"
# "pigz" (parallel) or "gzip".
compression_tool = "{c.compression_tool}"
compression_level = {c.compression_level}

[encryption]
# age identity (private key) used to encrypt and decrypt backups.
# Generate with: age-keygen -o {c.age_identity_file} && chmod 600 {c.age_identity_file}
# KEEP A COPY OF THIS KEY OFF THIS MACHINE - lost key means unreadable backups.
age_identity_file = "{c.age_identity_file}"
# Password file for restoring legacy openssl-encrypted (.enc) backups.
legacy_password_file = "{c.legacy_password_file}"

[docker]
enabled = {b(c.docker_enabled)}
# "down" or "stop" - how compose stacks are shut down.
shutdown_method = "{c.docker_shutdown_method}"
stacks_dir = "{c.docker_stacks_dir}"
# Per-stack timeout for `docker compose` operations (seconds).
compose_timeout = {c.docker_compose_timeout}
# Single settle delay after starting a batch of stacks (seconds).
start_settle_delay = {c.docker_start_settle_delay}
force_stop_timeout = {c.docker_force_stop_timeout}
shutdown_overall_timeout = {c.docker_shutdown_overall_timeout}
shutdown_max_retries = {c.docker_shutdown_max_retries}
shutdown_retry_delay = {c.docker_shutdown_retry_delay}
verify_shutdown_interval = {c.docker_verify_shutdown_interval}
kill_wait_time = {c.docker_kill_wait_time}
# Plex gets restarted first, before backup verification and rclone sync.
plex_data_dir = "{c.plex_data_dir}"
plex_compose_file = "{c.plex_compose_file}"

[rclone]
enabled = {b(c.rclone_enabled)}
remote_dest = "{c.rclone_remote_dest}"
bandwidth_limit = "{c.rclone_bandwidth_limit}"
# Optional rclone filter rules (passed via --filter-from).
filters = {str_array(c.rclone_filters)}
max_retries = {c.rclone_max_retries}
retry_delay = {c.rclone_retry_delay}
retry_max_delay = {c.rclone_retry_max_delay}
overall_timeout = {c.rclone_overall_timeout}

[discord]
# Leave empty to disable Discord notifications.
webhook_url = "{c.discord_webhook_url}"
username = "{c.discord_username}"
avatar_url = "{c.discord_avatar_url}"

[privatebin]
enabled = {b(c.privatebin_enabled)}
cli_path = "{c.privatebin_cli_path}"

[uptime_kuma]
enabled = {b(c.uptime_kuma_enabled)}
url = "{c.uptime_kuma_url}"
username = "{c.uptime_kuma_username}"
# Prefer the {ENV_UPTIME_KUMA_PASSWORD} environment variable over this.
password = "{c.uptime_kuma_password}"
status_page_slug = "{c.uptime_kuma_status_page_slug}"

[limits]
min_free_space_gb = {c.min_free_space_gb}
min_free_space_percent = {c.min_free_space_percent}
# Hard cap for the entire script run (seconds); enforced by a watchdog.
script_overall_timeout = {c.script_overall_timeout}
"""


# -----------------------------------------------------------------------------
# Type Definitions for Uptime Kuma
# -----------------------------------------------------------------------------


class ServerInfo(TypedDict):
    serverTimezone: str


class MaintenanceResponse(TypedDict):
    maintenanceID: int


class Monitor(TypedDict):
    id: int
    name: str


class StatusPage(TypedDict):
    id: int
    slug: str
    title: str


class MonitorId(TypedDict):
    id: int


class DeleteMaintenanceResponse(TypedDict):
    msg: str


MonitorList = list[Monitor]
StatusPageList = list[StatusPage]
MonitorIdList = list[MonitorId]


if TYPE_CHECKING:
    JsonDict = dict[
        str, str | int | float | bool | None | "JsonDict" | list["JsonDict"]
    ]
else:
    JsonDict = dict


# -----------------------------------------------------------------------------
# Backup State Tracking
# -----------------------------------------------------------------------------


class BackupStage(Enum):
    """Tracks the current stage of the backup process."""

    INIT = auto()
    PREFLIGHT = auto()
    MAINTENANCE_WINDOW = auto()
    CONTAINER_SHUTDOWN = auto()
    BACKUP_CREATION = auto()
    BACKUP_VERIFICATION = auto()
    CONTAINER_RESTART_PLEX = auto()
    RCLONE_SYNC = auto()
    CONTAINER_RESTART_ALL = auto()
    CLEANUP = auto()
    COMPLETE = auto()


@dataclass(slots=True)
class BackupState:
    """Maintains state throughout the backup process for proper recovery."""

    stage: BackupStage = BackupStage.INIT
    failed_stage: BackupStage | None = None
    containers_stopped: bool = False
    plex_started: bool = False
    other_services_started: bool = False
    maintenance_window_id: int | None = None
    backup_file: Path | None = None
    backup_created: bool = False
    backup_verified: bool = False
    rclone_completed: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    start_time: float = field(default_factory=time.monotonic)

    def add_error(self, error: str) -> None:
        if self.failed_stage is None:
            self.failed_stage = self.stage
        self.errors.append(f"[{self.stage.name}] {error}")

    def add_warning(self, warning: str) -> None:
        self.warnings.append(f"[{self.stage.name}] {warning}")

    @property
    def elapsed_time(self) -> float:
        return time.monotonic() - self.start_time

    @property
    def has_critical_errors(self) -> bool:
        return len(self.errors) > 0


# -----------------------------------------------------------------------------
# Global Variables
# -----------------------------------------------------------------------------

log = logging.getLogger(__name__)
CONFIG = Config()
dry_run_mode = False
backup_state = BackupState()
shutdown_requested = False


# -----------------------------------------------------------------------------
# External Command Resolution
# -----------------------------------------------------------------------------
# Under sudo, PATH is reset to a minimal "secure_path" that excludes
# Homebrew/Linuxbrew directories, so brew-installed tools (rclone, pigz, age,
# ...) are not found. Fall back to the standard brew locations when needed.

HOMEBREW_FALLBACK_DIRS = [
    Path("/home/linuxbrew/.linuxbrew/bin"),  # Linuxbrew (system-wide)
    Path("/home/linuxbrew/.linuxbrew/sbin"),
    Path("/opt/homebrew/bin"),  # Homebrew (macOS Apple Silicon)
    Path("/opt/homebrew/sbin"),
    Path("/usr/local/bin"),  # Homebrew (macOS Intel) / manual installs
    Path("/usr/local/sbin"),
]


def _command_search_dirs() -> list[Path]:
    """Candidate directories for binaries missing from the (sudo) PATH."""
    dirs: list[Path] = []

    # An explicit Homebrew prefix wins if it survived the environment.
    brew_prefix = os.environ.get("HOMEBREW_PREFIX")
    if brew_prefix:
        dirs.extend([Path(brew_prefix) / "bin", Path(brew_prefix) / "sbin"])

    dirs.extend(HOMEBREW_FALLBACK_DIRS)

    # Per-user Linuxbrew install (~/.linuxbrew) of the user who invoked sudo.
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            sudo_home = Path(pwd.getpwnam(sudo_user).pw_dir)
            dirs.extend([sudo_home / ".linuxbrew/bin", sudo_home / ".linuxbrew/sbin"])
        except KeyError:
            pass

    return [d for d in dirs if d.is_dir()]


@functools.lru_cache(maxsize=None)
def find_command(name: str) -> str | None:
    """Locate a command on PATH, falling back to Homebrew/Linuxbrew dirs."""
    found = shutil.which(name)
    if found:
        return found

    fallback_path = os.pathsep.join(str(d) for d in _command_search_dirs())
    if fallback_path:
        found = shutil.which(name, path=fallback_path)
        if found:
            log.info(f"'{name}' not on PATH; using brew fallback: {found}")
            return found
    return None


def resolve_command(name: str) -> str:
    """Return the resolved absolute path for a command, or the bare name."""
    return find_command(name) or name


def resolve_tar() -> str:
    """Prefer GNU tar. On macOS/BSD systems the default tar lacks the GNU
    flags this script uses, but Homebrew installs GNU tar as 'gtar'."""
    return find_command("gtar") or resolve_command("tar")


# -----------------------------------------------------------------------------
# Subprocess Tracking (signal-responsive execution)
# -----------------------------------------------------------------------------
# Every long-running subprocess is registered here so a termination signal or
# the watchdog can interrupt it immediately, instead of waiting hours for the
# subprocess to finish on its own.

_active_processes: set[subprocess.Popen[bytes] | subprocess.Popen[str]] = set()
_process_lock = threading.Lock()


def _track_process(proc: subprocess.Popen[bytes] | subprocess.Popen[str]) -> None:
    with _process_lock:
        _active_processes.add(proc)


def _untrack_process(proc: subprocess.Popen[bytes] | subprocess.Popen[str]) -> None:
    with _process_lock:
        _active_processes.discard(proc)


def terminate_active_processes() -> None:
    """Terminate all currently tracked subprocesses (signal handler safe)."""
    with _process_lock:
        procs = list(_active_processes)
    for proc in procs:
        with contextlib.suppress(OSError):
            proc.terminate()


def run_command(
    command: list[str],
    timeout: float,
    stdin: IO[bytes] | int | None = None,
) -> subprocess.CompletedProcess[str]:
    """subprocess.run() equivalent that registers the process for signal
    handling and captures output as text."""
    proc = subprocess.Popen(
        command,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _track_process(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        _ = proc.communicate()
        raise
    finally:
        _untrack_process(proc)
    return subprocess.CompletedProcess(command, proc.returncode, stdout or "", stderr or "")


@dataclass(slots=True)
class PipelineResult:
    returncodes: list[int]
    stderr: list[str]


def run_pipeline(
    stages: list[tuple[str, list[str]]],
    timeout: float,
    stdin_first: BinaryIO | None = None,
    stdout_final: int | IO[bytes] | None = subprocess.DEVNULL,
) -> PipelineResult:
    """Run a chain of processes connected by pipes (like `a | b | c`).

    Each stage's stderr goes to its own temp file (read back afterwards), so
    a noisy failure can never deadlock the pipe buffer. All processes are
    tracked for signal handling. Raises OperationTimeout if the pipeline does
    not finish within `timeout` seconds.
    """
    procs: list[subprocess.Popen[bytes]] = []
    with contextlib.ExitStack() as stack:
        errdir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="pipeline-err-")))
        err_files: list[IO[bytes]] = []

        prev_stdout: IO[bytes] | None = stdin_first
        for i, (name, cmd) in enumerate(stages):
            err_file = stack.enter_context(open(errdir / f"{i}-{name}.stderr", "w+b"))
            err_files.append(err_file)
            is_last = i == len(stages) - 1
            proc = subprocess.Popen(
                cmd,
                stdin=prev_stdout,
                stdout=stdout_final if is_last else subprocess.PIPE,
                stderr=err_file,
            )
            procs.append(proc)
            _track_process(proc)
            # Close the parent's copy of the previous stage's stdout so EOF
            # propagates correctly through the pipeline.
            if i > 0:
                prev_proc_stdout = procs[i - 1].stdout
                if prev_proc_stdout is not None:
                    prev_proc_stdout.close()
            prev_stdout = proc.stdout

        try:
            deadline = time.monotonic() + timeout
            for proc in reversed(procs):
                remaining = max(1.0, deadline - time.monotonic())
                _ = proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            for proc in procs:
                with contextlib.suppress(OSError):
                    proc.kill()
            for proc in procs:
                with contextlib.suppress(Exception):
                    _ = proc.wait(timeout=10)
            raise OperationTimeout(
                f"Pipeline ({' | '.join(name for name, _ in stages)}) "
                f"timed out after {timeout} seconds"
            )
        finally:
            for proc in procs:
                _untrack_process(proc)

        stderr_texts: list[str] = []
        for err_file in err_files:
            err_file.flush()
            _ = err_file.seek(0)
            data = err_file.read().decode(errors="replace")
            stderr_texts.append(data[-2000:].strip())

    return PipelineResult([p.returncode for p in procs], stderr_texts)


# -----------------------------------------------------------------------------
# Signal Handling & Watchdog
# -----------------------------------------------------------------------------


def signal_handler(signum: int, _frame: FrameType | None) -> None:
    """Handle termination signals: flag shutdown and interrupt any active
    subprocess so the script reacts promptly instead of after hours."""
    global shutdown_requested

    sig_name = signal.Signals(signum).name
    log.warning(f"Received signal {sig_name}. Initiating graceful shutdown...")
    shutdown_requested = True
    terminate_active_processes()


def setup_signal_handlers() -> None:
    """Set up signal handlers for graceful termination."""
    _ = signal.signal(signal.SIGTERM, signal_handler)
    _ = signal.signal(signal.SIGINT, signal_handler)
    _ = signal.signal(signal.SIGHUP, signal_handler)


def check_shutdown_requested() -> None:
    """Check if shutdown was requested and handle appropriately."""
    if shutdown_requested:
        log.warning("Shutdown requested - performing emergency cleanup...")
        raise KeyboardInterrupt("Shutdown signal received")


def _watchdog_fired() -> None:
    global shutdown_requested
    log.critical(
        f"Overall script timeout ({CONFIG.script_overall_timeout}s) exceeded! Aborting..."
    )
    shutdown_requested = True
    terminate_active_processes()


def start_watchdog() -> threading.Timer:
    """Enforce the overall script timeout."""
    timer = threading.Timer(CONFIG.script_overall_timeout, _watchdog_fired)
    timer.daemon = True
    timer.start()
    return timer


# -----------------------------------------------------------------------------
# Lock File Management
# -----------------------------------------------------------------------------

_lock_owned = False


@contextlib.contextmanager
def acquire_lock(lock_path: Path):
    """Context manager for a PID lock file.

    Only unlinks the lock file if *this process* created it - a losing
    concurrent run must never delete the winner's lock. Raises
    FileExistsError when another live run holds the lock.
    """
    global _lock_owned

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        if not is_stale_lock(lock_path):
            raise
        log.warning("Found stale lock file. Removing and retrying...")
        lock_path.unlink(missing_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)

    _lock_owned = True
    try:
        _ = os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    _ = atexit.register(cleanup_lock, lock_path)

    try:
        yield
    finally:
        cleanup_lock(lock_path)


def cleanup_lock(lock_path: Path) -> None:
    """Remove the lock file, but only if this process owns it."""
    global _lock_owned

    if not _lock_owned:
        return
    _lock_owned = False
    with contextlib.suppress(OSError):
        lock_path.unlink(missing_ok=True)


def is_stale_lock(lock_path: Path) -> bool:
    """A lock is stale if its owning process is no longer alive."""
    try:
        pid = int(lock_path.read_text().strip())
    except (OSError, ValueError):
        # No/unreadable PID (e.g. lock from an older script version):
        # fall back to an age check.
        try:
            age = time.time() - lock_path.stat().st_mtime
            return age > CONFIG.script_overall_timeout
        except OSError:
            return True

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # process exists, owned by someone else
    return False


# -----------------------------------------------------------------------------
# Disk Space Checking
# -----------------------------------------------------------------------------


def check_disk_space(path: Path) -> tuple[bool, str]:
    """
    Check if there's sufficient disk space for backup.

    Returns:
        Tuple of (is_sufficient, message)
    """
    try:
        stat = os.statvfs(path)
        free_bytes = stat.f_bavail * stat.f_frsize
        total_bytes = stat.f_blocks * stat.f_frsize
        free_gb = free_bytes / (1024**3)
        free_percent = (free_bytes / total_bytes) * 100 if total_bytes > 0 else 0

        if free_gb < CONFIG.min_free_space_gb:
            return (
                False,
                f"Insufficient disk space: {free_gb:.1f}GB free (minimum: {CONFIG.min_free_space_gb}GB)",
            )

        if free_percent < CONFIG.min_free_space_percent:
            return (
                False,
                f"Insufficient disk space: {free_percent:.1f}% free (minimum: {CONFIG.min_free_space_percent}%)",
            )

        return True, f"Disk space OK: {free_gb:.1f}GB ({free_percent:.1f}%) free"

    except OSError as e:
        return False, f"Failed to check disk space: {e}"


# -----------------------------------------------------------------------------
# Uptime Kuma Integration
# -----------------------------------------------------------------------------


class UptimeKumaRetry:
    """Uptime Kuma API wrapper with retry functionality."""

    url: str
    username: str
    password: str
    max_retries: int
    initial_delay: float
    max_delay: float
    backoff_factor: float
    api: "KumaApiT | None"

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        max_retries: int = 3,
        initial_delay: float = 1.0,
        max_delay: float = 10.0,
        backoff_factor: float = 2.0,
    ):
        self.url = url
        self.username = username
        self.password = password
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        self.api = None

    def connect(self) -> "KumaApiT":
        """Establish connection to Uptime Kuma with retries."""
        if UptimeKumaApi is None:
            raise RuntimeError("Uptime Kuma API not available")

        retry_count = 0
        delay = self.initial_delay

        while retry_count < self.max_retries:
            try:
                if self.api is not None:
                    with contextlib.suppress(Exception):
                        _ = self.api.disconnect()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

                self.api = UptimeKumaApi(self.url, timeout=30)
                _ = self.api.login(self.username, self.password)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                log.info("Successfully connected to Uptime Kuma")
                return self.api

            except Exception as e:
                retry_count += 1
                if retry_count == self.max_retries:
                    log.error(
                        f"Failed to connect to Uptime Kuma after {self.max_retries} attempts. Last error: {e!s}"
                    )
                    raise

                log.warning(
                    f"Uptime Kuma connection attempt {retry_count} failed: {e!s}"
                )
                log.info(f"Retrying in {delay} seconds...")
                time.sleep(delay)
                delay = min(delay * self.backoff_factor, self.max_delay)

        raise RuntimeError("Maximum connection retries exceeded")

    def require_api(self) -> "KumaApiT":
        """Return the connected API client, connecting if needed."""
        if self.api is None:
            return self.connect()
        return self.api

    def retry_operation[T](
        self, operation: Callable[..., T], *args: object, **kwargs: object
    ) -> T:
        """Retry an Uptime Kuma API operation with exponential backoff."""
        retry_count = 0
        delay = self.initial_delay

        while retry_count < self.max_retries:
            try:
                if self.api is None:
                    _ = self.connect()
                return operation(*args, **kwargs)

            except Exception as e:
                retry_count += 1
                if retry_count == self.max_retries:
                    log.error(
                        f"Uptime Kuma operation failed after {self.max_retries} attempts. Last error: {e!s}"
                    )
                    raise

                log.warning(
                    f"Uptime Kuma operation attempt {retry_count} failed: {e!s}"
                )
                log.info(f"Retrying in {delay} seconds...")
                time.sleep(delay)
                delay = min(delay * self.backoff_factor, self.max_delay)

                with contextlib.suppress(Exception):
                    _ = self.connect()

        raise RuntimeError("Maximum retries exceeded")

    def __enter__(self) -> Self:
        _ = self.connect()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        if self.api is not None:
            with contextlib.suppress(Exception):
                _ = self.api.disconnect()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def maintenance_id_file() -> Path:
    """State file for the active maintenance window. Lives under the log
    directory (not /tmp) so it survives a reboot mid-backup."""
    return CONFIG.log_root_dir / "backup_maintenance_id.txt"


def create_backup_maintenance_window() -> int | None:
    """Create a maintenance window for the backup process."""
    if not CONFIG.uptime_kuma_enabled or dry_run_mode:
        if dry_run_mode:
            log.info("DRY RUN: Skipping maintenance window creation.")
        return None

    if UptimeKumaApi is None or MaintenanceStrategy is None:
        log.warning(
            "Uptime Kuma dependencies not available. Skipping maintenance window creation."
        )
        return None

    # A leftover ID file means a previous run crashed (or the box rebooted)
    # before removing its window - clean that orphan up first.
    if maintenance_id_file().exists():
        log.warning(
            "Found leftover maintenance window from a previous run. Removing it first..."
        )
        remove_backup_maintenance_window()

    try:
        log.info("Creating Uptime Kuma maintenance window for backup...")

        with UptimeKumaRetry(
            CONFIG.uptime_kuma_url,
            CONFIG.uptime_kuma_username,
            CONFIG.uptime_kuma_password,
        ) as kuma:
            api = kuma.require_api()
            server_info = cast(
                ServerInfo,
                cast(object, kuma.retry_operation(api.info)),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            )
            raw_timezone = str(server_info["serverTimezone"])
            try:
                server_timezone = str(ZoneInfo(raw_timezone))
            except ZoneInfoNotFoundError:
                log.warning(
                    f"Timezone '{raw_timezone}' not found in system tzdata. Using raw timezone string from Uptime Kuma."
                )
                server_timezone = raw_timezone
            log.info(f"Using server timezone: {server_timezone}")

            maintenance = cast(
                MaintenanceResponse,
                cast(
                    object,
                    kuma.retry_operation(
                        api.add_maintenance,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                        title="Server Backup in Progress",
                        description="Automated server backup is currently running. Services may be temporarily unavailable.",
                        strategy=MaintenanceStrategy.MANUAL,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                        active=True,
                        timezoneOption=server_timezone,
                    ),
                ),
            )

            maintenance_id = int(maintenance["maintenanceID"])
            log.info(f"Maintenance window created with ID: {maintenance_id}")

            monitors = cast(
                MonitorList,
                cast(object, kuma.retry_operation(api.get_monitors)),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            )
            monitor_ids: MonitorIdList = [{"id": monitor["id"]} for monitor in monitors]

            if monitor_ids:
                log.info(f"Adding {len(monitor_ids)} monitors to maintenance window")
                _ = kuma.retry_operation(  # pyright: ignore[reportUnknownVariableType]
                    api.add_monitor_maintenance,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                    maintenance_id,
                    monitor_ids,
                )
                log.info("Monitors added to maintenance window")

            status_pages = cast(
                StatusPageList,
                cast(object, kuma.retry_operation(api.get_status_pages)),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            )
            if status_page := next(
                (
                    page
                    for page in status_pages
                    if page["slug"] == CONFIG.uptime_kuma_status_page_slug
                ),
                None,
            ):
                log.info(
                    f"Adding status page '{CONFIG.uptime_kuma_status_page_slug}' to maintenance window"
                )
                _ = kuma.retry_operation(  # pyright: ignore[reportUnknownVariableType]
                    api.add_status_page_maintenance,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                    maintenance_id,
                    [{"id": status_page["id"]}],
                )
                log.info("Status page added to maintenance window")
            else:
                log.warning(
                    f"Status page '{CONFIG.uptime_kuma_status_page_slug}' not found"
                )

            try:
                maintenance_id_file().parent.mkdir(parents=True, exist_ok=True)
                _ = maintenance_id_file().write_text(str(maintenance_id))
                log.info(f"Maintenance ID saved to {maintenance_id_file()}")
            except Exception as e:
                log.error(f"Failed to save maintenance ID: {e}")

            return maintenance_id

    except Exception as e:
        log.error(f"Failed to create maintenance window: {e}")
        return None


def remove_backup_maintenance_window() -> None:
    """Remove the backup maintenance window."""
    if not CONFIG.uptime_kuma_enabled or dry_run_mode:
        if dry_run_mode:
            log.info("DRY RUN: Skipping maintenance window removal.")
        return

    if UptimeKumaApi is None:
        log.warning(
            "Uptime Kuma dependencies not available. Skipping maintenance window removal."
        )
        return

    try:
        id_file = maintenance_id_file()
        if not id_file.exists():
            log.warning(
                "No maintenance ID file found. Maintenance window may not have been created."
            )
            return

        maintenance_id = int(id_file.read_text().strip())

        log.info(f"Removing maintenance window with ID: {maintenance_id}")

        with UptimeKumaRetry(
            CONFIG.uptime_kuma_url,
            CONFIG.uptime_kuma_username,
            CONFIG.uptime_kuma_password,
        ) as kuma:
            api = kuma.require_api()
            result = cast(
                DeleteMaintenanceResponse,
                cast(
                    object,
                    kuma.retry_operation(
                        api.delete_maintenance,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                        maintenance_id,
                    ),
                ),
            )
            log.info(f"Maintenance window deleted. Result: {result}")

        try:
            id_file.unlink()
            log.info("Maintenance ID file removed")
        except FileNotFoundError:
            log.warning("Maintenance ID file already removed")
        except Exception as e:
            log.warning(f"Failed to remove maintenance ID file: {e}")

    except Exception as e:
        log.error(f"Failed to remove maintenance window: {e}")


# -----------------------------------------------------------------------------
# Logging & Notification Helpers
# -----------------------------------------------------------------------------

LOG_FORMATTER = logging.Formatter(
    "[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)

COLOR_GREEN = 65280
COLOR_YELLOW = 16776960
COLOR_RED = 16711680


def setup_console_logging(verbose: bool) -> None:
    """Configure logging to the console. File logging is attached later,
    once the configured log directory is known."""
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(LOG_FORMATTER)
    log.addHandler(handler)


def add_file_logging(log_file: Path) -> None:
    """Attach a file handler for the run log."""
    handler = logging.FileHandler(log_file)
    handler.setFormatter(LOG_FORMATTER)
    log.addHandler(handler)


def send_discord_notification(
    status: str, message: str, color: int, title_override: str | None = None
) -> None:
    """Sends a formatted notification to the configured Discord webhook."""
    if not CONFIG.discord_webhook_url or requests is None:
        return
    if dry_run_mode:
        log.info("DRY RUN: Skipping Discord notification.")
        return

    title = title_override or f"Local Backup Status: {status}"
    payload = {
        "username": CONFIG.discord_username,
        "avatar_url": CONFIG.discord_avatar_url,
        "embeds": [
            {
                "title": title,
                "description": message,
                "color": color,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        ],
    }

    # Retry Discord notifications with exponential backoff
    retry_delays: list[float] = [1.0, 2.0, 4.0]
    for attempt in range(3):
        try:
            response = requests.post(
                CONFIG.discord_webhook_url, json=payload, timeout=10
            )
            response.raise_for_status()
            return
        except requests.RequestException as e:
            if attempt == 2:
                log.error(f"Failed to send Discord notification after 3 attempts: {e}")
            else:
                time.sleep(retry_delays[attempt])


def upload_log_to_privatebin(log_file: Path | None) -> str | None:
    """Uploads the log file to PrivateBin and returns the URL."""
    if not CONFIG.privatebin_enabled or dry_run_mode or log_file is None:
        return None
    privatebin_cmd = find_command(CONFIG.privatebin_cli_path)
    if not privatebin_cmd:
        log.warning(
            f"PrivateBin CLI not found at '{CONFIG.privatebin_cli_path}'. Skipping log upload."
        )
        return None

    log.info("Uploading log file to PrivateBin...")
    try:
        with open(log_file, "rb") as f:
            result = run_command([privatebin_cmd, "create"], timeout=60, stdin=f)
        if result.returncode != 0:
            log.error(f"Failed to upload log to PrivateBin: {result.stderr.strip()}")
            return None
        log_url = result.stdout.strip()
        log.info(f"Log uploaded successfully: {log_url}")
        return log_url
    except subprocess.TimeoutExpired:
        log.error("PrivateBin upload timed out")
        return None
    except OSError as e:
        log.error(f"Failed to upload log to PrivateBin: {e}")
        return None


def format_bytes(byte_count: int | None) -> str:
    """Helper to format bytes into KB, MB, GB, etc."""
    if byte_count is None or byte_count == 0:
        return "0 B"

    power = 1024.0
    n = 0
    power_labels = {0: "", 1: "K", 2: "M", 3: "G", 4: "T"}
    byte_count_float = float(byte_count)

    while byte_count_float >= power and n < len(power_labels) - 1:
        byte_count_float /= power
        n += 1
    return f"{byte_count_float:.2f} {power_labels[n]}B"


def format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours}h:{minutes}m:{secs}s"


def determine_final_status(success: bool, warnings: list[str]) -> tuple[str, int, str]:
    """Pick the final (status, color, emoji) for the run summary."""
    if not success:
        return "Failed", COLOR_RED, "❌"
    if warnings:
        return "Completed with Warnings", COLOR_YELLOW, "⚠️"
    return "Success", COLOR_GREEN, "✅"


# -----------------------------------------------------------------------------
# Rclone with Retry Logic
# -----------------------------------------------------------------------------


def run_rclone_sync_with_retry(log_file: Path | None) -> dict[str, str | int]:
    """Runs the rclone sync process with retry logic."""
    log.info("--- Starting Rclone Off-site Upload ---")

    if not CONFIG.rclone_enabled:
        log.info("Rclone upload is disabled. Skipping.")
        return {"status": "skipped"}

    if dry_run_mode:
        log.info("DRY RUN: Skipping rclone execution.")
        return {"status": "skipped"}

    start_time = time.monotonic()
    retry_delay = CONFIG.rclone_retry_delay
    last_error: str = ""

    # Send start notification
    start_timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    start_message = (
        f"**Status Details**\n"
        f"Beginning sync operation...\n\n"
        f"**Sync Info**\n"
        f"Source: `{CONFIG.backup_root_dir}`\n"
        f"Destination: `{CONFIG.rclone_remote_dest}`\n\n"
        f"**Timestamp**\n"
        f"{start_timestamp}"
    )
    send_discord_notification(
        "Started",
        start_message,
        COLOR_YELLOW,
        title_override="Rclone Sync Status: Started",
    )

    for attempt in range(1, CONFIG.rclone_max_retries + 1):
        check_shutdown_requested()

        if attempt > 1:
            log.info(f"Rclone retry attempt {attempt}/{CONFIG.rclone_max_retries}")
            log.info(f"Waiting {retry_delay} seconds before retry...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, CONFIG.rclone_retry_max_delay)

        try:
            result = _execute_rclone_sync(log_file)

            if result["status"] == "success":
                log.info(f"Rclone sync completed successfully on attempt {attempt}")
                result["attempts"] = attempt
                return result

            # Check if error is retryable
            exit_code = int(result.get("exit_code", 1))
            if not _is_retryable_rclone_error(exit_code):
                log.error(f"Rclone failed with non-retryable error (exit code {exit_code})")
                result["attempts"] = attempt
                return result

            last_error = str(result.get("last_error", "Unknown error"))
            log.warning(f"Rclone attempt {attempt} failed: {last_error}")

        except OperationTimeout:
            log.error(f"Rclone attempt {attempt} timed out")
            last_error = f"Operation timed out after {CONFIG.rclone_overall_timeout} seconds"

        except Exception as e:
            log.error(f"Rclone attempt {attempt} failed with exception: {e}")
            last_error = str(e)

    # All retries exhausted
    duration = time.monotonic() - start_time
    return {
        "status": "failed",
        "exit_code": 1,
        "duration": format_duration(duration),
        "attempts": CONFIG.rclone_max_retries,
        "last_error": f"All {CONFIG.rclone_max_retries} attempts failed. Last error: {last_error}",
        "transferred": "0 files, 0 B",
        "transferred_files": "0 files",
        "transferred_data": "0 B",
        "errors": str(CONFIG.rclone_max_retries),
        "checks": "0 files, 0 B",
        "checks_count": 0,
        "total_checks": 0,
    }


def _is_retryable_rclone_error(exit_code: int) -> bool:
    """Determine if an rclone error is retryable."""
    # Rclone exit codes:
    # 0 - Success
    # 1 - Syntax or usage error
    # 2 - Error not otherwise categorised
    # 3 - Directory not found
    # 4 - File not found
    # 5 - Temporary error (retryable)
    # 6 - Less serious errors (retryable)
    # 7 - Fatal error
    # 8 - Transfer limit exceeded
    # 9 - Operation successful but no files transferred

    # Non-retryable errors
    non_retryable = {1, 3, 4, 7}
    return exit_code not in non_retryable


def _execute_rclone_sync(log_file: Path | None) -> dict[str, str | int]:
    """Execute a single rclone sync operation."""
    start_time = time.monotonic()

    # Flush logs before rclone writes to the same file
    for handler in log.handlers:
        handler.flush()

    # Create a separate log file for this rclone run
    if log_file is not None:
        rclone_log = log_file.with_suffix(".rclone.log")
    else:
        rclone_log = Path(tempfile.gettempdir()) / f"backup-rclone-{os.getpid()}.log"

    command = [
        resolve_command("rclone"),
        "sync",
        str(CONFIG.backup_root_dir),
        CONFIG.rclone_remote_dest,
        "--create-empty-src-dirs",
        f"--bwlimit={CONFIG.rclone_bandwidth_limit}",
        "--retries",
        "3",
        "--retries-sleep",
        "10s",
        "--timeout",
        "30s",
        "--low-level-retries",
        "10",
        "--stats",
        "1m",
        "--stats-file-name-length",
        "0",
        "--transfers=8",
        "--log-file",
        str(rclone_log),
        "--log-level",
        "INFO",
        "--use-json-log",
    ]

    filter_file: Path | None = None
    if CONFIG.rclone_filters:
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".txt", prefix="rclone-filters-"
        ) as f:
            filter_file = Path(f.name)
            _ = f.write("\n".join(CONFIG.rclone_filters))
        command.append(f"--filter-from={filter_file}")

    log.info(f"Running rclone command: {' '.join(command)}")

    try:
        process = run_command(command, timeout=CONFIG.rclone_overall_timeout)
        exit_code = process.returncode
    except subprocess.TimeoutExpired:
        raise OperationTimeout(
            f"Rclone operation timed out after {CONFIG.rclone_overall_timeout} seconds"
        )
    finally:
        if filter_file:
            filter_file.unlink(missing_ok=True)

    duration = time.monotonic() - start_time

    # Parse stats from the JSON log file
    final_stats: JsonDict = {}
    error_lines: list[str] = []

    if rclone_log.exists():
        with open(rclone_log, "r") as f:
            for line in f:
                if not line.strip().startswith("{"):
                    continue
                try:
                    parsed_json: object = json.loads(line)  # pyright: ignore[reportAny]
                    if not isinstance(parsed_json, dict):
                        continue

                    log_entry = cast(JsonDict, parsed_json)

                    stats_data = log_entry.get("stats")
                    if "stats" in log_entry and isinstance(stats_data, dict):
                        final_stats = stats_data

                    elif log_entry.get("level") == "error":
                        if (msg := log_entry.get("msg")) and isinstance(msg, str):
                            error_lines.append(msg)
                except json.JSONDecodeError:
                    continue

        # Append rclone log to main log
        if log_file is not None:
            with open(log_file, "a") as main_log, open(rclone_log, "r") as rclone_f:
                _ = main_log.write("\n--- Rclone Log ---\n")
                _ = main_log.write(rclone_f.read())

        rclone_log.unlink(missing_ok=True)

    # Extract stats safely
    transfers = final_stats.get("transfers", 0)
    bytes_transferred = final_stats.get("bytes", 0)
    errors_count = final_stats.get("errors", len(error_lines))
    checks_count = final_stats.get("checks", 0)
    total_bytes = final_stats.get("totalBytes", 0)

    transfers = int(transfers) if isinstance(transfers, (int, float)) else 0
    bytes_transferred = (
        int(bytes_transferred) if isinstance(bytes_transferred, (int, float)) else 0
    )
    errors_count = (
        int(errors_count)
        if isinstance(errors_count, (int, float))
        else len(error_lines)
    )
    checks_count = int(checks_count) if isinstance(checks_count, (int, float)) else 0
    total_bytes = int(total_bytes) if isinstance(total_bytes, (int, float)) else 0

    return {
        "status": "success" if exit_code == 0 else "failed",
        "exit_code": exit_code,
        "duration": format_duration(duration),
        "transferred": f"{transfers} files, {format_bytes(bytes_transferred)}",
        "transferred_files": f"{transfers} files",
        "transferred_data": format_bytes(bytes_transferred),
        "errors": str(errors_count),
        "checks": f"{checks_count} files, {format_bytes(total_bytes)}",
        "checks_count": checks_count,
        "total_checks": checks_count,
        "last_error": "\n".join(error_lines[-3:]) if error_lines else "None",
    }


# -----------------------------------------------------------------------------
# Pre-flight Checks
# -----------------------------------------------------------------------------


def _validate_integrations() -> None:
    """Disable integrations whose configuration is incomplete, instead of
    letting them burn through retry ladders at runtime."""
    if CONFIG.uptime_kuma_enabled:
        missing = [
            name
            for name, value in (
                ("url", CONFIG.uptime_kuma_url),
                ("username", CONFIG.uptime_kuma_username),
                ("password", CONFIG.uptime_kuma_password),
            )
            if not value
        ]
        if missing:
            log.warning(
                f"Uptime Kuma maintenance is enabled but missing: {', '.join(missing)}. "
                f"Disabling for this run. (Hint: set {ENV_UPTIME_KUMA_PASSWORD} or the config file.)"
            )
            CONFIG.uptime_kuma_enabled = False
        elif UptimeKumaApi is None:
            log.warning(
                "Uptime Kuma maintenance is enabled but the 'uptime-kuma-api' package "
                "is not installed. Disabling for this run."
            )
            CONFIG.uptime_kuma_enabled = False

    if CONFIG.discord_webhook_url and requests is None:
        log.warning(
            "Discord webhook is configured but the 'requests' package is not "
            "installed. Notifications disabled for this run."
        )
        CONFIG.discord_webhook_url = ""
    elif not CONFIG.discord_webhook_url:
        log.info("Discord webhook not configured; notifications disabled.")

    if CONFIG.privatebin_enabled and not find_command(CONFIG.privatebin_cli_path):
        log.warning(
            f"PrivateBin CLI '{CONFIG.privatebin_cli_path}' not found. "
            "Log uploads disabled for this run."
        )
        CONFIG.privatebin_enabled = False


def _report_backup_sources() -> None:
    """Dry-run helper: report the state of every configured backup source."""
    log.info("Backup sources:")
    for src in CONFIG.backup_sources:
        if not src.exists():
            log.warning(f"  MISSING   {src}")
        elif not os.access(src, os.R_OK):
            log.warning(f"  UNREADABLE {src}")
        else:
            log.info(f"  ok        {src}")
    for excl in CONFIG.backup_exclusions:
        log.info(f"  excluded  {excl}{'' if excl.exists() else ' (not present)'}")


def pre_flight_checks() -> None:
    """Perform pre-flight checks before starting backup.

    In normal mode the first failure raises PreFlightError. In dry-run mode
    all checks run to completion and problems are reported (root is not
    required), turning --dry-run into a "will tonight's run work?" preview.
    """
    log.info("Performing pre-flight checks...")
    problems: list[str] = []

    def problem(message: str) -> None:
        if dry_run_mode:
            problems.append(message)
            log.warning(f"WOULD FAIL: {message}")
        else:
            raise PreFlightError(message)

    if os.geteuid() != 0:
        problem("This script must be run as root.")

    if CONFIG.compression_tool not in ("gzip", "pigz"):
        raise PreFlightError(
            f"Invalid compression_tool: {CONFIG.compression_tool}. Must be 'gzip' or 'pigz'."
        )

    deps = ["tar", CONFIG.compression_tool, "age"]
    if CONFIG.docker_enabled:
        deps.append("docker")
    if CONFIG.rclone_enabled:
        deps.append("rclone")

    for dep in deps:
        resolved = find_command(dep)
        if not resolved:
            hint = ""
            if dep == "pigz":
                hint = " (install with: sudo apt-get install pigz)"
            elif dep == "age":
                hint = " (install with: brew install age / sudo apt-get install age)"
            problem(
                f"Missing required dependency: {dep}{hint}. Searched PATH and "
                f"brew locations: {', '.join(str(d) for d in _command_search_dirs())}"
            )
        else:
            log.info(f"Dependency '{dep}' resolved to: {resolved}")

    identity = CONFIG.age_identity_file
    if not identity.is_file() or identity.stat().st_size == 0:
        problem(
            f"age identity file not found or empty: {identity}. "
            f"Create it with: age-keygen -o {identity} && chmod 600 {identity} "
            f"- and KEEP A COPY OFF THIS MACHINE (lost key = unreadable backups)."
        )
    else:
        mode = identity.stat().st_mode & 0o777
        if mode & 0o077:
            log.warning(
                f"age identity file {identity} has loose permissions "
                f"({mode:o}); consider: chmod 600 {identity}"
            )

    try:
        _ = pwd.getpwnam(CONFIG.backup_user)
        _ = grp.getgrnam(CONFIG.backup_group)
    except KeyError as e:
        problem(f"Backup user/group not found: {e}")

    space_ok, space_msg = check_disk_space(
        CONFIG.backup_root_dir if CONFIG.backup_root_dir.exists() else Path("/")
    )
    if not space_ok:
        problem(space_msg)
    else:
        log.info(space_msg)

    _validate_integrations()

    if dry_run_mode:
        _report_backup_sources()
        if problems:
            log.warning(
                f"DRY RUN: {len(problems)} pre-flight problem(s) found - a real run would fail."
            )
        else:
            log.info("DRY RUN: All pre-flight checks would pass.")
    else:
        log.info("Pre-flight checks passed.")


# -----------------------------------------------------------------------------
# Docker Management Functions
# -----------------------------------------------------------------------------


def get_docker_compose_files() -> tuple[list[Path], list[Path]]:
    """Get Docker compose files, separating Plex from others."""
    if not CONFIG.docker_stacks_dir.is_dir():
        log.warning(f"Docker stacks directory not found at {CONFIG.docker_stacks_dir}")
        return [], []

    all_files = sorted(
        list(CONFIG.docker_stacks_dir.glob("**/compose.yaml"))
        + list(CONFIG.docker_stacks_dir.glob("**/compose.yml"))
    )
    plex_files = [CONFIG.plex_compose_file] if CONFIG.plex_compose_file.is_file() else []
    if not plex_files:
        log.warning(f"Plex compose file not found: {CONFIG.plex_compose_file}")
    other_files = [
        f for f in all_files if f.resolve() != CONFIG.plex_compose_file.resolve()
    ]
    log.info(f"Found {len(all_files)} total Docker compose files.")
    return plex_files, other_files


def get_running_container_ids() -> list[str]:
    """Get list of all running container IDs."""
    try:
        result = run_command([resolve_command("docker"), "ps", "-q"], timeout=30)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.error(f"Failed to get running containers: {e}")
        return []
    if result.returncode != 0:
        log.error(f"Failed to get running containers: {result.stderr}")
        return []
    return [cid.strip() for cid in result.stdout.strip().split("\n") if cid.strip()]


def get_container_names(container_ids: list[str]) -> dict[str, str]:
    """Get container names for given IDs for better logging."""
    if not container_ids:
        return {}

    try:
        result = run_command(
            [
                resolve_command("docker"),
                "inspect",
                "--format",
                "{{.ID}}: {{.Name}}",
                *container_ids,
            ],
            timeout=30,
        )
        if result.returncode != 0:
            return {}

        names: dict[str, str] = {}
        for line in result.stdout.strip().split("\n"):
            if ": " in line:
                cid, name = line.split(": ", 1)
                names[cid[:12]] = name.lstrip("/")
        return names
    except (subprocess.TimeoutExpired, OSError):
        return {}


def _describe_containers(container_ids: list[str]) -> str:
    names = get_container_names(container_ids)
    return ", ".join(names.get(cid[:12], cid[:12]) for cid in container_ids)


def _compose_action(file: Path, action: str) -> bool:
    """Run a single `docker compose` stop/start for one compose file."""
    command = [resolve_command("docker"), "compose", "-f", str(file)]
    if action == "stop":
        command.append(CONFIG.docker_shutdown_method)
    else:
        command.extend(["up", "-d"])

    try:
        result = run_command(command, timeout=CONFIG.docker_compose_timeout)
    except subprocess.TimeoutExpired:
        log.error(f"Timeout while trying to {action} services for {file}")
        return False
    except OSError as e:
        log.error(f"Failed to {action} services for {file}: {e}")
        return False

    if result.returncode != 0:
        log.error(f"Failed to {action} services for {file}: {result.stderr}")
        return False
    return True


def manage_docker_services(compose_files: list[Path], action: str) -> bool:
    """Manage Docker services using docker compose.

    Stops run sequentially; starts run concurrently (a settle delay is
    applied once per batch instead of per stack)."""
    if not CONFIG.docker_enabled or dry_run_mode:
        return True

    existing = [f for f in compose_files if f.is_file()]
    for missing in set(compose_files) - set(existing):
        log.warning(f"Compose file not found: {missing}. Skipping.")

    if not existing:
        return True

    log.info(f"Performing '{action}' on {len(existing)} Docker compose file(s)...")

    if action == "stop":
        results = [_compose_action(f, action) for f in existing]
    else:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda f: _compose_action(f, action), existing))
        log.info(
            f"Waiting {CONFIG.docker_start_settle_delay}s for services to settle..."
        )
        time.sleep(CONFIG.docker_start_settle_delay)

    return all(results)


def force_stop_containers(container_ids: list[str], timeout: int = 30) -> None:
    """Force stop specific containers using docker stop."""
    if not container_ids:
        return

    if dry_run_mode:
        log.info(f"DRY RUN: Would force stop {len(container_ids)} container(s).")
        return

    log.warning(
        f"Force stopping {len(container_ids)} container(s): "
        f"{_describe_containers(container_ids)}"
    )

    try:
        result = run_command(
            [resolve_command("docker"), "stop", "-t", str(timeout), *container_ids],
            timeout=timeout + 30,
        )
        if result.returncode != 0:
            log.error(f"docker stop returned error: {result.stderr}")
    except subprocess.TimeoutExpired:
        log.error("docker stop command timed out")


def force_kill_containers(container_ids: list[str]) -> None:
    """Force kill specific containers using docker kill."""
    if not container_ids:
        return

    if dry_run_mode:
        log.info(f"DRY RUN: Would force kill {len(container_ids)} container(s).")
        return

    log.warning(
        f"Force killing {len(container_ids)} container(s): "
        f"{_describe_containers(container_ids)}"
    )

    try:
        result = run_command(
            [resolve_command("docker"), "kill", *container_ids], timeout=60
        )
        if result.returncode != 0:
            log.error(f"docker kill returned error: {result.stderr}")
    except subprocess.TimeoutExpired:
        log.error("docker kill command timed out")


def ensure_all_containers_stopped(
    compose_files: list[Path],
    timeout: int | None = None,
) -> bool:
    """
    Ensure all Docker containers are stopped with retry logic.

    Uses a multi-stage approach with overall timeout and retries:
    1. Try docker compose down/stop for each compose file (graceful)
    2. If any containers remain, use docker stop on all remaining
    3. If still running, use docker kill as last resort
    4. Retry entire process if containers persist

    Returns:
        True if all containers are stopped, False otherwise.
    """
    if not CONFIG.docker_enabled:
        log.info("Docker stop/start is disabled. Skipping container shutdown.")
        return True

    if dry_run_mode:
        log.info("DRY RUN: Skipping container shutdown.")
        return True

    if timeout is None:
        timeout = CONFIG.docker_force_stop_timeout

    overall_start = time.monotonic()

    for retry in range(CONFIG.docker_shutdown_max_retries):
        check_shutdown_requested()

        if retry > 0:
            log.warning(
                f"Container shutdown retry {retry + 1}/{CONFIG.docker_shutdown_max_retries}"
            )
            time.sleep(CONFIG.docker_shutdown_retry_delay)

        # Check overall timeout
        if time.monotonic() - overall_start > CONFIG.docker_shutdown_overall_timeout:
            log.critical("Overall container shutdown timeout exceeded!")
            break

        initial_containers = get_running_container_ids()
        if not initial_containers:
            log.info("No running containers found. Nothing to stop.")
            return True

        log.info("=" * 60)
        log.info(f"Starting container shutdown (attempt {retry + 1})")
        log.info(f"Found {len(initial_containers)} running container(s) to stop.")
        log.info("=" * 60)

        # Stage 1: Try compose down for each file
        log.info("-" * 40)
        log.info("Stage 1: Graceful shutdown via docker compose")
        log.info("-" * 40)
        _ = manage_docker_services(compose_files, "stop")

        remaining = get_running_container_ids()
        if not remaining:
            log.info("✓ All containers stopped successfully via docker compose.")
            return True

        log.warning(f"{len(remaining)} container(s) still running after compose down.")

        # Stage 2: Use docker stop on remaining containers
        log.info("-" * 40)
        log.info("Stage 2: Fallback via docker stop")
        log.info("-" * 40)
        force_stop_containers(remaining, timeout=timeout)

        time.sleep(CONFIG.docker_verify_shutdown_interval)

        remaining = get_running_container_ids()
        if not remaining:
            log.info("✓ All containers stopped successfully via docker stop.")
            return True

        log.warning(f"{len(remaining)} container(s) still running after docker stop.")

        # Stage 3: Use docker kill as last resort
        log.info("-" * 40)
        log.info("Stage 3: Last resort via docker kill")
        log.info("-" * 40)
        force_kill_containers(remaining)

        time.sleep(CONFIG.docker_kill_wait_time)

        final_remaining = get_running_container_ids()
        if not final_remaining:
            log.info("✓ All containers stopped successfully via docker kill.")
            return True

        log.error(
            f"Still have {len(final_remaining)} stubborn container(s): "
            f"{_describe_containers(final_remaining)}"
        )

    # All retries exhausted
    final_remaining = get_running_container_ids()
    if final_remaining:
        container_list = _describe_containers(final_remaining)
        log.critical(
            f"CRITICAL: {len(final_remaining)} container(s) could not be stopped after all retries!"
        )
        log.critical(f"Stubborn containers: {container_list}")

        send_discord_notification(
            "Container Shutdown Failed",
            (
                f"⚠️ **{len(final_remaining)} container(s) could not be stopped**\n\n"
                + f"Containers: `{container_list}`\n\n"
                + "**BACKUP WILL PROCEED ANYWAY** - data may be inconsistent for these services.\n\n"
                + "Manual intervention required after backup completes."
            ),
            COLOR_RED,
            title_override="Backup Warning: Containers Still Running",
        )

        # Return False but DON'T stop the backup - just note the warning
        backup_state.add_warning(f"Containers still running: {container_list}")
        return False

    return True


# -----------------------------------------------------------------------------
# Backup Functions
# -----------------------------------------------------------------------------


def rotate_items(
    dir_path: Path, patterns: str | list[str], retention_count: int
) -> list[Path]:
    """Rotate files in a directory: keep the newest `retention_count` files
    matching any of the patterns, delete the rest. In dry-run mode, report
    what would be deleted instead. Returns the removed (or would-be-removed)
    files."""
    if not dir_path.is_dir():
        return []
    if isinstance(patterns, str):
        patterns = [patterns]

    log.info(f"Rotating items in {dir_path} matching {patterns}...")
    items = sorted(
        {p for pattern in patterns for p in dir_path.glob(pattern) if p.is_file()},
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    removed: list[Path] = []
    for item in items[retention_count:]:
        if dry_run_mode:
            log.info(f"DRY RUN: Would remove old item: {item.name}")
            removed.append(item)
            continue
        try:
            item.unlink()
            log.info(f"Removed old item: {item.name}")
            removed.append(item)
        except OSError as e:
            log.error(f"Failed to remove {item.name}: {e}")
    return removed


def cleanup_orphan_manifests(backup_dir: Path) -> None:
    """Delete .sha256 manifests whose backup file no longer exists."""
    if not backup_dir.is_dir():
        return
    for manifest in backup_dir.glob("*.sha256"):
        target = manifest.with_name(manifest.name.removesuffix(".sha256"))
        if target.exists():
            continue
        if dry_run_mode:
            log.info(f"DRY RUN: Would remove orphaned manifest: {manifest.name}")
            continue
        with contextlib.suppress(OSError):
            manifest.unlink()
            log.info(f"Removed orphaned manifest: {manifest.name}")


def _effective_backup_sources() -> tuple[list[str], list[str]]:
    """Validate configured sources; ensure the Plex data dir is covered.

    Returns (valid_source_paths, failed_descriptions).
    """
    sources = list(CONFIG.backup_sources)

    # The Plex data dir was historically handled as a separate priority pass.
    # Now it is simply guaranteed to be part of the source list (unless a
    # configured source already contains it).
    plex = CONFIG.plex_data_dir
    if plex.exists():
        plex_resolved = plex.resolve()
        covered = any(
            plex_resolved == s.resolve() or s.resolve() in plex_resolved.parents
            for s in sources
            if s.exists()
        )
        if not covered:
            sources.append(plex)
    else:
        log.warning(f"Plex data directory not found at {plex}")

    valid: list[str] = []
    failed: list[str] = []
    for p in sources:
        if p.exists():
            if os.access(p, os.R_OK):
                valid.append(str(p.resolve()))
            else:
                failed.append(f"{p} (permission denied)")
                log.warning(f"Cannot read backup source, skipping: {p}")
        else:
            failed.append(f"{p} (not found)")
            log.warning(f"Backup source path does not exist, skipping: {p}")

    return valid, failed


def create_backup(backup_file: Path) -> bool:
    """Create the backup archive via a single streaming pipeline:

        tar -cf - <sources> | pigz | age -e -i <identity> -o <backup_file>

    No temporary uncompressed tar is written to disk, halving I/O and
    removing the need for free space equal to the uncompressed data size.
    """
    if dry_run_mode:
        log.info("DRY RUN: Skipping backup creation.")
        return True

    log.info("Starting backup creation process...")

    valid_sources, failed_sources = _effective_backup_sources()
    if not valid_sources:
        raise BackupCreationError("No valid backup sources found!")
    if failed_sources:
        backup_state.add_warning(f"Skipped directories: {', '.join(failed_sources)}")

    exclude_opts = [
        f"--exclude={path.resolve()}"
        for path in CONFIG.backup_exclusions
        if path.exists()
    ]

    tar_cmd = [
        resolve_tar(),
        "-cf",
        "-",
        "-C",
        "/",
        "--ignore-failed-read",
        "--warning=no-file-changed",
        *exclude_opts,
        *valid_sources,
    ]
    compress_cmd = [
        resolve_command(CONFIG.compression_tool),
        f"-{CONFIG.compression_level}",
    ]
    encrypt_cmd = [
        resolve_command("age"),
        "-e",
        "-i",
        str(CONFIG.age_identity_file),
        "-o",
        str(backup_file),
    ]

    log.info(
        f"Backing up {len(valid_sources)} source(s) via streaming pipeline: "
        f"tar | {CONFIG.compression_tool} -{CONFIG.compression_level} | age"
    )

    try:
        result = run_pipeline(
            [
                ("tar", tar_cmd),
                (CONFIG.compression_tool, compress_cmd),
                ("age", encrypt_cmd),
            ],
            timeout=BACKUP_CREATE_TIMEOUT,
        )

        tar_rc, compress_rc, encrypt_rc = result.returncodes
        tar_err, compress_err, encrypt_err = result.stderr

        # tar exit code 1 means "some files changed while reading" - acceptable.
        if tar_rc > 1 or tar_rc < 0:
            raise BackupCreationError(f"Tar failed with exit code {tar_rc}: {tar_err}")
        if tar_rc == 1:
            log.warning("Some files changed during backup (non-critical)")
        if compress_rc != 0:
            raise BackupCreationError(
                f"Compression failed with exit code {compress_rc}: {compress_err}"
            )
        if encrypt_rc != 0:
            raise BackupCreationError(
                f"Encryption failed with exit code {encrypt_rc}: {encrypt_err}"
            )

        if not backup_file.exists():
            raise BackupCreationError("Final backup file was not created")

        final_size = backup_file.stat().st_size
        if final_size == 0:
            raise BackupCreationError("Final backup file is empty")

        log.info(f"Backup created successfully: {format_bytes(final_size)}")
        return True

    except (BackupError, OSError):
        # Never leave a partial/corrupt archive behind.
        with contextlib.suppress(OSError):
            backup_file.unlink(missing_ok=True)
        raise


def verify_backup(backup_file: Path) -> bool:
    """Verify backup integrity by decrypting, decompressing, and listing the
    entire archive (gzip's CRC and tar's structure checks run over all data)."""
    if dry_run_mode:
        log.info("DRY RUN: Skipping verification.")
        return True

    if not backup_file.exists():
        raise BackupVerificationError("Backup file does not exist")

    log.info(f"Verifying backup integrity of {backup_file.name}...")

    stages = [
        (
            "age",
            [
                resolve_command("age"),
                "-d",
                "-i",
                str(CONFIG.age_identity_file),
            ],
        ),
        (CONFIG.compression_tool, [resolve_command(CONFIG.compression_tool), "-d"]),
        ("tar", [resolve_tar(), "-tf", "-"]),
    ]

    try:
        with open(backup_file, "rb") as f_in:
            result = run_pipeline(
                stages,
                timeout=BACKUP_VERIFY_TIMEOUT,
                stdin_first=f_in,
                stdout_final=subprocess.DEVNULL,
            )
    except OperationTimeout:
        raise BackupVerificationError("Verification timed out")

    decrypt_rc, decompress_rc, tar_rc = result.returncodes
    decrypt_err, decompress_err, tar_err = result.stderr

    if decrypt_rc != 0:
        raise BackupVerificationError(f"Decryption failed: {decrypt_err}")
    if decompress_rc != 0:
        raise BackupVerificationError(f"Decompression failed: {decompress_err}")
    if tar_rc != 0:
        raise BackupVerificationError(f"Tar verification failed: {tar_err}")

    log.info("Backup verification successful.")
    return True


def write_sha256_manifest(backup_file: Path) -> Path | None:
    """Write a `<backup>.sha256` manifest so remote copies can be checked
    without downloading and decrypting the archive."""
    if dry_run_mode:
        return None

    h = hashlib.sha256()
    with open(backup_file, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)

    manifest = Path(str(backup_file) + ".sha256")
    _ = manifest.write_text(f"{h.hexdigest()}  {backup_file.name}\n")
    log.info(f"SHA-256 manifest written: {manifest.name}")

    try:
        uid = pwd.getpwnam(CONFIG.backup_user).pw_uid
        gid = grp.getgrnam(CONFIG.backup_group).gr_gid
        os.chown(manifest, uid, gid)
        os.chmod(manifest, 0o644)
    except (KeyError, OSError) as e:
        log.warning(f"Could not set permissions on manifest {manifest}: {e}")

    return manifest


def set_permissions(backup_file: Path) -> None:
    """Set ownership and permissions on backup file."""
    if dry_run_mode:
        return

    log.info("Setting final permissions on backup file...")
    try:
        uid = pwd.getpwnam(CONFIG.backup_user).pw_uid
        gid = grp.getgrnam(CONFIG.backup_group).gr_gid
        os.chown(backup_file, uid, gid)
        os.chmod(backup_file, 0o600)
    except (KeyError, OSError) as e:
        log.error(f"Failed to set permissions on {backup_file}: {e}")
        backup_state.add_warning(f"Could not set permissions: {e}")


def set_log_permissions(log_file: Path) -> None:
    """Set ownership of log file to the backup user."""
    if dry_run_mode:
        return

    try:
        uid = pwd.getpwnam(CONFIG.backup_user).pw_uid
        gid = grp.getgrnam(CONFIG.backup_group).gr_gid
        os.chown(log_file, uid, gid)
        os.chmod(log_file, 0o644)
    except (KeyError, OSError) as e:
        log.warning(f"Failed to set permissions on log file {log_file}: {e}")


# -----------------------------------------------------------------------------
# Emergency Recovery & Final Notification
# -----------------------------------------------------------------------------


def emergency_container_restart() -> None:
    """Emergency function to restart all containers in case of failure."""
    log.warning("=" * 60)
    log.warning("EMERGENCY: Attempting to restart all containers")
    log.warning("=" * 60)

    try:
        plex_compose, other_compose = get_docker_compose_files()

        # Start Plex first
        if plex_compose:
            log.info("Starting Plex services...")
            _ = manage_docker_services(plex_compose, "start")

        # Then other services
        if other_compose:
            log.info("Starting other services...")
            _ = manage_docker_services(other_compose, "start")

        # Verify some containers are running
        time.sleep(10)
        running = get_running_container_ids()
        log.info(f"Emergency restart complete. {len(running)} container(s) now running.")

    except Exception as e:
        log.critical(f"Emergency container restart failed: {e}")


def send_final_status_notification(success: bool, log_file: Path | None) -> None:
    """Send final status notification with summary. This is the single place
    that uploads the log to PrivateBin (once per run)."""
    if not CONFIG.discord_webhook_url:
        return

    privatebin_link = upload_log_to_privatebin(log_file)

    duration = format_duration(backup_state.elapsed_time)
    status, color, emoji = determine_final_status(success, backup_state.warnings)

    message = f"{emoji} **Backup {status}**\n\n"
    message += f"**Duration:** {duration}\n"

    if not success and backup_state.failed_stage is not None:
        message += f"**Failed during:** {backup_state.failed_stage.name}\n"

    if backup_state.backup_file and backup_state.backup_file.exists():
        size = format_bytes(backup_state.backup_file.stat().st_size)
        message += f"**Backup Size:** {size}\n"

    message += f"**Backup Created:** {'Yes' if backup_state.backup_created else 'No'}\n"
    message += f"**Backup Verified:** {'Yes' if backup_state.backup_verified else 'No'}\n"
    message += f"**Rclone Sync:** {'Yes' if backup_state.rclone_completed else 'No'}\n"

    if backup_state.warnings:
        message += f"\n**Warnings ({len(backup_state.warnings)}):**\n"
        for warning in backup_state.warnings[:5]:
            message += f"• {warning}\n"
        if len(backup_state.warnings) > 5:
            message += f"• ... and {len(backup_state.warnings) - 5} more\n"

    if backup_state.errors:
        message += f"\n**Errors ({len(backup_state.errors)}):**\n"
        for error in backup_state.errors[:5]:
            message += f"• {error}\n"
        if len(backup_state.errors) > 5:
            message += f"• ... and {len(backup_state.errors) - 5} more\n"

    if privatebin_link:
        message += f"\n🔗 **[View Full Log]({privatebin_link})**"

    send_discord_notification(status, message, color, f"Backup Status: {status}")


# -----------------------------------------------------------------------------
# Restore Mode
# -----------------------------------------------------------------------------


def _decrypt_stage(
    backup_file: Path, identity: Path, password_file: Path
) -> tuple[str, list[str]]:
    """Build the first pipeline stage for restoring a backup, based on its
    format: .age (current) or legacy openssl .enc."""
    name = backup_file.name
    if name.endswith(".age"):
        if not identity.is_file():
            raise BackupError(
                f"age identity file not found: {identity} (use --identity PATH)"
            )
        return (
            "age",
            [resolve_command("age"), "-d", "-i", str(identity)],
        )
    if name.endswith(".enc"):
        if not password_file.is_file():
            raise BackupError(
                f"Password file not found: {password_file} (use --password-file PATH)"
            )
        # Must match exactly how legacy backups were created (no -iter flag).
        return (
            "openssl",
            [
                resolve_command("openssl"),
                "enc",
                "-d",
                "-aes-256-cbc",
                "-md",
                "sha256",
                "-pass",
                f"file:{password_file.resolve()}",
                "-pbkdf2",
            ],
        )
    raise BackupError(
        f"Unsupported backup format: {name} (expected *.tar.gz.age or *.tar.gz.enc)"
    )


def run_restore(
    backup_file: Path,
    output_dir: Path | None,
    list_only: bool,
    force: bool,
    identity: Path,
    password_file: Path,
) -> int:
    """List or extract a backup archive. Returns a process exit code."""
    if not backup_file.is_file():
        log.critical(f"Backup file not found: {backup_file}")
        return 1

    try:
        decrypt = _decrypt_stage(backup_file, identity, password_file)
    except BackupError as e:
        log.critical(str(e))
        return 1

    decompress_tool = find_command("pigz") or find_command("gzip") or "gzip"
    decompress = (Path(decompress_tool).name, [decompress_tool, "-d"])

    stdout_final: int | None
    if list_only:
        tar_stage = ("tar", [resolve_tar(), "-tf", "-"])
        stdout_final = None  # inherit stdout: print the listing directly
    else:
        if output_dir is None:
            log.critical("--output-dir is required when extracting (or use --list).")
            return 1
        output_dir.mkdir(parents=True, exist_ok=True)
        if any(output_dir.iterdir()) and not force:
            log.critical(
                f"Output directory {output_dir} is not empty. Use --force to extract anyway."
            )
            return 1
        tar_stage = ("tar", [resolve_tar(), "-xf", "-", "-C", str(output_dir)])
        stdout_final = subprocess.DEVNULL
        log.info(f"Extracting {backup_file.name} to {output_dir}...")

    stages = [decrypt, decompress, tar_stage]
    try:
        with open(backup_file, "rb") as f_in:
            result = run_pipeline(
                stages, timeout=RESTORE_TIMEOUT, stdin_first=f_in, stdout_final=stdout_final
            )
    except OperationTimeout as e:
        log.critical(str(e))
        return 1
    except KeyboardInterrupt:
        log.warning("Restore interrupted.")
        terminate_active_processes()
        return 130

    failed = False
    for (stage_name, _), rc, err in zip(stages, result.returncodes, result.stderr):
        if rc != 0:
            log.critical(f"{stage_name} failed with exit code {rc}: {err}")
            failed = True
    if failed:
        return 1

    if list_only:
        log.info("Archive listing completed successfully.")
    else:
        log.info(f"Restore completed successfully into {output_dir}")
        if os.geteuid() != 0:
            log.warning(
                "Not running as root: original file ownership could not be restored."
            )
    return 0


# -----------------------------------------------------------------------------
# Backup Run Orchestration
# -----------------------------------------------------------------------------


def _finalize_run(success: bool, log_file: Path | None) -> None:
    """Restore services, remove the maintenance window, and send the final
    notification. Only called when the run actually got past pre-flight."""
    log.info("=" * 60)
    log.info("FINALIZATION: Ensuring all services are restored")
    log.info("=" * 60)

    backup_state.stage = BackupStage.CONTAINER_RESTART_ALL

    if CONFIG.docker_enabled and not dry_run_mode:
        try:
            plex_compose, other_compose = get_docker_compose_files()

            if not backup_state.plex_started and plex_compose:
                log.warning("Plex was not started. Starting now...")
                if manage_docker_services(plex_compose, "start"):
                    backup_state.plex_started = True

            if other_compose:
                log.info("Starting all other services...")
                if manage_docker_services(other_compose, "start"):
                    backup_state.other_services_started = True

            # Verify services are running
            time.sleep(5)
            running = get_running_container_ids()
            log.info(f"Service restart complete. {len(running)} container(s) running.")

            if not running:
                log.critical(
                    "No containers running after restart! Attempting emergency recovery..."
                )
                emergency_container_restart()

        except Exception as e:
            log.critical(f"Failed to restart services: {e}")
            emergency_container_restart()

    # Remove maintenance window
    backup_state.stage = BackupStage.CLEANUP
    if backup_state.maintenance_window_id is not None:
        log.info("Removing maintenance window...")
        remove_backup_maintenance_window()

    # Send final status notification (single PrivateBin upload happens here)
    send_final_status_notification(success, log_file)

    backup_state.stage = BackupStage.COMPLETE


def _run_backup(timestamp: str, log_file: Path | None) -> int:
    """Execute the full backup flow. The lock is already held."""
    watchdog = start_watchdog()
    success = False
    finalization_needed = False

    try:
        # Stage: Pre-flight checks
        backup_state.stage = BackupStage.PREFLIGHT
        pre_flight_checks()
        finalization_needed = True

        # Stage: Create maintenance window
        backup_state.stage = BackupStage.MAINTENANCE_WINDOW
        backup_state.maintenance_window_id = create_backup_maintenance_window()

        # Get compose files
        if CONFIG.docker_enabled:
            plex_compose, other_compose = get_docker_compose_files()
        else:
            plex_compose, other_compose = [], []
        all_compose_files = plex_compose + other_compose

        # Log rotation (backup rotation happens after verification)
        _ = rotate_items(CONFIG.log_root_dir, "*-backupScript.log", CONFIG.retention_logs)

        # Stage: Container shutdown
        backup_state.stage = BackupStage.CONTAINER_SHUTDOWN
        if CONFIG.docker_enabled:
            backup_state.containers_stopped = ensure_all_containers_stopped(
                all_compose_files
            )
            # Note: We continue even if some containers couldn't be stopped
            if not backup_state.containers_stopped:
                log.warning("Proceeding with backup despite container shutdown issues")

        check_shutdown_requested()

        # Stage: Backup creation
        backup_state.stage = BackupStage.BACKUP_CREATION
        backup_filename = f"{timestamp.replace('_', '-')}_backup.tar.gz.age"
        backup_file = CONFIG.backup_root_dir / backup_filename
        backup_state.backup_file = backup_file
        if not dry_run_mode:
            CONFIG.backup_root_dir.mkdir(parents=True, exist_ok=True)

        try:
            if create_backup(backup_file):
                backup_state.backup_created = True
                log.info(f"Local backup created: {backup_file}")
        except BackupCreationError as e:
            backup_state.add_error(str(e))
            log.critical(f"Backup creation failed: {e}")
            # This is critical - we need to restart containers and exit
            raise

        # Stage: Start Plex first (priority service)
        backup_state.stage = BackupStage.CONTAINER_RESTART_PLEX
        if CONFIG.docker_enabled and plex_compose:
            log.info("Restarting Plex (priority service)...")
            if manage_docker_services(plex_compose, "start"):
                backup_state.plex_started = True

        check_shutdown_requested()

        # Stage: Backup verification
        backup_state.stage = BackupStage.BACKUP_VERIFICATION
        try:
            if verify_backup(backup_file):
                backup_state.backup_verified = True
                set_permissions(backup_file)
                _ = write_sha256_manifest(backup_file)
        except BackupVerificationError as e:
            backup_state.add_error(str(e))
            log.critical(f"Backup verification failed: {e}")
            # Continue anyway - we have the backup even if verification failed

        # Rotate backups only now that the new one exists (and hopefully
        # verified) - a failed run never eats into existing good backups.
        _ = rotate_items(
            CONFIG.backup_root_dir,
            ["*.tar.gz.age", "*.tar.gz.enc"],
            CONFIG.retention_backups,
        )
        cleanup_orphan_manifests(CONFIG.backup_root_dir)

        check_shutdown_requested()

        # Stage: Rclone sync
        backup_state.stage = BackupStage.RCLONE_SYNC
        if CONFIG.rclone_enabled and backup_state.backup_created:
            rclone_summary = run_rclone_sync_with_retry(log_file)

            if rclone_summary["status"] == "success":
                backup_state.rclone_completed = True
                log.info("Rclone sync completed successfully")

                success_timestamp = datetime.datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                message = (
                    f"**Status Details**\n"
                    f"✅ Sync completed successfully\n"
                    f"⏱️ Duration: {rclone_summary['duration']}\n"
                    f"📦 Data: {rclone_summary['transferred_data']}\n"
                    f"📄 Files: {rclone_summary['transferred_files']}\n"
                    f"🔍 Checks: {rclone_summary['checks_count']} / {rclone_summary['total_checks']}\n\n"
                    f"**Sync Info**\n"
                    f"Source: `{CONFIG.backup_root_dir}`\n"
                    f"Destination: `{CONFIG.rclone_remote_dest}`\n\n"
                    f"**Timestamp**\n"
                    f"{success_timestamp}"
                )
                send_discord_notification(
                    "Success", message, COLOR_GREEN, "Rclone Sync Status: Success"
                )

            elif rclone_summary["status"] == "failed":
                backup_state.add_error(
                    f"Rclone sync failed: {rclone_summary.get('last_error', 'Unknown error')}"
                )
                message = (
                    f"❌ **Sync failed after {rclone_summary.get('attempts', '?')} attempts**\n"
                    f"⏱️ **Duration:** {rclone_summary['duration']}\n"
                    f"**Exit Code:** {rclone_summary['exit_code']}\n"
                    f"⚠️ **Last Error:**\n```\n{rclone_summary['last_error']}\n```"
                )
                send_discord_notification(
                    "Failed", message, COLOR_RED, "Rclone Sync Status: Failed"
                )

        # Determine overall success
        success = backup_state.backup_created and not backup_state.has_critical_errors

    except PreFlightError as e:
        log.critical(f"Pre-flight checks failed: {e}")
        backup_state.add_error(str(e))
        send_discord_notification(
            "Pre-flight Failed",
            f"Backup aborted before touching any services:\n```\n{e}\n```",
            COLOR_RED,
        )

    except KeyboardInterrupt:
        log.warning("Script interrupted by user or signal")
        backup_state.add_error("Script interrupted")

    except Exception as e:
        log.critical(f"An unexpected critical error occurred: {e}", exc_info=True)
        backup_state.add_error(f"Unexpected error: {e}")
        send_discord_notification(
            "Critical Failure",
            f"The script encountered a fatal error:\n```\n{e}\n```",
            COLOR_RED,
        )

    finally:
        watchdog.cancel()
        if finalization_needed:
            _finalize_run(success, log_file)

        log.info("=" * 60)
        log.info(f"Backup script finished. Success: {success}")
        log.info(f"Total duration: {format_duration(backup_state.elapsed_time)}")
        log.info("=" * 60)

    return 0 if success else 1


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="overengineered-backup-script",
        description="A robust server backup and sync script (tar | pigz | age).",
    )
    _ = parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    _ = parser.add_argument(
        "-d", "--dry-run", action="store_true", help="Preview the run without changing anything."
    )
    _ = parser.add_argument(
        "--verbose", action="store_true", help="Enable debug logging."
    )
    _ = parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"Config file path (default: {DEFAULT_CONFIG_PATH}).",
    )
    _ = parser.add_argument(
        "--no-docker", action="store_true", help="Skip Docker stop/start."
    )
    _ = parser.add_argument(
        "--no-upload", action="store_true", help="Skip the rclone off-site upload."
    )
    _ = parser.add_argument(
        "--backup-only",
        action="store_true",
        help="Backup only: skip Docker, rclone upload, and Uptime Kuma.",
    )
    _ = parser.add_argument(
        "--print-default-config",
        action="store_true",
        help="Print a commented example config file and exit.",
    )

    subparsers = parser.add_subparsers(dest="command")
    restore = subparsers.add_parser(
        "restore", help="Decrypt and restore a backup archive."
    )
    _ = restore.add_argument("backup_file", type=Path, help="Backup archive to restore.")
    _ = restore.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Directory to extract into (required unless --list).",
    )
    _ = restore.add_argument(
        "--list",
        action="store_true",
        dest="list_contents",
        help="List archive contents instead of extracting.",
    )
    _ = restore.add_argument(
        "--force",
        action="store_true",
        help="Extract even if the output directory is not empty.",
    )
    _ = restore.add_argument(
        "--config", type=Path, default=None, metavar="PATH", help="Config file path."
    )
    _ = restore.add_argument(
        "--identity",
        type=Path,
        default=None,
        metavar="PATH",
        help="age identity file (default: from config).",
    )
    _ = restore.add_argument(
        "--password-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="Password file for legacy .enc backups (default: from config).",
    )
    _ = restore.add_argument(
        "--verbose", action="store_true", help="Enable debug logging."
    )
    return parser


def cmd_backup(args: argparse.Namespace) -> int:
    global CONFIG, dry_run_mode, backup_state

    setup_console_logging(verbose=bool(args.verbose))

    try:
        CONFIG = load_config(args.config)
    except ConfigError as e:
        log.critical(str(e))
        return 2

    dry_run_mode = bool(args.dry_run)
    if args.no_docker or args.backup_only:
        CONFIG.docker_enabled = False
    if args.no_upload or args.backup_only:
        CONFIG.rclone_enabled = False
    if args.backup_only:
        CONFIG.uptime_kuma_enabled = False

    backup_state = BackupState()
    setup_signal_handlers()

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file: Path | None = None
    try:
        CONFIG.log_root_dir.mkdir(parents=True, exist_ok=True)
        log_file = CONFIG.log_root_dir / f"{timestamp}-backupScript.log"
        add_file_logging(log_file)
        set_log_permissions(log_file)
    except OSError as e:
        log.warning(
            f"Cannot write log file under {CONFIG.log_root_dir} ({e}); "
            f"continuing with console logging only."
        )
        log_file = None

    if dry_run_mode:
        log.info("--- Starting DRY RUN ---")

    # Acquire the lock BEFORE entering the run's try/finally. A concurrent
    # run must exit here without touching Docker, notifications, or the
    # other run's lock file.
    lock_ctx = acquire_lock(CONFIG.lock_file)
    try:
        _ = lock_ctx.__enter__()
    except FileExistsError:
        log.critical(f"Script is already running. Lock file exists: {CONFIG.lock_file}")
        return 1

    try:
        return _run_backup(timestamp, log_file)
    finally:
        _ = lock_ctx.__exit__(None, None, None)


def cmd_restore(args: argparse.Namespace) -> int:
    global CONFIG

    setup_console_logging(verbose=bool(args.verbose))

    try:
        CONFIG = load_config(args.config)
    except ConfigError as e:
        log.critical(str(e))
        return 2

    identity = args.identity or CONFIG.age_identity_file
    password_file = args.password_file or CONFIG.legacy_password_file

    return run_restore(
        backup_file=args.backup_file,
        output_dir=args.output_dir,
        list_only=bool(args.list_contents),
        force=bool(args.force),
        identity=identity,
        password_file=password_file,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "print_default_config", False):
        _ = sys.stdout.write(default_config_toml())
        return 0

    if args.command == "restore":
        return cmd_restore(args)

    return cmd_backup(args)


if __name__ == "__main__":
    sys.exit(main())
