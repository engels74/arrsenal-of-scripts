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
import datetime
import json
import logging
import os
import pwd
import grp
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
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
# Configuration
# -----------------------------------------------------------------------------

# --- Backup Ownership ---
BACKUP_USER = "user01"
BACKUP_GROUP = "user01"

# --- Local Backup Paths & Retention ---
BACKUP_ROOT_DIR = Path("/data/backups")
LOG_ROOT_DIR = Path("/home/user01/scripts/logs/backupScript")
LOCK_FILE = Path(f"/tmp/{Path(__file__).name}.lock")
RETENTION_COUNT_BACKUPS = 3
RETENTION_COUNT_LOGS = 7

# --- Encryption & Compression ---
# IMPORTANT: The password is now read from a file for security.
# Create a file (e.g., /root/.backup_password), place your password inside,
# and set permissions with: sudo chmod 600 /root/.backup_password
BACKUP_PASSWORD_FILE = Path("/root/.backup_password")
# Use "pigz" for multi-threaded compression (requires 'pigz' to be installed),
# which can significantly speed up backups on multi-core systems.
# Use "gzip" for the standard single-threaded compression.
BACKUP_COMPRESSION_TOOL = "pigz"
BACKUP_COMPRESSION_LEVEL = 3  # 1=fast/less, 9=slow/more for pigz/gzip

# --- Docker Management ---
DOCKER_ENABLE_STOP_START = True
DOCKER_SHUTDOWN_METHOD = "down"  # "stop" or "down"
DOCKER_STACKS_DIR = Path("/home/user01/stacks")
DOCKER_STOP_TIMEOUT = 2
DOCKER_START_DELAY = 30

# --- Priority Backup & Services (Plex) ---
PLEX_DATA_DIR = Path("/opt/docker-all/mediaServers/plex")
PLEX_COMPOSE_FILE = DOCKER_STACKS_DIR / "006-plex-media-server/compose.yaml"

# --- Backup Sources & Exclusions ---
BACKUP_DIRS = [
    Path("/etc/logrotate.d"),
    Path("/root/.config"),
    Path("/root/.ssh"),
    Path("/home/user01/.config"),
    Path("/home/user01/.ssh"),
    Path("/home/user01/scripts"),
    Path("/home/user01/stacks"),
    Path("/data/scripts"),
    Path("/opt/docker-all"),
    Path("/etc/fail2ban"),
    Path("/etc/ssh"),
    Path("/var/lib/docker/volumes"),
]
EXCLUDE_DIRS = [Path("/opt/docker-all/miscSoftware/immich")]

# --- Rclone & Off-site Upload Configuration ---
ENABLE_RCLONE_UPLOAD = True
RCLONE_SOURCE_DIR = BACKUP_ROOT_DIR  # The directory to upload
RCLONE_REMOTE_DEST = "remote:backup"  # Your rclone remote destination
BANDWIDTH_LIMIT = "50M"  # e.g., "10M" for 10 MB/s, or "off"
# Rclone filter rules (https://rclone.org/filtering/)
RCLONE_FILTERS: list[str] = [
    # Example: "+ *.tar.gz.enc", "- **" to only upload backup files
]

# --- Discord & Notification Configuration ---
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."  # Your Discord webhook URL
DISCORD_USERNAME = "Server Backup & Sync"
DISCORD_AVATAR_URL = ""  # Optional: URL to an avatar image

# --- PrivateBin Configuration ---
ENABLE_PRIVATEBIN_UPLOAD = True  # Set to True to upload logs
PRIVATEBIN_CLI_PATH = "privatebin"  # Path to the privatebin-cli executable

# --- Uptime Kuma Maintenance Window Configuration ---
ENABLE_UPTIME_KUMA_MAINTENANCE = True  # Set to False to disable maintenance windows
UPTIME_KUMA_URL = "https://uptimekuma.example.com"
UPTIME_KUMA_USERNAME = "xxx"
UPTIME_KUMA_PASSWORD = "xxx"
UPTIME_KUMA_STATUS_PAGE_SLUG = "xxx"
MAINTENANCE_ID_FILE = Path("/tmp/backup_maintenance_id.txt")

# -----------------------------------------------------------------------------
# Type Definitions for Uptime Kuma
# -----------------------------------------------------------------------------

# Type variable for generic return type
T = TypeVar("T")


class ServerInfo(TypedDict):
    """Server information response from info() API call."""

    serverTimezone: str


class MaintenanceResponse(TypedDict):
    """Response from add_maintenance() API call."""

    maintenanceID: int


