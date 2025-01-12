#!/usr/bin/env bash
#
# -----------------------------------------------------------------------------
# server-backup-script.sh
#
# A minimalist, automated backup script that uses tar, gzip, and OpenSSL 
# for encryption and compression. Optional Docker Compose stop/start management,
# with logs and backup retention.
#
# Usage:
#   ./server-backup-script.sh [options]
#     -h, --help        Show help message
#     -d, --dry-run     Perform a dry run (logging only; no backup/removals)
#     -p, --password    Prompt for encryption password at runtime
#
# Cron Example:
#   0 2 * * * /usr/local/bin/server-backup-script.sh
#
# To unpack a backup:
#   openssl enc -aes-256-cbc -pbkdf2 -md sha256 -d \
#     -in backup_YYYY-MM-DD-HH-mm-ss.tar.gz.enc \
#     -pass pass:"YOUR_BACKUP_PASSWORD" \
#   | tar -xzv -C /path/to/extraction/directory
#
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# --- Backup Ownership --------------------------------------------------------
BACKUP_USER="backupuser"
BACKUP_GROUP="backupgroup"

# --- Paths -------------------------------------------------------------------
BACKUP_ROOT_DIR="/data/backups"
LOG_ROOT_DIR="/var/log/server-backup-script"

# --- Retention --------------------------------------------------------------
RETENTION_COUNT_BACKUPS=5
RETENTION_COUNT_LOGS=7

# --- Encryption & Compression -----------------------------------------------
BACKUP_PASSWORD=""          # Provide via --password if empty
BACKUP_COMPRESSION_LEVEL=3  # 1=fast/less, 9=slow/more for gzip
# NOTE: We will use `gzip -${BACKUP_COMPRESSION_LEVEL}` for compression,
#       and OpenSSL for encryption (AES-256-CBC).

# --- Docker Management Delays -----------------------------------------------
DOCKER_STOP_TIMEOUT=2
DOCKER_START_DELAY=30

# --- Docker Management Settings ---------------------------------------------
DOCKER_ENABLE_STOP_BEFORE_BACKUP=true
DOCKER_ENABLE_START_AFTER_BACKUP=false
DOCKER_SHUTDOWN_METHOD="stop"  # "stop" or "down"

# --- Log Filename -----------------------------------------------------------
LOG_FILENAME="backupScript"

# --- Backup Sources & Docker Compose Files -----------------------------------
BACKUP_DIRS=(
    "/etc/logrotate.d"
    "/home/aplex/.config"
    "/home/aplex/.ssh"
    "/home/aplex/scripts"
    "/home/aplex/stacks"
    "/data/scripts"
    "/opt/docker-all"
    "/etc/fail2ban"
    "/etc/ssh"
    "/var/lib/docker/volumes"
)

DOCKER_COMPOSE_FILES_STOP=(
    "/home/aplex/stacks/000-caddy/compose.yaml"
    "/home/aplex/stacks/001-download-tools/compose.yaml"
    "/home/aplex/stacks/002-prowlarr/compose.yaml"
    "/home/aplex/stacks/003-arr-software/compose.yaml"
    "/home/aplex/stacks/004-misc-software/compose.yaml"
    "/home/aplex/stacks/005-plex-media-server/compose.yaml"
    "/home/aplex/stacks/006-hoarder-app/compose.yaml"
)

DOCKER_COMPOSE_FILES_START=(
    "/home/aplex/stacks/000-caddy/compose.yaml"
    "/home/aplex/stacks/001-download-tools/compose.yaml"
    "/home/aplex/stacks/002-prowlarr/compose.yaml"
    "/home/aplex/stacks/003-arr-software/compose.yaml"
    "/home/aplex/stacks/004-misc-software/compose.yaml"
    "/home/aplex/stacks/005-plex-media-server/compose.yaml"
    "/home/aplex/stacks/006-hoarder-app/compose.yaml"
)


# -----------------------------------------------------------------------------
# Functions
# -----------------------------------------------------------------------------

print_usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Options:
  -h, --help       Show help message
  -d, --dry-run    Perform a dry run (logging only; no backups or removals)
  -p, --password   Prompt for encryption password at runtime
EOF
}

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

error_exit() {
    log "ERROR: $*"
    exit 1
}

