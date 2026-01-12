# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "pytz",
#     "requests",
#     "uptime-kuma-api",
# ]
# ///

#!/usr/bin/env python3
#
# -----------------------------------------------------------------------------
# server-backup.py
#
# A robust, automated backup and off-site upload script using Python.
#
# Features:
# - Creates local encrypted backups using tar, pigz/gzip, and OpenSSL.
# - Manages Docker services, with priority restart for critical containers.
# - Uploads backups to a cloud remote using rclone.
# - Sends detailed status notifications to a Discord webhook.
# - Optionally uploads full logs to PrivateBin for easy debugging.
# - Includes robust error handling, log rotation, and concurrency locking.
#
# Requirements:
# - Python 3.13+
# - rclone, tar, pigz/gzip, openssl, docker
# - Python 'requests' library (`uv pip install requests`)
# - (Optional) privatebin (from gearnode/privatebin)
# -----------------------------------------------------------------------------

import argparse
import atexit
import contextlib
import datetime
import json
import logging
import os
import pwd
import grp
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from types import FrameType
from typing import TypedDict, Callable, TypeVar, cast, TYPE_CHECKING


# This script requires the 'requests' library for Discord notifications.
try:
    import requests  # pyright: ignore[reportMissingModuleSource]
except ImportError:
    print(
        "Error: The 'requests' library is not installed. Please install it using: pip install requests"
    )
    sys.exit(1)

# Uptime Kuma integration dependencies
try:
    import pytz  # pyright: ignore[reportMissingModuleSource]
    from uptime_kuma_api import UptimeKumaApi, MaintenanceStrategy  # pyright: ignore[reportMissingImports, reportUnknownVariableType]
except ImportError:
    print(
        "Warning: Uptime Kuma dependencies not installed. Maintenance window functionality will be disabled."
    )
    print("To enable: pip install uptime-kuma-api pytz")
    pytz = None
    UptimeKumaApi = None
    MaintenanceStrategy = None


# -----------------------------------------------------------------------------
# Custom Exceptions for Better Error Handling
# -----------------------------------------------------------------------------


class BackupError(Exception):
    """Base exception for backup-related errors."""

    pass


class ContainerShutdownError(BackupError):
    """Raised when containers cannot be stopped."""

    remaining_containers: list[str]

    def __init__(self, message: str, remaining_containers: list[str] | None = None):
        super().__init__(message)
        self.remaining_containers = remaining_containers or []


class BackupCreationError(BackupError):
    """Raised when backup creation fails."""

    pass


class BackupVerificationError(BackupError):
    """Raised when backup verification fails."""

    pass


class RcloneSyncError(BackupError):
    """Raised when rclone sync fails."""

    exit_code: int
    is_retryable: bool

    def __init__(self, message: str, exit_code: int, is_retryable: bool = True):
        super().__init__(message)
        self.exit_code = exit_code
        self.is_retryable = is_retryable


class DiskSpaceError(BackupError):
    """Raised when there's insufficient disk space."""

    pass


class TimeoutError(BackupError):
    """Raised when an operation times out."""

    pass


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# --- Backup Ownership ---
BACKUP_USER = "aplex"
BACKUP_GROUP = "aplex"

# --- Local Backup Paths & Retention ---
BACKUP_ROOT_DIR = Path("/dir/to/backups")
LOG_ROOT_DIR = Path("/home/user/scripts/logs/backupScript")
LOCK_FILE = Path(f"/tmp/{Path(__file__).name}.lock")
RETENTION_COUNT_BACKUPS = 3
RETENTION_COUNT_LOGS = 7

# --- Encryption & Compression ---
BACKUP_PASSWORD_FILE = Path("/root/.backup_password")
BACKUP_COMPRESSION_TOOL = "pigz"
BACKUP_COMPRESSION_LEVEL = 3

# --- Docker Management ---
DOCKER_ENABLE_STOP_START = True
DOCKER_SHUTDOWN_METHOD = "down"
DOCKER_STACKS_DIR = Path("/home/user/stacks")
DOCKER_STOP_TIMEOUT = 2
DOCKER_START_DELAY = 30
DOCKER_FORCE_STOP_TIMEOUT = 30

# --- NEW: Enhanced Docker Shutdown Configuration ---
DOCKER_SHUTDOWN_OVERALL_TIMEOUT = 300  # 5 minutes max for entire shutdown process
DOCKER_SHUTDOWN_MAX_RETRIES = 3  # Retry entire shutdown process this many times
DOCKER_SHUTDOWN_RETRY_DELAY = 10  # Seconds between shutdown retries
DOCKER_VERIFY_SHUTDOWN_INTERVAL = 2  # Seconds between verification checks
DOCKER_KILL_WAIT_TIME = 5  # Seconds to wait after kill before checking

# --- Priority Backup & Services (Plex) ---
PLEX_DATA_DIR = Path("/opt/docker-all/mediaServers/plex")
PLEX_COMPOSE_FILE = DOCKER_STACKS_DIR / "006-plex-media-server/compose.yaml"

