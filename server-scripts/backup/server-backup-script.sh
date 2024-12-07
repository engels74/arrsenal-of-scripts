#!/usr/bin/env bash
set -euo pipefail

###########################################################################
# server-backup-script.sh
#
# Description:
#   This script automates the backup of configuration files and directories,
#   optionally stopping Docker services before backup and starting them again
#   afterward. It creates compressed, encrypted archives with 7z, manages
#   retention of old backups and logs, and supports verification after backup.
#
# Installation:
#   1) Place this script in a secure location, e.g. /usr/local/bin/server-backup-script.sh
#   2) Make the script executable: chmod +x /usr/local/bin/server-backup-script.sh
#   3) Adjust the configuration variables below to match your environment.
#      - Set BACKUP_USER and BACKUP_GROUP to your desired backup account.
#      - Update BACKUP_DIRS to the directories you want to back up.
#      - Update DOCKER_COMPOSE_FILES_STOP and DOCKER_COMPOSE_FILES_START with your stack files.
#   4) Ensure all dependencies are installed (7z, Docker if using Docker functionality).
#
# Usage:
#   ./server-backup-script.sh [options]
#
# Options:
#   -h, --help       Show help message
#   -d, --dry-run     Perform a dry run (no backup or file removals, just logging)
#   -p, --password    Prompt for backup password if not set as an environment variable
#
# Cron Example:
#   Run every day at 2 AM as root:
#   sudo crontab -e
#   0 2 * * * /usr/local/bin/server-backup-script.sh
#
###########################################################################

### Configuration Section ##################################################

# Set the user and group that will own backup files.
# Replace 'backupuser' and 'backupgroup' with the appropriate user and group.
BACKUP_USER="backupuser"
BACKUP_GROUP="backupgroup"

# Directories to back up.
# Adjust these paths to match what you want to back up.
BACKUP_DIRS=(
    "/path/to/system/config/logrotate"
    "/path/to/user/config"
    "/path/to/ssh/config"
    "/path/to/scripts"
    "/path/to/docker/stacks"
    "/path/to/data/scripts"
)

# Docker compose files to stop before backup.
# These should be the paths to your Docker Compose stack files if you run services via Docker.
# If you don't use Docker, leave empty or remove.
DOCKER_COMPOSE_FILES_STOP=(
    "/path/to/stacks/proxy/compose.yaml"
    "/path/to/stacks/tools/compose.yaml"
    "/path/to/stacks/indexer/compose.yaml"
    "/path/to/stacks/media/compose.yaml"
    "/path/to/stacks/utilities/compose.yaml"
    "/path/to/stacks/mediaserver/compose.yaml"
)

# Docker compose files to start after backup, in the desired order.
DOCKER_COMPOSE_FILES_START=(
    "/path/to/stacks/mediaserver/compose.yaml"
    "/path/to/stacks/proxy/compose.yaml"
    "/path/to/stacks/tools/compose.yaml"
    "/path/to/stacks/indexer/compose.yaml"
    "/path/to/stacks/media/compose.yaml"
    "/path/to/stacks/utilities/compose.yaml"
)

# Root directories for backups and logs
BACKUP_ROOT_DIR="/data/backups"
LOG_ROOT_DIR="/var/log/server-backup-script"

# Number of old backups and logs to keep
RETENTION_COUNT_BACKUPS=5
RETENTION_COUNT_LOGS=7

# 7z encryption password
# If empty, you can supply at runtime with --password option.
BACKUP_PASSWORD=""

# Compression level for 7z (0=none, 9=max)
BACKUP_COMPRESSION_LEVEL=3

# Docker stop/start delays
DOCKER_STOP_TIMEOUT=2
DOCKER_START_DELAY=30

### End Configuration Section ###############################################


### Functions Section #######################################################

print_usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Options:
  -h, --help       Show this help message
  -d, --dry-run     Perform a dry run (no backup, no removals, just logging)
  -p, --password    Prompt for 7z encryption password if not already set in environment
EOF
}

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

error_exit() {
    log "ERROR: $*"
    exit 1
}