verify_user_group() {
    id -u "$BACKUP_USER" >/dev/null 2>&1 || error_exit "User '$BACKUP_USER' not found"
    getent group "$BACKUP_GROUP" >/dev/null 2>&1 || error_exit "Group '$BACKUP_GROUP' not found"
}

check_dependencies() {
    # Updated dependencies to remove '7z' and add 'tar', 'openssl', 'gzip'
    local deps=("tar" "gzip" "openssl" "sort" "awk" "xargs" "find" "mkdir" "tee" "rm" "date")
    for dep in "${deps[@]}"; do
        command -v "$dep" &>/dev/null || error_exit "Missing dependency: $dep"
    done

    if [ "$DOCKER_ENABLE_STOP_BEFORE_BACKUP" = true ] || [ "$DOCKER_ENABLE_START_AFTER_BACKUP" = true ]; then
        command -v docker &>/dev/null || error_exit "Docker not found (disable Docker steps or install Docker)."
    fi
}

check_root() {
    [ "$EUID" -eq 0 ] || error_exit "Please run as root."
}

already_running_check() {
    if pidof -x "$(basename "$0")" -o %PPID &>/dev/null; then
        error_exit "Script is already running."
    fi
}

rotate_logs() {
    find "$LOG_ROOT_DIR" -type f -name "*-backupScript.log" -printf '%T@ %p\n' \
        | sort -nr \
        | awk "NR>$RETENTION_COUNT_LOGS {print \$2}" \
        | xargs -r rm --
}

rotate_backups() {
    # Updated for '.tar.gz.enc' files
    find "$BACKUP_ROOT_DIR" -type f -name "backup-*.tar.gz.enc" -printf '%T@ %p\n' \
        | sort -nr \
        | awk "NR>$RETENTION_COUNT_BACKUPS {print \$2}" \
        | xargs -r rm --
}

stop_docker_services() {
    log "Stopping Docker services..."
    for file in "${DOCKER_COMPOSE_FILES_STOP[@]}"; do
        if [ -f "$file" ]; then
            log "Shutting down using '$DOCKER_SHUTDOWN_METHOD': $file"
            if   [ "$DOCKER_SHUTDOWN_METHOD" = "stop" ]; then
                docker compose -f "$file" stop || log "Warning: stop failed on $file"
            elif [ "$DOCKER_SHUTDOWN_METHOD" = "down" ]; then
                docker compose -f "$file" down || log "Warning: down failed on $file"
            else
                log "Unknown DOCKER_SHUTDOWN_METHOD: $DOCKER_SHUTDOWN_METHOD"
            fi
            sleep "$DOCKER_STOP_TIMEOUT"
        else
            log "Warning: $file not found; skipping."
        fi
    done

    if [ "$DOCKER_SHUTDOWN_METHOD" = "stop" ]; then
        log "Ensuring all containers are stopped..."
        [ -n "$(docker ps -q)" ] && docker stop $(docker ps -q) || log "No running containers."
    fi
}

start_docker_services() {
    log "Starting Docker services..."
    for file in "${DOCKER_COMPOSE_FILES_START[@]}"; do
        if [ -f "$file" ]; then
            log "Starting services from $file"
            docker compose -f "$file" up -d
            sleep "$DOCKER_START_DELAY"
        else
            log "Warning: $file not found; skipping."
        fi
    done
}

create_backup() {
    [ -z "$BACKUP_PASSWORD" ] && error_exit "No backup password set."
    cd / || error_exit "Failed to change to root directory"

    # Debug line: show which directories will be backed up
    log "DEBUG: BACKUP_DIRS expanded to: ${BACKUP_DIRS[@]}"

    local max_retries=3
    local retry_count=0
    local success=false

    while [ $retry_count -lt $max_retries ] && [ "$success" = false ]; do
        log "Backup attempt $((retry_count + 1))/$max_retries"
        
        # Create a tar archive, pipe through gzip, then encrypt with OpenSSL
        # The final output is a single encrypted file with ".tar.gz.enc"
        if tar -cf - "${BACKUP_DIRS[@]}" \
            | gzip -"$BACKUP_COMPRESSION_LEVEL" \
            | openssl enc -aes-256-cbc -md sha256 -pass pass:"$BACKUP_PASSWORD" -pbkdf2 \
                -out "$BACKUP_FILE"
        then
            log "Backup completed successfully."
            success=true
        else
            local ret=$?
            log "tar/gzip/openssl returned code: $ret"
            if [ $ret -eq 0 ]; then
                # Sometimes tar can exit 0 even if warnings occur
                log "Backup completed with warnings."
                success=true
            else
                retry_count=$((retry_count + 1))
                [ $retry_count -lt $max_retries ] && log "Retrying in 30s..." && sleep 30
            fi
        fi
    done

    [ "$success" = true ] || return 1
    return 0
}