# --- Backup Sources & Exclusions ---
BACKUP_DIRS = [
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
EXCLUDE_DIRS = [Path("/opt/docker-all/miscSoftware/immich")]

# --- Rclone & Off-site Upload Configuration ---
ENABLE_RCLONE_UPLOAD = True
RCLONE_SOURCE_DIR = BACKUP_ROOT_DIR
RCLONE_REMOTE_DEST = "pcrypt:backup"
BANDWIDTH_LIMIT = "50M"
RCLONE_FILTERS: list[str] = []

# --- NEW: Enhanced Rclone Configuration ---
RCLONE_MAX_RETRIES = 5  # Number of retry attempts for rclone
RCLONE_RETRY_DELAY = 30  # Initial delay between retries (exponential backoff)
RCLONE_RETRY_MAX_DELAY = 300  # Maximum delay between retries
RCLONE_OVERALL_TIMEOUT = 7200  # 2 hours max for rclone operation

# --- Discord & Notification Configuration ---
DISCORD_WEBHOOK_URL = "***REMOVED***"
DISCORD_USERNAME = "Server Backup & Sync"
DISCORD_AVATAR_URL = ""

# --- PrivateBin Configuration ---
ENABLE_PRIVATEBIN_UPLOAD = True
PRIVATEBIN_CLI_PATH = "privatebin"

# --- Uptime Kuma Maintenance Window Configuration ---
ENABLE_UPTIME_KUMA_MAINTENANCE = True
UPTIME_KUMA_URL = "https://uptimekuma.cccp.ps"
UPTIME_KUMA_USERNAME = "engels74"
UPTIME_KUMA_PASSWORD = "***REMOVED***"
UPTIME_KUMA_STATUS_PAGE_SLUG = "cccp-ps"
MAINTENANCE_ID_FILE = Path("/tmp/backup_maintenance_id.txt")

# --- NEW: Disk Space Configuration ---
MIN_FREE_SPACE_GB = 50  # Minimum free space required before starting backup
MIN_FREE_SPACE_PERCENT = 10  # Minimum free space percentage

# --- NEW: Script Timeout Configuration ---
SCRIPT_OVERALL_TIMEOUT = 14400  # 4 hours max for entire script


# -----------------------------------------------------------------------------
# Type Definitions for Uptime Kuma
# -----------------------------------------------------------------------------

T = TypeVar("T")


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


class RcloneStats(TypedDict):
    transfers: int
    bytes: int
    errors: int
    checks: int
    totalBytes: int


class RcloneLogEntry(TypedDict):
    level: str
    msg: str
    stats: RcloneStats


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


@dataclass
class BackupState:
    """Maintains state throughout the backup process for proper recovery."""

    stage: BackupStage = BackupStage.INIT
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
dry_run_mode = False
backup_state = BackupState()
shutdown_requested = False
lock_fd: int | None = None


# -----------------------------------------------------------------------------
# Signal Handling for Graceful Shutdown
# -----------------------------------------------------------------------------


def signal_handler(signum: int, _frame: FrameType | None) -> None:
    """Handle termination signals gracefully."""
    global shutdown_requested

    sig_name = signal.Signals(signum).name
    log.warning(f"Received signal {sig_name}. Initiating graceful shutdown...")
    shutdown_requested = True

    # If we're in critical sections, we need to complete them
    if backup_state.stage in (
        BackupStage.BACKUP_CREATION,
        BackupStage.CONTAINER_SHUTDOWN,
    ):
        log.warning(
            f"Currently in {backup_state.stage.name} - will complete before shutting down"
        )
    else:
        log.warning("Will shutdown after current operation completes")


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


# -----------------------------------------------------------------------------
# Lock File Management with Guaranteed Cleanup
# -----------------------------------------------------------------------------


@contextlib.contextmanager
def acquire_lock(lock_path: Path):
    """Context manager for lock file with guaranteed cleanup."""
    global lock_fd

    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        # Register cleanup with atexit as a backup
        _ = atexit.register(lambda: cleanup_lock(lock_path))
        yield lock_fd
    except FileExistsError:
        # Check if the lock is stale (process that created it is dead)
        if is_stale_lock(lock_path):
            log.warning("Found stale lock file. Removing and retrying...")
            lock_path.unlink(missing_ok=True)
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            _ = atexit.register(lambda: cleanup_lock(lock_path))
            yield lock_fd
        else:
            raise
    finally:
        cleanup_lock(lock_path)


def cleanup_lock(lock_path: Path) -> None:
    """Clean up lock file."""
    global lock_fd

    if lock_fd is not None:
        with contextlib.suppress(OSError):
            os.close(lock_fd)
        lock_fd = None

    with contextlib.suppress(OSError):
        lock_path.unlink(missing_ok=True)


def is_stale_lock(lock_path: Path) -> bool:
    """Check if lock file is stale (owning process is dead)."""
    try:
        # Check if lock file is older than script timeout
        stat = lock_path.stat()
        age = time.time() - stat.st_mtime
        if age > SCRIPT_OVERALL_TIMEOUT:
            return True

        # Could also check /proc for the PID if stored in lock file
        return False
    except OSError:
        return True


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

        if free_gb < MIN_FREE_SPACE_GB:
            return (
                False,
                f"Insufficient disk space: {free_gb:.1f}GB free (minimum: {MIN_FREE_SPACE_GB}GB)",
            )

        if free_percent < MIN_FREE_SPACE_PERCENT:
            return (
                False,
                f"Insufficient disk space: {free_percent:.1f}% free (minimum: {MIN_FREE_SPACE_PERCENT}%)",
            )

        return True, f"Disk space OK: {free_gb:.1f}GB ({free_percent:.1f}%) free"

    except OSError as e:
        return False, f"Failed to check disk space: {e}"


# -----------------------------------------------------------------------------
# Timeout Decorator for Operations
# -----------------------------------------------------------------------------


class OperationTimeout(Exception):
    """Raised when an operation times out."""

    pass


def run_with_timeout(
    func: Callable[..., T],
    timeout: float,
    *args: object,
    **kwargs: object,
) -> T:
    """
    Run a function with a timeout.

    Uses threading for timeout (works with subprocess calls).
    """
    result: list[T] = []
    exception: list[Exception] = []

    def target() -> None:
        try:
            result.append(func(*args, **kwargs))
        except Exception as e:
            exception.append(e)

    thread = threading.Thread(target=target)
    thread.daemon = True
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        raise OperationTimeout(f"Operation timed out after {timeout} seconds")

    if exception:
        raise exception[0]

    if not result:
        raise RuntimeError("Operation completed without result or exception")

    return result[0]


# -----------------------------------------------------------------------------
# Uptime Kuma Integration Classes
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
    api: object | None

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        max_retries: int = 5,
        initial_delay: float = 1.0,
        max_delay: float = 30.0,
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

    def connect(self) -> object:
        """Establish connection to Uptime Kuma with retries."""
        if UptimeKumaApi is None:
            raise RuntimeError("Uptime Kuma API not available")

        retry_count = 0
        delay = self.initial_delay

        while retry_count < self.max_retries:
            try:
                if self.api is not None:  # pyright: ignore[reportUnknownMemberType]
                    with contextlib.suppress(Exception):
                        _ = self.api.disconnect()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownVariableType, reportUnknownMemberType]

                self.api = UptimeKumaApi(self.url, timeout=30)
                _ = self.api.login(self.username, self.password)  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess, reportUnknownVariableType, reportUnknownMemberType]
                log.info("Successfully connected to Uptime Kuma")
                return self.api  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

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

    def retry_operation(
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

    def __enter__(self) -> "UptimeKumaRetry":
        _ = self.connect()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        if self.api is not None:
            with contextlib.suppress(Exception):
                _ = self.api.disconnect()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownVariableType, reportUnknownMemberType]