check_dependencies() {
    local deps=("7z" "sort" "awk" "xargs" "find" "mkdir" "tee" "rm" "date")
    for dep in "${deps[@]}"; do
        command -v "$dep" &>/dev/null || error_exit "Missing dependency: $dep"
    done

    # Check Docker only if we have Docker compose files defined
    if [ ${#DOCKER_COMPOSE_FILES_STOP[@]} -gt 0 ] || [ ${#DOCKER_COMPOSE_FILES_START[@]} -gt 0 ]; then
        command -v docker &>/dev/null || error_exit "Docker not found. Either install Docker or remove Docker-related config."
    fi
}

check_root() {
    if [ "$EUID" -ne 0 ]; then
        error_exit "Please run as root."
    fi
}

already_running_check() {
    if pidof -x "$(basename "$0")" -o %PPID &>/dev/null; then
        error_exit "Script is already running. Exiting."
    fi
}

rotate_logs() {
    find "$LOG_ROOT_DIR" -type f -name "*-backupScript.log" -printf '%T@ %p\n' \
        | sort -nr \
        | awk "NR>$RETENTION_COUNT_LOGS {print \$2}" \
        | xargs -r rm --
}

rotate_backups() {
    find "$BACKUP_ROOT_DIR" -type f -name "backup-*.7z" -printf '%T@ %p\n' \
        | sort -nr \
        | awk "NR>$RETENTION_COUNT_BACKUPS {print \$2}" \
        | xargs -r rm --
}

stop_docker_services() {
    if ! command -v docker &>/dev/null; then
        log "Docker not found, skipping Docker stop step."
        return
    fi

    log "Stopping Docker services..."
    for file in "${DOCKER_COMPOSE_FILES_STOP[@]}"; do
        if [ -f "$file" ]; then
            log "Stopping services from $file"
            docker compose -f "$file" stop || log "Warning: Failed to stop services from $file"
            sleep $DOCKER_STOP_TIMEOUT
        else
            log "Warning: $file not found, skipping."
        fi
    done

    log "Ensuring all containers are stopped"
    if [ -n "$(docker ps -q)" ]; then
        docker stop $(docker ps -q)
    else
        log "No running containers found"
    fi
}

start_docker_services() {
    if ! command -v docker &>/dev/null; then
        log "Docker not found, skipping Docker start step."
        return
    fi

    log "Starting Docker services..."
    for file in "${DOCKER_COMPOSE_FILES_START[@]}"; do
        if [ -f "$file" ]; then
            log "Starting services from $file"
            docker compose -f "$file" up -d
            log "Waiting $DOCKER_START_DELAY seconds before starting next stack..."
            sleep $DOCKER_START_DELAY
        else
            log "Warning: $file not found, skipping."
        fi
    done
}

create_backup() {
    local max_retries=3
    local retry_count=0
    local success=false

    if [ -z "$BACKUP_PASSWORD" ]; then
        error_exit "No backup password set. Provide one with BACKUP_PASSWORD env or --password option."
    fi

    cd /

    while [ $retry_count -lt $max_retries ] && [ "$success" = false ]; do
        log "Backup attempt $((retry_count + 1)) of $max_retries"
        log "Creating backup archive..."

        # Adjust or remove the exclude pattern (-xr!) if not needed.
        # Example exclude: -xr!/opt/docker-all/immich/library
        if 7z a \
           -mx="$BACKUP_COMPRESSION_LEVEL" \
           -mmt=on \
           -p"$BACKUP_PASSWORD" \
           -mhe=on \
           -spf2 \
           "$BACKUP_FILE" \
           "${BACKUP_DIRS[@]}"; then
            log "Backup completed successfully"
            success=true
        else
            local ret=$?
            log "7z returned error code: $ret"
            if [ $ret -eq 1 ]; then
                log "Backup completed with warnings. Check logs for details."
                success=true
            else
                retry_count=$((retry_count + 1))
                if [ $retry_count -lt $max_retries ]; then
                    log "Critical failure, retrying in 30 seconds..."
                    sleep 30
                else
                    log "Backup failed after $max_retries attempts"
                    return 1
                fi
            fi
        fi
    done
    return 0
}

verify_backup() {
    log "Verifying backup file integrity..."
    if ! 7z t "$BACKUP_FILE" -p"$BACKUP_PASSWORD"; then
        log "Backup verification failed"
        return 1
    fi
    return 0
}

set_permissions() {
    chown "${BACKUP_USER}:${BACKUP_GROUP}" "$BACKUP_FILE"
    chmod 600 "$BACKUP_FILE"
    chown "${BACKUP_USER}:${BACKUP_GROUP}" "$BACKUP_ROOT_DIR"
}

### End Functions Section ###################################################


### Main Section ############################################################

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
            shift
            ;;
        -p|--password)
            PROMPT_PASSWORD=true
            shift
            ;;
        *)
            print_usage
            exit 1
            ;;
    esac
done

check_root
check_dependencies
already_running_check

if [ "$PROMPT_PASSWORD" = true ] && [ -z "$BACKUP_PASSWORD" ]; then
    read -s -p "Enter backup password: " BACKUP_PASSWORD
    echo
fi

mkdir -p "$LOG_ROOT_DIR"
LOG_FILE="$LOG_ROOT_DIR/$(date '+%Y-%m-%d_%H-%M-%S')-backupScript.log"
exec > >(tee -a "$LOG_FILE") 2>&1

if [ "$DRY_RUN" = true ]; then
    log "Dry run enabled. No backups or removals will be performed."
fi

rotate_logs

log "Starting backup process at $(date '+%Y-%m-%d %H:%M:%S')"

mkdir -p "$BACKUP_ROOT_DIR"
BACKUP_FILE="$BACKUP_ROOT_DIR/backup-$(date '+%Y-%m-%d').7z"

if [ "$DRY_RUN" = false ]; then
    log "Cleaning up old backup files..."
    rotate_backups
else
    log "[DRY-RUN] Would rotate old backups here."
fi

log "Stopping services..."
if [ "$DRY_RUN" = false ]; then
    stop_docker_services
else
    log "[DRY-RUN] Would stop docker services here."
fi

log "Creating backup..."
if [ "$DRY_RUN" = false ]; then
    if ! create_backup; then
        log "Backup process failed"
        log "Starting services again..."
        start_docker_services
        exit 1
    fi
else
    log "[DRY-RUN] Would create backup here."
fi

if [ "$DRY_RUN" = false ]; then
    if ! verify_backup; then
        log "Backup verification failed"
        log "Starting services again..."
        start_docker_services
        exit 1
    fi

    log "Setting appropriate permissions..."
    set_permissions
else
    log "[DRY-RUN] Would verify and set permissions here."
fi

log "Starting services..."
if [ "$DRY_RUN" = false ]; then
    start_docker_services
else
    log "[DRY-RUN] Would start docker services here."
fi

log "Backup process completed at $(date '+%Y-%m-%d %H:%M:%S')"
log "Backup file: $BACKUP_FILE"

exit 0

### End Main Section ########################################################

# Server Backup Script
# <https://github.com/engels74/arrsenal-of-scripts>
# This script backups up files and directories using 7-zip
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