verify_backup() {
    log "Verifying backup integrity..."

    # Decrypt the file, decompress, and test the tar archive
    if openssl enc -aes-256-cbc -md sha256 -d -pass pass:"$BACKUP_PASSWORD" -pbkdf2 -in "$BACKUP_FILE" \
        | gzip -d \
        | tar -tf - >/dev/null
    then
        log " Backup verification successful - archive integrity confirmed"
        return 0
    else
        log " Backup verification failed - archive may be corrupted"
        return 1
    fi
}

set_permissions() {
    chown "${BACKUP_USER}:${BACKUP_GROUP}" "$BACKUP_FILE" || error_exit "Chown failed on backup file"
    chmod 600 "$BACKUP_FILE" || error_exit "Chmod failed on backup file"
    chown "${BACKUP_USER}:${BACKUP_GROUP}" "$BACKUP_ROOT_DIR" || error_exit "Chown failed on backup directory"
}


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

DRY_RUN=false
PROMPT_PASSWORD=false

while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            print_usage
            exit 0
            ;;
        -d|--dry-run)
            DRY_RUN=true
            ;;
        -p|--password)
            PROMPT_PASSWORD=true
            ;;
        *)
            print_usage
            exit 1
            ;;
    esac
    shift
done

check_root
check_dependencies
verify_user_group
already_running_check

if [ "$PROMPT_PASSWORD" = true ] && [ -z "$BACKUP_PASSWORD" ]; then
    read -s -p "Enter backup password: " BACKUP_PASSWORD
    echo
fi

mkdir -p "$LOG_ROOT_DIR"

# Build the full log file name using LOG_FILENAME plus date/time prefix.
LOG_FILE="$LOG_ROOT_DIR/$(date '+%Y-%m-%d_%H-%M-%S')-${LOG_FILENAME}.log"

exec > >(tee -a "$LOG_FILE") 2>&1

$DRY_RUN && log "DRY RUN: No backup or removal actions will be performed."

rotate_logs

log "Starting backup @ $(date '+%Y-%m-%d %H:%M:%S')"
mkdir -p "$BACKUP_ROOT_DIR" || error_exit "Failed to create $BACKUP_ROOT_DIR"

# Extension updated from .7z to .tar.gz.enc
BACKUP_FILE="$BACKUP_ROOT_DIR/$(date '+%Y-%m-%d-%H-%M-%S')_backup.tar.gz.enc"

if ! $DRY_RUN; then
    log "Cleaning old backups..."
    rotate_backups
fi

log "Stopping services (if enabled)..."
if ! $DRY_RUN && [ "$DOCKER_ENABLE_STOP_BEFORE_BACKUP" = true ]; then
    stop_docker_services
fi

log "Creating backup..."
if ! $DRY_RUN; then
    if ! create_backup; then
        log "Backup failed."
        if [ "$DOCKER_ENABLE_START_AFTER_BACKUP" = true ]; then
            log "Attempting to start services..."
            start_docker_services
        fi
        exit 1
    fi
fi

if ! $DRY_RUN; then
    if ! verify_backup; then
        log "Backup verification failed."
        [ "$DOCKER_ENABLE_START_AFTER_BACKUP" = true ] && start_docker_services
        exit 1
    fi

    log "Setting permissions..."
    set_permissions
fi

log "Starting services (if enabled)..."
if ! $DRY_RUN && [ "$DOCKER_ENABLE_START_AFTER_BACKUP" = true ]; then
    start_docker_services
fi

log "Backup completed @ $(date '+%Y-%m-%d %H:%M:%S')."
log "Backup file: $BACKUP_FILE"
exit 0

# -----------------------------------------------------------------------------
# License
# -----------------------------------------------------------------------------

# Server Backup Script
# <https://github.com/engels74/arrsenal-of-scripts>
# This script backups up files and directories using tar, gzip, and OpenSSL
# Copyright (C) 2024 - engels74
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# Contact: engels74@tuta.io