# -----------------------------------------------------------------------------
# Uptime Kuma Maintenance Window Functions
# -----------------------------------------------------------------------------


def create_backup_maintenance_window() -> int | None:
    """Create a maintenance window for the backup process."""
    if not ENABLE_UPTIME_KUMA_MAINTENANCE or dry_run_mode:
        if dry_run_mode:
            log.info("DRY RUN: Skipping maintenance window creation.")
        return None

    if UptimeKumaApi is None or pytz is None or MaintenanceStrategy is None:
        log.warning(
            "Uptime Kuma dependencies not available. Skipping maintenance window creation."
        )
        return None

    try:
        log.info("Creating Uptime Kuma maintenance window for backup...")

        with UptimeKumaRetry(
            UPTIME_KUMA_URL,
            UPTIME_KUMA_USERNAME,
            UPTIME_KUMA_PASSWORD,
            max_retries=3,
            initial_delay=1.0,
            max_delay=10.0,
            backoff_factor=2.0,
        ) as kuma:
            server_info = cast(
                ServerInfo,
                cast(object, kuma.retry_operation(kuma.api.info)),  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess, reportUnknownMemberType, reportUnknownArgumentType]
            )
            server_timezone = pytz.timezone(str(server_info["serverTimezone"]))
            log.info(f"Using server timezone: {server_timezone}")

            maintenance = cast(
                MaintenanceResponse,
                cast(
                    object,
                    kuma.retry_operation(
                        kuma.api.add_maintenance,  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess, reportUnknownMemberType, reportUnknownArgumentType]
                        title="Server Backup in Progress",
                        description="Automated server backup is currently running. Services may be temporarily unavailable.",
                        strategy=MaintenanceStrategy.MANUAL,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                        active=True,
                        timezoneOption=str(server_timezone),
                    ),
                ),
            )

            maintenance_id = int(maintenance["maintenanceID"])
            log.info(f"Maintenance window created with ID: {maintenance_id}")

            monitors = cast(
                MonitorList,
                cast(object, kuma.retry_operation(kuma.api.get_monitors)),  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess, reportUnknownMemberType, reportUnknownArgumentType]
            )
            monitor_ids: MonitorIdList = [{"id": monitor["id"]} for monitor in monitors]

            if monitor_ids:
                log.info(f"Adding {len(monitor_ids)} monitors to maintenance window")
                _ = kuma.retry_operation(  # pyright: ignore[reportUnknownVariableType]
                    kuma.api.add_monitor_maintenance,  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess, reportUnknownMemberType, reportUnknownArgumentType]
                    maintenance_id,
                    monitor_ids,
                )
                log.info("Monitors added to maintenance window")

            status_pages = cast(
                StatusPageList,
                cast(object, kuma.retry_operation(kuma.api.get_status_pages)),  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess, reportUnknownMemberType, reportUnknownArgumentType]
            )
            status_page = next(
                (
                    page
                    for page in status_pages
                    if page["slug"] == UPTIME_KUMA_STATUS_PAGE_SLUG
                ),
                None,
            )

            if status_page:
                log.info(
                    f"Adding status page '{UPTIME_KUMA_STATUS_PAGE_SLUG}' to maintenance window"
                )
                _ = kuma.retry_operation(  # pyright: ignore[reportUnknownVariableType]
                    kuma.api.add_status_page_maintenance,  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess, reportUnknownMemberType, reportUnknownArgumentType]
                    maintenance_id,
                    [{"id": status_page["id"]}],
                )
                log.info("Status page added to maintenance window")
            else:
                log.warning(f"Status page '{UPTIME_KUMA_STATUS_PAGE_SLUG}' not found")

            try:
                MAINTENANCE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(MAINTENANCE_ID_FILE, "w") as f:
                    _ = f.write(str(maintenance_id))
                log.info(f"Maintenance ID saved to {MAINTENANCE_ID_FILE}")
            except Exception as e:
                log.error(f"Failed to save maintenance ID: {e}")

            return maintenance_id

    except Exception as e:
        log.error(f"Failed to create maintenance window: {e}")
        return None


def remove_backup_maintenance_window() -> None:
    """Remove the backup maintenance window."""
    if not ENABLE_UPTIME_KUMA_MAINTENANCE or dry_run_mode:
        if dry_run_mode:
            log.info("DRY RUN: Skipping maintenance window removal.")
        return

    if UptimeKumaApi is None:
        log.warning(
            "Uptime Kuma dependencies not available. Skipping maintenance window removal."
        )
        return

    try:
        if not MAINTENANCE_ID_FILE.exists():
            log.warning(
                "No maintenance ID file found. Maintenance window may not have been created."
            )
            return

        with open(MAINTENANCE_ID_FILE, "r") as f:
            maintenance_id = int(f.read().strip())

        log.info(f"Removing maintenance window with ID: {maintenance_id}")

        with UptimeKumaRetry(
            UPTIME_KUMA_URL,
            UPTIME_KUMA_USERNAME,
            UPTIME_KUMA_PASSWORD,
            max_retries=3,
            initial_delay=1.0,
            max_delay=10.0,
            backoff_factor=2.0,
        ) as kuma:
            result = cast(
                DeleteMaintenanceResponse,
                cast(
                    object,
                    kuma.retry_operation(
                        kuma.api.delete_maintenance,  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess, reportUnknownMemberType, reportUnknownArgumentType]
                        maintenance_id,
                    ),
                ),
            )
            log.info(f"Maintenance window deleted. Result: {result}")

        try:
            MAINTENANCE_ID_FILE.unlink()
            log.info("Maintenance ID file removed")
        except FileNotFoundError:
            log.warning("Maintenance ID file already removed")
        except Exception as e:
            log.warning(f"Failed to remove maintenance ID file: {e}")

    except Exception as e:
        log.error(f"Failed to remove maintenance window: {e}")


# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------