class Monitor(TypedDict):
    """Monitor object from get_monitors() API call."""

    id: int
    name: str


class StatusPage(TypedDict):
    """Status page object from get_status_pages() API call."""

    id: int
    slug: str
    title: str


class MonitorId(TypedDict):
    """Monitor ID object for maintenance operations."""

    id: int


class DeleteMaintenanceResponse(TypedDict):
    """Response from delete_maintenance() API call."""

    msg: str


# Type aliases for common return types
MonitorList = list[Monitor]
StatusPageList = list[StatusPage]
MonitorIdList = list[MonitorId]


# JSON data types for rclone log parsing
class RcloneStats(TypedDict):
    """Rclone statistics from JSON log."""

    transfers: int
    bytes: int
    errors: int
    checks: int
    totalBytes: int


class RcloneLogEntry(TypedDict):
    """Rclone log entry structure."""

    level: str
    msg: str
    stats: RcloneStats


# Type alias for JSON data
if TYPE_CHECKING:
    JsonDict = dict[
        str, str | int | float | bool | None | "JsonDict" | list["JsonDict"]
    ]
else:
    JsonDict = dict

# -----------------------------------------------------------------------------
# Global Variables
# -----------------------------------------------------------------------------
log = logging.getLogger(__name__)
dry_run_mode = False

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
                    try:
                        _ = self.api.disconnect()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownVariableType, reportUnknownMemberType]
                    except Exception:
                        pass

                self.api = UptimeKumaApi(self.url, timeout=30)
                _ = self.api.login(self.username, self.password)  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess, reportUnknownVariableType, reportUnknownMemberType]
                log.info("Successfully connected to Uptime Kuma")
                return self.api  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

            except Exception as e:
                retry_count += 1
                if retry_count == self.max_retries:
                    log.error(
                        f"Failed to connect to Uptime Kuma after {self.max_retries} attempts. Last error: {str(e)}"
                    )
                    raise

                log.warning(
                    f"Uptime Kuma connection attempt {retry_count} failed: {str(e)}"
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
                        f"Uptime Kuma operation failed after {self.max_retries} attempts. Last error: {str(e)}"
                    )
                    raise

                log.warning(
                    f"Uptime Kuma operation attempt {retry_count} failed: {str(e)}"
                )
                log.info(f"Retrying in {delay} seconds...")
                time.sleep(delay)
                delay = min(delay * self.backoff_factor, self.max_delay)

                # Try to reconnect before the next attempt
                try:
                    _ = self.connect()
                except Exception:
                    pass  # Will be handled in the next iteration

        raise RuntimeError("Maximum retries exceeded")

    def __enter__(self) -> "UptimeKumaRetry":
        """Context manager entry."""
        _ = self.connect()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Context manager exit."""
        if self.api is not None:
            try:
                _ = self.api.disconnect()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownVariableType, reportUnknownMemberType]
            except Exception:
                pass


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
            # Get server timezone
            server_info = cast(
                ServerInfo,
                cast(object, kuma.retry_operation(kuma.api.info)),  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess, reportUnknownMemberType, reportUnknownArgumentType]
            )
            server_timezone = pytz.timezone(str(server_info["serverTimezone"]))
            log.info(f"Using server timezone: {server_timezone}")

            # Create maintenance window
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

            # Add all monitors to maintenance window
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

            # Add status page to maintenance window
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

            # Save maintenance ID to file
            try:
                MAINTENANCE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(MAINTENANCE_ID_FILE, "w") as f:
                    _ = f.write(str(maintenance_id))
                log.info(f"Maintenance ID saved to {MAINTENANCE_ID_FILE}")
            except Exception as e:
                log.error(f"Failed to save maintenance ID: {e}")
                # Don't fail the backup for this

            return maintenance_id

    except Exception as e:
        log.error(f"Failed to create maintenance window: {e}")
        # Don't fail the backup process for maintenance window issues
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
        # Read maintenance ID from file
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
            # Delete the maintenance window
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

        # Remove the maintenance ID file
        try:
            MAINTENANCE_ID_FILE.unlink()
            log.info("Maintenance ID file removed")
        except FileNotFoundError:
            log.warning("Maintenance ID file already removed")
        except Exception as e:
            log.warning(f"Failed to remove maintenance ID file: {e}")

    except Exception as e:
        log.error(f"Failed to remove maintenance window: {e}")
        # Don't fail for maintenance window cleanup issues


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
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Failed to send Discord notification: {e}")


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
            )
        log_url = result.stdout.strip()
        log.info(f"Log uploaded successfully: {log_url}")
        return log_url
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


def run_rclone_sync(log_file: Path) -> dict[str, str | int]:
    """Runs the rclone sync process and returns a summary."""
    log.info("--- Starting Rclone Off-site Upload ---")
    if not ENABLE_RCLONE_UPLOAD:
        log.info("Rclone upload is disabled. Skipping.")
        return {"status": "skipped"}

    if dry_run_mode:
        log.info("DRY RUN: Skipping rclone execution.")
        return {"status": "skipped"}

    start_time = time.monotonic()

    # Format start message with new structure
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
        16776960,  # Yellow
        title_override="Rclone Sync Status: Started",
    )

    # Flush logs before rclone writes to the same file
    for handler in log.handlers:
        handler.flush()

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
        str(log_file),
        "--log-level",
        "INFO",
        "--use-json-log",  # Use structured JSON logging for reliable parsing
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

    process = subprocess.run(command, capture_output=True, text=True)
    exit_code = process.returncode

    if filter_file:
        _ = filter_file.unlink()

    duration = time.monotonic() - start_time

    # Parse stats from the JSON log file
    final_stats: JsonDict = {}
    error_lines: list[str] = []
    with open(log_file, "r") as f:
        for line in f:
            # Rclone's JSON output is one JSON object per line.
            # The script's own logs are not JSON. We skip them.
            if not line.strip().startswith("{"):
                continue
            try:
                # Parse JSON and ensure it's a dictionary
                parsed_json: object = json.loads(line)  # pyright: ignore[reportAny]
                if not isinstance(parsed_json, dict):
                    continue

                log_entry = cast(JsonDict, parsed_json)

                # The summary stats from rclone are in a log entry
                # containing a 'stats' object. We grab the last one found.
                stats_data = log_entry.get("stats")
                if "stats" in log_entry and isinstance(stats_data, dict):
                    final_stats = stats_data

                elif log_entry.get("level") == "error":
                    msg = log_entry.get("msg")
                    if isinstance(msg, str):
                        error_lines.append(msg)
            except json.JSONDecodeError:
                continue  # Ignore non-JSON lines

    # Safely extract values with proper type checking
    transfers = final_stats.get("transfers", 0)
    bytes_transferred = final_stats.get("bytes", 0)
    errors_count = final_stats.get("errors", len(error_lines))
    checks_count = final_stats.get("checks", 0)
    total_bytes = final_stats.get("totalBytes", 0)

    # Ensure we have integers for calculations
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

    stats = {
        "status": "success" if exit_code == 0 else "failed",
        "exit_code": exit_code,
        "duration": f"{int(duration // 3600)}h:{int((duration % 3600) // 60)}m:{int(duration % 60)}s",
        "transferred": f"{transfers} files, {format_bytes(bytes_transferred)}",
        "transferred_files": f"{transfers} files",
        "transferred_data": format_bytes(bytes_transferred),
        "errors": str(errors_count),
        "checks": f"{checks_count} files, {format_bytes(total_bytes)}",
        "checks_count": checks_count,
        "total_checks": checks_count,  # For now, assume all checks were completed
        "last_error": "\n".join(error_lines[-3:]) if error_lines else "None",
    }

    return stats


def pre_flight_checks() -> None:
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
    log.info("Pre-flight checks passed.")


def get_docker_compose_files() -> tuple[list[Path], list[Path]]:
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


def manage_docker_services(compose_files: list[Path], action: str) -> bool:
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

        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            log.error(f"Failed to {action} services for {file}: {result.stderr}")
            all_successful = False

        delay = DOCKER_STOP_TIMEOUT if action == "stop" else DOCKER_START_DELAY
        time.sleep(delay)
    return all_successful


def rotate_items(dir_path: Path, pattern: str, retention_count: int) -> None:
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
    if dry_run_mode:
        log.info("DRY RUN: Skipping backup creation.")
        return True
    log.info("Starting backup creation process...")

    # Check for existence of source directories before backup
    valid_backup_dirs: list[str] = []
    for p in BACKUP_DIRS:
        if p.exists():
            valid_backup_dirs.append(str(p.resolve()))
        else:
            log.warning(f"Backup source path does not exist, skipping: {p}")

    if not PLEX_DATA_DIR.exists():
        log.warning(
            f"Plex data directory not found at {PLEX_DATA_DIR}, cannot create priority backup."
        )

    temp_tar_file = backup_file.with_suffix(".tmp.tar")
    try:
        # Create initial tar with Plex data
        if PLEX_DATA_DIR.exists():
            plex_parent = PLEX_DATA_DIR.parent
            plex_name = PLEX_DATA_DIR.name
            _ = subprocess.run(
                ["tar", "-cf", str(temp_tar_file), "-C", str(plex_parent), plex_name],
                check=True,
                capture_output=True,
                text=True,
            )

        # Append other directories to the tarball
        all_exclusions: list[Path] = EXCLUDE_DIRS + [PLEX_DATA_DIR]
        exclude_opts: list[str] = [
            f"--exclude={path.resolve()}" for path in all_exclusions if path.exists()
        ]

        if valid_backup_dirs:
            tar_command: list[str] = (
                ["tar", "-rf", str(temp_tar_file), "-C", "/"]
                + exclude_opts
                + valid_backup_dirs
            )
            _ = subprocess.run(tar_command, check=True, capture_output=True, text=True)

        # Compress and Encrypt the tarball
        with open(temp_tar_file, "rb") as tar_in, open(backup_file, "wb") as final_out:
            procs: list[subprocess.Popen[bytes]] = []

            # Add compression to the pipeline
            compress_proc = subprocess.Popen(
                [BACKUP_COMPRESSION_TOOL, f"-{BACKUP_COMPRESSION_LEVEL}"],
                stdin=tar_in,
                stdout=subprocess.PIPE,
            )
            procs.append(compress_proc)
            last_proc_stdout = compress_proc.stdout

            # Add encryption as the final stage of the pipeline
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
                stdin=last_proc_stdout,
                stdout=final_out,
            )
            if compress_proc.stdout:
                compress_proc.stdout.close()
            procs.append(encrypt_proc)

            # Wait for all processes in the pipeline to complete
            for proc in procs:
                _ = proc.wait()

            # Check for errors in any part of the pipeline
            for proc in procs:
                if proc.returncode != 0:
                    # Handle args properly - convert to list of strings
                    if proc.args:
                        # proc.args can be a string or sequence, handle both cases
                        if isinstance(proc.args, str):
                            cmd_str = proc.args
                        else:
                            # Convert each argument to string safely, handling PathLike objects
                            try:
                                # Check if proc.args is a list or tuple (sequence types we can iterate)
                                if isinstance(proc.args, (list, tuple)):
                                    cmd_str = " ".join(str(arg) for arg in proc.args)
                                else:
                                    cmd_str = str(proc.args)
                            except (TypeError, AttributeError):
                                # Fallback if args is not iterable
                                cmd_str = str(proc.args)
                    else:
                        cmd_str = "unknown"
                    raise subprocess.CalledProcessError(proc.returncode, cmd_str)

    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        log.critical(f"Backup creation failed: {e}")
        return False
    finally:
        if temp_tar_file.exists():
            temp_tar_file.unlink()
    log.info("Backup created successfully.")
    return True


def verify_backup(backup_file: Path) -> bool:
    if dry_run_mode:
        log.info("DRY RUN: Skipping verification.")
        return True
    if not backup_file.exists():
        log.error("Verification failed: Backup file does not exist.")
        return False
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
            )
            compress_proc = subprocess.Popen(
                [BACKUP_COMPRESSION_TOOL, "-d"],
                stdin=openssl_proc.stdout,
                stdout=subprocess.PIPE,
            )
            tar_proc = subprocess.Popen(
                ["tar", "-tf", "-"],
                stdin=compress_proc.stdout,
                stdout=subprocess.DEVNULL,
            )
            if openssl_proc.stdout:
                openssl_proc.stdout.close()
            if compress_proc.stdout:
                compress_proc.stdout.close()
            _ = tar_proc.wait()
            _ = compress_proc.wait()
            _ = openssl_proc.wait()
            if (
                tar_proc.returncode != 0
                or compress_proc.returncode != 0
                or openssl_proc.returncode != 0
            ):
                raise subprocess.CalledProcessError(
                    tar_proc.returncode, "verification pipeline"
                )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        log.critical(f"Backup verification failed: {e}")
        return False
    log.info("Backup verification successful.")
    return True


def set_permissions(backup_file: Path) -> None:
    if dry_run_mode:
        return
    log.info("Setting final permissions on backup file...")
    try:
        uid = pwd.getpwnam(BACKUP_USER).pw_uid
        gid = grp.getgrnam(BACKUP_GROUP).gr_gid
        os.chown(backup_file, uid, gid)
        os.chmod(backup_file, 0o600)
    except (KeyError, OSError) as e:
        log.critical(f"Failed to set permissions on {backup_file}: {e}")


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
# Main Execution
# -----------------------------------------------------------------------------


def main() -> None:
    """Main script execution logic."""
    global dry_run_mode
    parser = argparse.ArgumentParser(
        description="A robust server backup and sync script."
    )
    _ = parser.add_argument(
        "-d", "--dry-run", action="store_true", help="Perform a dry run."
    )
    args = parser.parse_args()
    dry_run_mode = bool(getattr(args, "dry_run", False))

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    LOG_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_ROOT_DIR / f"{timestamp}-backupScript.log"
    setup_logging(log_file)

    # Set proper ownership for the log file
    set_log_permissions(log_file)

    if dry_run_mode:
        log.info("--- Starting DRY RUN ---")
    try:
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        log.critical(f"Script is already running. Lock file exists: {LOCK_FILE}")
        sys.exit(1)

    services_stopped_successfully = False
    plex_service_started = False
    all_other_services_to_start: list[Path] = []
    maintenance_window_created = False

    try:
        pre_flight_checks()

        # Create maintenance window before starting backup operations
        maintenance_id = create_backup_maintenance_window()
        if maintenance_id is not None:
            maintenance_window_created = True
            log.info("Maintenance window created successfully")
        else:
            log.info("Maintenance window creation skipped or failed")

        plex_compose, other_compose = get_docker_compose_files()
        all_other_services_to_start = other_compose

        rotate_logs(LOG_ROOT_DIR, "*-backupScript.log", RETENTION_COUNT_LOGS)
        rotate_backups(BACKUP_ROOT_DIR, "*.tar.gz.enc", RETENTION_COUNT_BACKUPS)

        if DOCKER_ENABLE_STOP_START:
            services_stopped_successfully = manage_docker_services(
                plex_compose + other_compose, "stop"
            )
            if not services_stopped_successfully:
                log.critical("Failed to stop all services. Aborting backup.")
                return

        backup_filename = f"{timestamp.replace('_', '-')}_backup.tar.gz.enc"
        backup_file = BACKUP_ROOT_DIR / backup_filename
        BACKUP_ROOT_DIR.mkdir(parents=True, exist_ok=True)

        if not create_backup(backup_file):
            log.critical("Backup creation failed.")
            return

        if services_stopped_successfully:
            log.info("Restarting Plex post-priority backup...")
            if manage_docker_services(plex_compose, "start"):
                plex_service_started = True

        if not verify_backup(backup_file):
            log.critical("BACKUP VERIFICATION FAILED. The backup may be corrupt.")
        else:
            set_permissions(backup_file)
            log.info(f"Local backup successful. File created: {backup_file}")
            # --- Rclone Upload Step ---
            rclone_summary = run_rclone_sync(log_file)
            if rclone_summary["status"] != "skipped":
                privatebin_link = upload_log_to_privatebin(log_file)
                if rclone_summary["status"] == "success":
                    # Format success message with new structure
                    success_timestamp = datetime.datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
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
                else:
                    message = (
                        f"❌ **Sync failed (Exit: {rclone_summary['exit_code']})**\n"
                        f"⏱️ **Duration:** {rclone_summary['duration']}\n"
                        f"⚠️ **Last Error:**\n```\n{rclone_summary['last_error']}\n```"
                    )
                    if privatebin_link:
                        message += f"\n🔗 **[View Full Log]({privatebin_link})**"
                    send_discord_notification(
                        "Failed", message, 16711680, "Rclone Sync Status: Failed"
                    )

    except Exception as e:
        log.critical(f"An unexpected critical error occurred: {e}", exc_info=True)
        send_discord_notification(
            "Critical Failure",
            f"The script encountered a fatal error:\n```\n{e}\n```",
            16711680,
        )

    finally:
        log.info("--- Entering Finalization Block ---")
        if services_stopped_successfully:
            if not plex_service_started:
                log.warning("Plex was not started. Attempting to start now.")
                _ = manage_docker_services(get_docker_compose_files()[0], "start")
            log.info("Attempting to start all other services...")
            _ = manage_docker_services(all_other_services_to_start, "start")

        # Remove maintenance window after all services are restored
        if maintenance_window_created:
            log.info("Removing maintenance window...")
            remove_backup_maintenance_window()

        log.info("--- Backup script finished ---")
        os.close(lock_fd)
        os.remove(LOCK_FILE)


if __name__ == "__main__":
    main()