def setup_logging(log_file: Path) -> None:
    """Configures logging to both console and a file."""
    log.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    log.addHandler(logging.StreamHandler(sys.stdout))
    log.addHandler(logging.FileHandler(log_file))
    for handler in log.handlers:
        handler.setFormatter(formatter)


def send_discord_notification(
    status: str, message: str, color: int, title_override: str | None = None
) -> None:
    """Sends a formatted notification to the configured Discord webhook."""
    if not DISCORD_WEBHOOK_URL:
        return
    if dry_run_mode:
        log.info("DRY RUN: Skipping Discord notification.")
        return

    title = title_override or f"Local Backup Status: {status}"
    payload = {
        "username": DISCORD_USERNAME,
        "avatar_url": DISCORD_AVATAR_URL,
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
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
            response.raise_for_status()
            return
        except requests.RequestException as e:
            if attempt == 2:
                log.error(f"Failed to send Discord notification after 3 attempts: {e}")
            else:
                time.sleep(retry_delays[attempt])


def upload_log_to_privatebin(log_file: Path) -> str | None:
    """Uploads the log file to PrivateBin and returns the URL."""
    if not ENABLE_PRIVATEBIN_UPLOAD or dry_run_mode:
        return None
    if not shutil.which(PRIVATEBIN_CLI_PATH):
        log.warning(
            f"PrivateBin CLI not found at '{PRIVATEBIN_CLI_PATH}'. Skipping log upload."
        )
        return None

    log.info("Uploading log file to PrivateBin...")
    try:
        with open(log_file, "r") as f:
            result = subprocess.run(
                [PRIVATEBIN_CLI_PATH, "create"],
                stdin=f,
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )
        log_url = result.stdout.strip()
        log.info(f"Log uploaded successfully: {log_url}")
        return log_url
    except subprocess.TimeoutExpired:
        log.error("PrivateBin upload timed out")
        return None
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
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


# -----------------------------------------------------------------------------
# Rclone with Retry Logic
# -----------------------------------------------------------------------------


def run_rclone_sync_with_retry(log_file: Path) -> dict[str, str | int]:
    """Runs the rclone sync process with retry logic."""
    log.info("--- Starting Rclone Off-site Upload ---")

    if not ENABLE_RCLONE_UPLOAD:
        log.info("Rclone upload is disabled. Skipping.")
        return {"status": "skipped"}

    if dry_run_mode:
        log.info("DRY RUN: Skipping rclone execution.")
        return {"status": "skipped"}

    start_time = time.monotonic()
    retry_delay = RCLONE_RETRY_DELAY
    last_error: str = ""

    # Send start notification
    start_timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    start_message = (
        f"**Status Details**\n"
        f"Beginning sync operation...\n\n"
        f"**Sync Info**\n"
        f"Source: `{RCLONE_SOURCE_DIR}`\n"
        f"Destination: `{RCLONE_REMOTE_DEST}`\n\n"
        f"**Timestamp**\n"
        f"{start_timestamp}"
    )
    send_discord_notification(
        "Started",
        start_message,
        16776960,
        title_override="Rclone Sync Status: Started",
    )

    for attempt in range(1, RCLONE_MAX_RETRIES + 1):
        check_shutdown_requested()

        if attempt > 1:
            log.info(f"Rclone retry attempt {attempt}/{RCLONE_MAX_RETRIES}")
            log.info(f"Waiting {retry_delay} seconds before retry...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, RCLONE_RETRY_MAX_DELAY)

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
            last_error = f"Operation timed out after {RCLONE_OVERALL_TIMEOUT} seconds"

        except Exception as e:
            log.error(f"Rclone attempt {attempt} failed with exception: {e}")
            last_error = str(e)

    # All retries exhausted
    duration = time.monotonic() - start_time
    return {
        "status": "failed",
        "exit_code": 1,
        "duration": format_duration(duration),
        "attempts": RCLONE_MAX_RETRIES,
        "last_error": f"All {RCLONE_MAX_RETRIES} attempts failed. Last error: {last_error}",
        "transferred": "0 files, 0 B",
        "transferred_files": "0 files",
        "transferred_data": "0 B",
        "errors": str(RCLONE_MAX_RETRIES),
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


def _execute_rclone_sync(log_file: Path) -> dict[str, str | int]:
    """Execute a single rclone sync operation."""
    start_time = time.monotonic()

    # Flush logs before rclone writes to the same file
    for handler in log.handlers:
        handler.flush()

    # Create a separate log file for this rclone run
    rclone_log = log_file.with_suffix(".rclone.log")

    command = [
        "rclone",
        "sync",
        str(RCLONE_SOURCE_DIR),
        RCLONE_REMOTE_DEST,
        "--create-empty-src-dirs",
        f"--bwlimit={BANDWIDTH_LIMIT}",
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
    if RCLONE_FILTERS:
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".txt", prefix="rclone-filters-"
        ) as f:
            filter_file = Path(f.name)
            _ = f.write("\n".join(RCLONE_FILTERS))
        command.append(f"--filter-from={filter_file}")

    log.info(f"Running rclone command: {' '.join(command)}")

    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=RCLONE_OVERALL_TIMEOUT,
        )
        exit_code = process.returncode
    except subprocess.TimeoutExpired:
        raise OperationTimeout(
            f"Rclone operation timed out after {RCLONE_OVERALL_TIMEOUT} seconds"
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
                        msg = log_entry.get("msg")
                        if isinstance(msg, str):
                            error_lines.append(msg)
                except json.JSONDecodeError:
                    continue

        # Append rclone log to main log
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


def pre_flight_checks() -> None:
    """Perform pre-flight checks before starting backup."""
    log.info("Performing pre-flight checks...")

    if os.geteuid() != 0:
        log.critical("This script must be run as root.")
        sys.exit(1)

    if not BACKUP_PASSWORD_FILE.is_file() or BACKUP_PASSWORD_FILE.stat().st_size == 0:
        log.critical(
            f"Backup password file not found or is empty: {BACKUP_PASSWORD_FILE}"
        )
        log.critical(
            "Please create it, add your password, and set permissions (chmod 600)."
        )
        sys.exit(1)

    deps = ["tar", "openssl"]
    if BACKUP_COMPRESSION_TOOL not in ["gzip", "pigz"]:
        log.critical(
            f"Invalid BACKUP_COMPRESSION_TOOL: {BACKUP_COMPRESSION_TOOL}. Must be 'gzip' or 'pigz'."
        )
        sys.exit(1)
    deps.append(BACKUP_COMPRESSION_TOOL)

    if DOCKER_ENABLE_STOP_START:
        deps.append("docker")
    if ENABLE_RCLONE_UPLOAD:
        deps.append("rclone")

    for dep in deps:
        if not shutil.which(dep):
            log.critical(f"Missing required dependency: {dep}")
            if dep == "pigz":
                log.critical(
                    "You can usually install it with: sudo apt-get install pigz"
                )
            sys.exit(1)

    try:
        _ = pwd.getpwnam(BACKUP_USER)
        _ = grp.getgrnam(BACKUP_GROUP)
    except KeyError as e:
        log.critical(f"Backup user/group not found: {e}")
        sys.exit(1)

    # Check disk space
    space_ok, space_msg = check_disk_space(BACKUP_ROOT_DIR.parent)
    if not space_ok:
        log.critical(space_msg)
        sys.exit(1)
    log.info(space_msg)

    log.info("Pre-flight checks passed.")


# -----------------------------------------------------------------------------
# Docker Management Functions
# -----------------------------------------------------------------------------


def get_docker_compose_files() -> tuple[list[Path], list[Path]]:
    """Get Docker compose files, separating Plex from others."""
    if not DOCKER_STACKS_DIR.is_dir():
        log.warning(f"Docker stacks directory not found at {DOCKER_STACKS_DIR}")
        return [], []

    all_files = sorted(
        list(DOCKER_STACKS_DIR.glob("**/compose.yaml"))
        + list(DOCKER_STACKS_DIR.glob("**/compose.yml"))
    )
    other_files = [f for f in all_files if f.resolve() != PLEX_COMPOSE_FILE.resolve()]
    log.info(f"Found {len(all_files)} total Docker compose files.")
    return [PLEX_COMPOSE_FILE], other_files


def get_running_container_ids() -> list[str]:
    """Get list of all running container IDs."""
    result = subprocess.run(
        ["docker", "ps", "-q"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        log.error(f"Failed to get running containers: {result.stderr}")
        return []
    return [cid.strip() for cid in result.stdout.strip().split("\n") if cid.strip()]


def get_container_names(container_ids: list[str]) -> dict[str, str]:
    """Get container names for given IDs for better logging."""
    if not container_ids:
        return {}

    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.ID}}: {{.Name}}", *container_ids],
            capture_output=True,
            text=True,
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
    except subprocess.TimeoutExpired:
        return {}


def manage_docker_services(compose_files: list[Path], action: str) -> bool:
    """Manage Docker services using docker compose."""
    if not DOCKER_ENABLE_STOP_START or dry_run_mode:
        return True

    log.info(f"Performing '{action}' on {len(compose_files)} Docker compose file(s)...")
    all_successful = True

    for file in compose_files:
        if not file.is_file():
            log.warning(f"Compose file not found: {file}. Skipping.")
            continue

        command = ["docker", "compose", "-f", str(file)]
        if action == "stop":
            command.append(DOCKER_SHUTDOWN_METHOD)
        else:
            command.extend(["up", "-d"])

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                log.error(f"Failed to {action} services for {file}: {result.stderr}")
                all_successful = False
        except subprocess.TimeoutExpired:
            log.error(f"Timeout while trying to {action} services for {file}")
            all_successful = False

        delay = DOCKER_STOP_TIMEOUT if action == "stop" else DOCKER_START_DELAY
        time.sleep(delay)

    return all_successful


def force_stop_containers(container_ids: list[str], timeout: int = 30) -> bool:
    """Force stop specific containers using docker stop."""
    if not container_ids:
        return True

    if dry_run_mode:
        log.info(f"DRY RUN: Would force stop {len(container_ids)} container(s).")
        return True

    names = get_container_names(container_ids)
    container_list = ", ".join(
        f"{names.get(cid[:12], cid[:12])}" for cid in container_ids
    )
    log.warning(f"Force stopping {len(container_ids)} container(s): {container_list}")

    try:
        result = subprocess.run(
            ["docker", "stop", "-t", str(timeout), *container_ids],
            capture_output=True,
            text=True,
            timeout=timeout + 30,
        )
        if result.returncode != 0:
            log.error(f"docker stop returned error: {result.stderr}")
    except subprocess.TimeoutExpired:
        log.error("docker stop command timed out")

    return True


def force_kill_containers(container_ids: list[str]) -> bool:
    """Force kill specific containers using docker kill."""
    if not container_ids:
        return True

    if dry_run_mode:
        log.info(f"DRY RUN: Would force kill {len(container_ids)} container(s).")
        return True

    names = get_container_names(container_ids)
    container_list = ", ".join(
        f"{names.get(cid[:12], cid[:12])}" for cid in container_ids
    )
    log.warning(f"Force killing {len(container_ids)} container(s): {container_list}")

    try:
        result = subprocess.run(
            ["docker", "kill", *container_ids],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            log.error(f"docker kill returned error: {result.stderr}")
    except subprocess.TimeoutExpired:
        log.error("docker kill command timed out")

    return True


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
    if not DOCKER_ENABLE_STOP_START:
        log.info("Docker stop/start is disabled. Skipping container shutdown.")
        return True

    if dry_run_mode:
        log.info("DRY RUN: Skipping container shutdown.")
        return True

    if timeout is None:
        timeout = DOCKER_FORCE_STOP_TIMEOUT

    overall_start = time.monotonic()

    for retry in range(DOCKER_SHUTDOWN_MAX_RETRIES):
        check_shutdown_requested()

        if retry > 0:
            log.warning(f"Container shutdown retry {retry + 1}/{DOCKER_SHUTDOWN_MAX_RETRIES}")
            time.sleep(DOCKER_SHUTDOWN_RETRY_DELAY)

        # Check overall timeout
        if time.monotonic() - overall_start > DOCKER_SHUTDOWN_OVERALL_TIMEOUT:
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
        _ = force_stop_containers(remaining, timeout=timeout)

        time.sleep(DOCKER_VERIFY_SHUTDOWN_INTERVAL)

        remaining = get_running_container_ids()
        if not remaining:
            log.info("✓ All containers stopped successfully via docker stop.")
            return True

        log.warning(f"{len(remaining)} container(s) still running after docker stop.")

        # Stage 3: Use docker kill as last resort
        log.info("-" * 40)
        log.info("Stage 3: Last resort via docker kill")
        log.info("-" * 40)
        _ = force_kill_containers(remaining)

        time.sleep(DOCKER_KILL_WAIT_TIME)

        final_remaining = get_running_container_ids()
        if not final_remaining:
            log.info("✓ All containers stopped successfully via docker kill.")
            return True

        names = get_container_names(final_remaining)
        container_list = ", ".join(
            f"{names.get(cid[:12], cid[:12])}" for cid in final_remaining
        )
        log.error(f"Still have {len(final_remaining)} stubborn container(s): {container_list}")

    # All retries exhausted
    final_remaining = get_running_container_ids()
    if final_remaining:
        names = get_container_names(final_remaining)
        container_list = ", ".join(
            f"{names.get(cid[:12], cid[:12])}" for cid in final_remaining
        )
        log.critical(
            f"CRITICAL: {len(final_remaining)} container(s) could not be stopped after all retries!"
        )
        log.critical(f"Stubborn containers: {container_list}")

        send_discord_notification(
            "Container Shutdown Failed",
            (
                f"⚠️ **{len(final_remaining)} container(s) could not be stopped**\n\n"
                + f"Containers: `{container_list}`\n\n"
                + f"**BACKUP WILL PROCEED ANYWAY** - data may be inconsistent for these services.\n\n"
                + f"Manual intervention required after backup completes."
            ),
            16711680,
            title_override="Backup Warning: Containers Still Running",
        )

        # Return False but DON'T stop the backup - just note the warning
        backup_state.add_warning(f"Containers still running: {container_list}")
        return False

    return True


# -----------------------------------------------------------------------------
# Backup Functions
# -----------------------------------------------------------------------------


def rotate_items(dir_path: Path, pattern: str, retention_count: int) -> None:
    """Rotate items in a directory by removing oldest beyond retention count."""
    if dry_run_mode or not dir_path.is_dir():
        return

    log.info(f"Rotating items in {dir_path} matching '{pattern}'...")
    items = sorted(
        [p for p in dir_path.glob(pattern)],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    for item in items[retention_count:]:
        try:
            item.unlink()
            log.info(f"Removed old item: {item.name}")
        except OSError as e:
            log.error(f"Failed to remove {item.name}: {e}")


def rotate_logs(log_dir: Path, pattern: str, retention_count: int) -> None:
    """Rotate log files in the specified directory."""
    rotate_items(log_dir, pattern, retention_count)


def rotate_backups(backup_dir: Path, pattern: str, retention_count: int) -> None:
    """Rotate backup files in the specified directory."""
    rotate_items(backup_dir, pattern, retention_count)


def create_backup(backup_file: Path) -> bool:
    """
    Create the backup archive with improved error handling.

    Handles partial failures gracefully - continues with available directories.
    """
    if dry_run_mode:
        log.info("DRY RUN: Skipping backup creation.")
        return True

    log.info("Starting backup creation process...")

    # Validate backup directories
    valid_backup_dirs: list[str] = []
    failed_dirs: list[str] = []

    for p in BACKUP_DIRS:
        if p.exists():
            if os.access(p, os.R_OK):
                valid_backup_dirs.append(str(p.resolve()))
            else:
                failed_dirs.append(f"{p} (permission denied)")
                log.warning(f"Cannot read backup source, skipping: {p}")
        else:
            failed_dirs.append(f"{p} (not found)")
            log.warning(f"Backup source path does not exist, skipping: {p}")

    if not valid_backup_dirs and not PLEX_DATA_DIR.exists():
        raise BackupCreationError("No valid backup directories found!")

    if failed_dirs:
        backup_state.add_warning(f"Skipped directories: {', '.join(failed_dirs)}")

    temp_tar_file = backup_file.with_suffix(".tmp.tar")

    try:
        # Create initial tar with Plex data (priority)
        if PLEX_DATA_DIR.exists():
            log.info("Adding Plex data to backup (priority)...")
            plex_parent = PLEX_DATA_DIR.parent
            plex_name = PLEX_DATA_DIR.name

            result = subprocess.run(
                ["tar", "-cf", str(temp_tar_file), "-C", str(plex_parent), plex_name],
                capture_output=True,
                text=True,
                timeout=3600,  # 1 hour timeout for Plex data
            )

            if result.returncode != 0:
                log.error(f"Failed to add Plex data: {result.stderr}")
                # Continue anyway - this is important data but not critical
                backup_state.add_warning("Failed to backup Plex data")
        else:
            log.warning(f"Plex data directory not found at {PLEX_DATA_DIR}")

        # Append other directories to the tarball
        all_exclusions: list[Path] = EXCLUDE_DIRS + [PLEX_DATA_DIR]
        exclude_opts: list[str] = [
            f"--exclude={path.resolve()}" for path in all_exclusions if path.exists()
        ]

        if valid_backup_dirs:
            log.info(f"Adding {len(valid_backup_dirs)} directories to backup...")

            # Use --ignore-failed-read to continue on errors
            tar_command: list[str] = (
                [
                    "tar",
                    "-rf" if temp_tar_file.exists() else "-cf",
                    str(temp_tar_file),
                    "-C",
                    "/",
                    "--ignore-failed-read",
                    "--warning=no-file-changed",
                ]
                + exclude_opts
                + valid_backup_dirs
            )

            result = subprocess.run(
                tar_command,
                capture_output=True,
                text=True,
                timeout=7200,  # 2 hour timeout
            )

            # Exit code 1 means files changed during read - acceptable
            if result.returncode > 1:
                log.error(f"Tar command failed: {result.stderr}")
                raise BackupCreationError(f"Tar failed with exit code {result.returncode}")
            elif result.returncode == 1:
                log.warning("Some files changed during backup (non-critical)")

        # Verify temp tar exists and has content
        if not temp_tar_file.exists() or temp_tar_file.stat().st_size == 0:
            raise BackupCreationError("Temporary tar file is empty or missing")

        log.info(f"Tar archive created: {format_bytes(temp_tar_file.stat().st_size)}")

        # Compress and Encrypt the tarball
        log.info("Compressing and encrypting backup...")

        with open(temp_tar_file, "rb") as tar_in, open(backup_file, "wb") as final_out:
            compress_proc = subprocess.Popen(
                [BACKUP_COMPRESSION_TOOL, f"-{BACKUP_COMPRESSION_LEVEL}"],
                stdin=tar_in,
                stdout=subprocess.PIPE,
            )

            encrypt_proc = subprocess.Popen(
                [
                    "openssl",
                    "enc",
                    "-aes-256-cbc",
                    "-md",
                    "sha256",
                    "-pass",
                    f"file:{BACKUP_PASSWORD_FILE.resolve()}",
                    "-pbkdf2",
                ],
                stdin=compress_proc.stdout,
                stdout=final_out,
            )

            if compress_proc.stdout:
                compress_proc.stdout.close()

            # Wait with timeout
            _ = encrypt_proc.wait(timeout=3600)
            _ = compress_proc.wait(timeout=60)

            if compress_proc.returncode != 0:
                raise BackupCreationError(
                    f"Compression failed with exit code {compress_proc.returncode}"
                )

            if encrypt_proc.returncode != 0:
                raise BackupCreationError(
                    f"Encryption failed with exit code {encrypt_proc.returncode}"
                )

        # Verify final backup
        if not backup_file.exists():
            raise BackupCreationError("Final backup file was not created")

        final_size = backup_file.stat().st_size
        if final_size == 0:
            raise BackupCreationError("Final backup file is empty")

        log.info(f"Backup created successfully: {format_bytes(final_size)}")
        return True

    except subprocess.TimeoutExpired as e:
        raise BackupCreationError(f"Backup operation timed out: {e}")

    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise BackupCreationError(f"Backup creation failed: {e}")

    finally:
        # Always clean up temp file
        if temp_tar_file.exists():
            with contextlib.suppress(OSError):
                temp_tar_file.unlink()


def verify_backup(backup_file: Path) -> bool:
    """Verify backup integrity by testing decryption and decompression."""
    if dry_run_mode:
        log.info("DRY RUN: Skipping verification.")
        return True

    if not backup_file.exists():
        raise BackupVerificationError("Backup file does not exist")

    log.info(f"Verifying backup integrity of {backup_file.name}...")

    try:
        with open(backup_file, "rb") as f_in:
            openssl_proc = subprocess.Popen(
                [
                    "openssl",
                    "enc",
                    "-d",
                    "-aes-256-cbc",
                    "-md",
                    "sha256",
                    "-pass",
                    f"file:{BACKUP_PASSWORD_FILE.resolve()}",
                    "-pbkdf2",
                ],
                stdin=f_in,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            compress_proc = subprocess.Popen(
                [BACKUP_COMPRESSION_TOOL, "-d"],
                stdin=openssl_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            tar_proc = subprocess.Popen(
                ["tar", "-tf", "-"],
                stdin=compress_proc.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            if openssl_proc.stdout:
                openssl_proc.stdout.close()
            if compress_proc.stdout:
                compress_proc.stdout.close()

            # Wait with timeouts
            _ = tar_proc.wait(timeout=1800)  # 30 minutes
            _ = compress_proc.wait(timeout=60)
            _ = openssl_proc.wait(timeout=60)

            if openssl_proc.returncode != 0:
                stderr = openssl_proc.stderr.read().decode() if openssl_proc.stderr else ""
                raise BackupVerificationError(f"Decryption failed: {stderr}")

            if compress_proc.returncode != 0:
                stderr = compress_proc.stderr.read().decode() if compress_proc.stderr else ""
                raise BackupVerificationError(f"Decompression failed: {stderr}")

            if tar_proc.returncode != 0:
                stderr = tar_proc.stderr.read().decode() if tar_proc.stderr else ""
                raise BackupVerificationError(f"Tar verification failed: {stderr}")

    except subprocess.TimeoutExpired:
        raise BackupVerificationError("Verification timed out")

    log.info("Backup verification successful.")
    return True


def set_permissions(backup_file: Path) -> None:
    """Set ownership and permissions on backup file."""
    if dry_run_mode:
        return

    log.info("Setting final permissions on backup file...")
    try:
        uid = pwd.getpwnam(BACKUP_USER).pw_uid
        gid = grp.getgrnam(BACKUP_GROUP).gr_gid
        os.chown(backup_file, uid, gid)
        os.chmod(backup_file, 0o600)
    except (KeyError, OSError) as e:
        log.error(f"Failed to set permissions on {backup_file}: {e}")
        backup_state.add_warning(f"Could not set permissions: {e}")


def set_log_permissions(log_file: Path) -> None:
    """Set ownership of log file to the backup user."""
    if dry_run_mode:
        return

    log.info("Setting log file permissions...")
    try:
        uid = pwd.getpwnam(BACKUP_USER).pw_uid
        gid = grp.getgrnam(BACKUP_GROUP).gr_gid
        os.chown(log_file, uid, gid)
        os.chmod(log_file, 0o644)
    except (KeyError, OSError) as e:
        log.error(f"Failed to set permissions on log file {log_file}: {e}")


# -----------------------------------------------------------------------------
# Emergency Recovery Functions
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


def send_final_status_notification(success: bool, log_file: Path) -> None:
    """Send final status notification with summary."""
    privatebin_link = upload_log_to_privatebin(log_file)

    duration = format_duration(backup_state.elapsed_time)

    if success and not backup_state.errors:
        color = 65280  # Green
        status = "Success"
        emoji = "✅"
    elif success and backup_state.warnings:
        color = 16776960  # Yellow
        status = "Completed with Warnings"
        emoji = "⚠️"
    else:
        color = 16711680  # Red
        status = "Failed"
        emoji = "❌"

    message = f"{emoji} **Backup {status}**\n\n"
    message += f"**Duration:** {duration}\n"

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
# Main Execution
# -----------------------------------------------------------------------------


def main() -> None:
    """Main script execution logic with comprehensive error handling."""
    global dry_run_mode, backup_state

    parser = argparse.ArgumentParser(
        description="A robust server backup and sync script."
    )
    _ = parser.add_argument(
        "-d", "--dry-run", action="store_true", help="Perform a dry run."
    )
    args = parser.parse_args()
    dry_run_mode = bool(getattr(args, "dry_run", False))

    # Initialize backup state
    backup_state = BackupState()

    # Set up signal handlers
    setup_signal_handlers()

    # Set up logging
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    LOG_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_ROOT_DIR / f"{timestamp}-backupScript.log"
    setup_logging(log_file)
    set_log_permissions(log_file)

    if dry_run_mode:
        log.info("--- Starting DRY RUN ---")

    success = False

    try:
        with acquire_lock(LOCK_FILE):
            # Stage: Pre-flight checks
            backup_state.stage = BackupStage.PREFLIGHT
            pre_flight_checks()

            # Stage: Create maintenance window
            backup_state.stage = BackupStage.MAINTENANCE_WINDOW
            backup_state.maintenance_window_id = create_backup_maintenance_window()

            # Get compose files
            plex_compose, other_compose = get_docker_compose_files()
            all_compose_files = plex_compose + other_compose

            # Rotation
            rotate_logs(LOG_ROOT_DIR, "*-backupScript.log", RETENTION_COUNT_LOGS)
            rotate_backups(BACKUP_ROOT_DIR, "*.tar.gz.enc", RETENTION_COUNT_BACKUPS)

            # Stage: Container shutdown
            backup_state.stage = BackupStage.CONTAINER_SHUTDOWN
            if DOCKER_ENABLE_STOP_START:
                backup_state.containers_stopped = ensure_all_containers_stopped(
                    all_compose_files
                )
                # Note: We continue even if some containers couldn't be stopped
                if not backup_state.containers_stopped:
                    log.warning("Proceeding with backup despite container shutdown issues")

            check_shutdown_requested()

            # Stage: Backup creation
            backup_state.stage = BackupStage.BACKUP_CREATION
            backup_filename = f"{timestamp.replace('_', '-')}_backup.tar.gz.enc"
            backup_file = BACKUP_ROOT_DIR / backup_filename
            backup_state.backup_file = backup_file
            BACKUP_ROOT_DIR.mkdir(parents=True, exist_ok=True)

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
            if DOCKER_ENABLE_STOP_START and plex_compose:
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
            except BackupVerificationError as e:
                backup_state.add_error(str(e))
                log.critical(f"Backup verification failed: {e}")
                # Continue anyway - we have the backup even if verification failed

            check_shutdown_requested()

            # Stage: Rclone sync
            backup_state.stage = BackupStage.RCLONE_SYNC
            if ENABLE_RCLONE_UPLOAD and backup_state.backup_created:
                rclone_summary = run_rclone_sync_with_retry(log_file)

                if rclone_summary["status"] == "success":
                    backup_state.rclone_completed = True
                    log.info("Rclone sync completed successfully")

                    success_timestamp = datetime.datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    privatebin_link = upload_log_to_privatebin(log_file)

                    message = (
                        f"**Status Details**\n"
                        f"✅ Sync completed successfully\n"
                        f"⏱️ Duration: {rclone_summary['duration']}\n"
                        f"📦 Data: {rclone_summary['transferred_data']}\n"
                        f"📄 Files: {rclone_summary['transferred_files']}\n"
                        f"🔍 Checks: {rclone_summary['checks_count']} / {rclone_summary['total_checks']}\n"
                    )
                    if privatebin_link:
                        message += f"🔗 **[View Full Log]({privatebin_link})**\n\n"
                    else:
                        message += "\n"
                    message += (
                        f"**Sync Info**\n"
                        f"Source: `{RCLONE_SOURCE_DIR}`\n"
                        f"Destination: `{RCLONE_REMOTE_DEST}`\n\n"
                        f"**Timestamp**\n"
                        f"{success_timestamp}"
                    )
                    send_discord_notification(
                        "Success", message, 65280, "Rclone Sync Status: Success"
                    )

                elif rclone_summary["status"] == "failed":
                    backup_state.add_error(
                        f"Rclone sync failed: {rclone_summary.get('last_error', 'Unknown error')}"
                    )
                    privatebin_link = upload_log_to_privatebin(log_file)

                    message = (
                        f"❌ **Sync failed after {rclone_summary.get('attempts', '?')} attempts**\n"
                        f"⏱️ **Duration:** {rclone_summary['duration']}\n"
                        f"**Exit Code:** {rclone_summary['exit_code']}\n"
                        f"⚠️ **Last Error:**\n```\n{rclone_summary['last_error']}\n```"
                    )
                    if privatebin_link:
                        message += f"\n🔗 **[View Full Log]({privatebin_link})**"
                    send_discord_notification(
                        "Failed", message, 16711680, "Rclone Sync Status: Failed"
                    )

            # Determine overall success
            success = backup_state.backup_created and not backup_state.has_critical_errors

    except KeyboardInterrupt:
        log.warning("Script interrupted by user or signal")
        backup_state.add_error("Script interrupted")

    except FileExistsError:
        log.critical(f"Script is already running. Lock file exists: {LOCK_FILE}")
        sys.exit(1)

    except Exception as e:
        log.critical(f"An unexpected critical error occurred: {e}", exc_info=True)
        backup_state.add_error(f"Unexpected error: {e}")
        send_discord_notification(
            "Critical Failure",
            f"The script encountered a fatal error:\n```\n{e}\n```",
            16711680,
        )

    finally:
        log.info("=" * 60)
        log.info("FINALIZATION: Ensuring all services are restored")
        log.info("=" * 60)

        backup_state.stage = BackupStage.CONTAINER_RESTART_ALL

        # Always try to restart services
        if DOCKER_ENABLE_STOP_START:
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
                    log.critical("No containers running after restart! Attempting emergency recovery...")
                    emergency_container_restart()

            except Exception as e:
                log.critical(f"Failed to restart services: {e}")
                emergency_container_restart()

        # Remove maintenance window
        backup_state.stage = BackupStage.CLEANUP
        if backup_state.maintenance_window_id is not None:
            log.info("Removing maintenance window...")
            remove_backup_maintenance_window()

        # Send final status notification
        send_final_status_notification(success, log_file)

        backup_state.stage = BackupStage.COMPLETE
        log.info("=" * 60)
        log.info(f"Backup script finished. Success: {success}")
        log.info(f"Total duration: {format_duration(backup_state.elapsed_time)}")
        log.info("=" * 60)

        # Exit with appropriate code
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
