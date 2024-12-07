#!/bin/bash

###############################################################################
# Script Name: rclone_sync_script.sh
#
# Description:
#   This script uses rclone to synchronize data from a local source directory
#   to a remote storage of your choice. It can send notifications to a
#   Discord webhook and optionally upload logs to a PrivateBin instance.
#
# Features:
#   - Sync data using rclone with configurable bandwidth limits and retry logic
#   - Generate and rotate logs
#   - Send formatted Discord notifications with status updates
#   - Optionally upload logs to a PrivateBin instance
#
# Requirements:
#   - rclone installed and configured (including crypt remote)
#   - curl for Discord webhook notifications and for checking GitHub releases
#   - (Optional) privatebin CLI tool for uploading logs
#
# Usage:
#   1. Download the script:
#      wget https://raw.githubusercontent.com/engels74/arrsenal-of-scripts/refs/heads/main/server-scripts/rclone/rclone-sync-script.sh -O rclone_sync_script.sh
#
#   2. Make it executable:
#      chmod +x rclone_sync_script.sh
#
#   3. Edit the configuration variables in the "Configuration Section" below.
#
#   4. Run the script:
#      ./rclone_sync_script.sh
#
# Cron Setup Example:
#   To run once a day at 2 AM:
#   0 2 * * * /path/to/rclone_sync_script.sh
# #############################################################################


### Configuration Section #####################################################
# Adjust the following variables to match your environment and preferences.
LOG_DIR="/path/to/log/directory"        # Directory for logs
MAX_LOGS=7                              # Maximum number of log files to keep
SOURCE="/path/to/local/source"          # Local source directory
DEST="crypt_remote:path/to/destination" # rclone crypt remote destination
BANDWIDTH_LIMIT="10M"                   # Bandwidth limit, e.g. "10M" or "off"
ENABLE_PRIVATEBIN_UPLOAD="false"        # Set "true" to enable PrivateBin

# Discord Configuration (optional)
DISCORD_WEBHOOK_URL=""                  # Your Discord webhook URL
DISCORD_ICON_OVERRIDE=""                # Custom icon URL for notifications
DISCORD_NAME_OVERRIDE="Rclone Sync"     # Custom username override for messages

### Functions Section #########################################################

# Escape a string for use as a JSON value
escape_json() {
    printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

# Send a formatted Discord notification
send_notification() {
    local status=$1
    local message=$2
    local color=$3  # Green: 65280, Red: 16711680, Yellow: 16776960

    # If no webhook URL is provided, skip sending
    [ -z "$DISCORD_WEBHOOK_URL" ] && return 0

    local escaped_message
    escaped_message=$(escape_json "$message")

    notification_data=$(cat <<EOF
{
    "username": "$DISCORD_NAME_OVERRIDE",
    "avatar_url": "$DISCORD_ICON_OVERRIDE",
    "embeds": [
      {
        "title": "Rclone Sync Status: $status",
        "color": $color,
        "fields": [
          {
            "name": "Status Details",
            "value": $escaped_message
          },
          {
            "name": "Sync Info",
            "value": "Source: \`$SOURCE\`\nDestination: \`$DEST\`"
          },
          {
            "name": "Timestamp",
            "value": "$(date '+%Y-%m-%d %H:%M:%S')"
          }
        ]
      }
    ]
}
EOF
)
    /usr/bin/curl -H "Content-Type: application/json" -d "$notification_data" "$DISCORD_WEBHOOK_URL"
}

# Log a message with a timestamp and append to both console and log file
log_message() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

### PrivateBin Version Check Section ##########################################
if [ "$ENABLE_PRIVATEBIN_UPLOAD" = "true" ]; then
    # Check if 'privatebin' is installed
    if ! command -v privatebin &> /dev/null; then
        echo "The 'privatebin' CLI tool is not installed. Please install it from:"
        echo "https://github.com/gearnode/privatebin"
        exit 1
    fi

    # Extract full version info from privatebin
    FULL_VERSION=$(privatebin -v | awk '{print $3}')  # e.g. v2.0.2-97cd0b5
    TAG_NAME=$(echo "$FULL_VERSION" | cut -d'-' -f1)   # e.g. v2.0.2
    COMMIT_SHORT=$(echo "$FULL_VERSION" | cut -d'-' -f2) # e.g. 97cd0b5

    # Fetch the latest release tag from GitHub
    LATEST_TAG=$(curl -s https://api.github.com/repos/gearnode/privatebin/releases/latest | grep '"tag_name":' | awk -F'"' '{print $4}')

    # Compare installed version/tag with the latest
    if [ "$TAG_NAME" != "$LATEST_TAG" ]; then
        echo "Warning: Installed privatebin tag ($TAG_NAME) does not match the latest release tag ($LATEST_TAG)."
        echo "Your version: $FULL_VERSION | Latest version: $LATEST_TAG"
        echo "Consider updating: https://github.com/gearnode/privatebin/releases/latest"
    else
        # If tags match, verify the commit hash
        TAG_COMMIT_URL="https://api.github.com/repos/gearnode/privatebin/git/refs/tags/${LATEST_TAG}"
        FULL_COMMIT_SHA=$(curl -s "$TAG_COMMIT_URL" | grep '"sha":' | head -n 1 | awk -F'"' '{print $4}')

        if [ -z "$FULL_COMMIT_SHA" ]; then
            echo "Warning: Unable to retrieve commit SHA for tag $LATEST_TAG."
            echo "Your version: $FULL_VERSION | Latest version: $LATEST_TAG"
        else
            GITHUB_SHORT_COMMIT=${FULL_COMMIT_SHA:0:7}
            if [ "$GITHUB_SHORT_COMMIT" = "$COMMIT_SHORT" ]; then
                echo "You are using the latest privatebin version and commit!"
                echo "Your version: $FULL_VERSION | Latest: $LATEST_TAG-$GITHUB_SHORT_COMMIT"
            else
                echo "Warning: Your installed commit ($COMMIT_SHORT) does not match ($GITHUB_SHORT_COMMIT) for $LATEST_TAG."
                echo "Your version: $FULL_VERSION | Latest: $LATEST_TAG-$GITHUB_SHORT_COMMIT"
                echo "Consider updating or verifying your binary."
            fi
        fi
    fi
fi

### Logging Setup & Rotation Section ##########################################
# Create log directory if it doesn't exist
mkdir -p "$LOG_DIR"

# Generate timestamp for log file
TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
LOG_FILE="${LOG_DIR}/${TIMESTAMP}-rcloneSync.log"

# Log rotation - remove oldest logs if count exceeds MAX_LOGS
while [ $(ls -1 "$LOG_DIR"/*.log 2>/dev/null | wc -l) -ge $MAX_LOGS ]; do
    oldest_log=$(ls -1t "$LOG_DIR"/*.log | tail -n 1)
    rm "$oldest_log"
done

### Start Sync Operation Section ##############################################
log_message "Starting rclone sync operation"
log_message "Source: $SOURCE"
log_message "Destination: $DEST"

# Send starting notification to Discord
send_notification "Started" "Beginning sync operation..." "16776960"

start_time=$(date +%s)

### Rclone Execution Section ##################################################
temp_output=$(mktemp)

rclone sync "$SOURCE" "$DEST" \
    --create-empty-src-dirs \
    --verbose \
    --bwlimit "$BANDWIDTH_LIMIT" \
    --retries 3 \
    --retries-sleep 10s \
    --timeout 30s \
    --low-level-retries 10 \
    --stats 1m \
    --stats-file-name-length 0 \
    --transfers=8 \
    2>&1 | tee "$temp_output"

EXIT_STATUS=${PIPESTATUS[0]}

### Post-Execution & Status Processing Section ###############################
end_time=$(date +%s)
duration=$((end_time - start_time))
duration_text=$(printf '%dh:%dm:%ds' $((duration/3600)) $((duration%3600/60)) $((duration%60)))

cat "$temp_output" >> "$LOG_FILE"
rm "$temp_output"

# Parse summary stats
data_line=$(grep "Transferred:" "$LOG_FILE" | grep "B" | tail -n 1)
if [ -n "$data_line" ]; then
    data=$(echo "$data_line" | awk -F',' '{print $1}' | sed 's/Transferred://; s/^[[:space:]]*//; s/[[:space:]]*$//')
else
    data=""
fi

checks_line=$(grep "Checks:" "$LOG_FILE" | tail -n 1)
if [ -n "$checks_line" ]; then
    checks_raw=$(echo "$checks_line" | sed 's/^.*Checks:\s*//; s/,.*//')
    checks="$checks_raw"
else
    checks=""
fi

files_line=$(grep "^Transferred:" "$LOG_FILE" | grep -v "B" | tail -n 1)
if [ -n "$files_line" ]; then
    files_raw=$(echo "$files_line" | awk -F',' '{print $1}' | sed 's/Transferred://; s/^[[:space:]]*//; s/[[:space:]]*$//')
    files_num=$(echo "$files_raw" | awk '{print $1}')
    files="$files_num files"
else
    files="0 files"
fi

# If successful and data looks like X B / X B, simplify it
if [ $EXIT_STATUS -eq 0 ] && [ -n "$data" ]; then
    if echo "$data" | grep -q "/"; then
        left_side=$(echo "$data" | awk -F'/' '{print $1}' | xargs)
        right_side=$(echo "$data" | awk -F'/' '{print $2}' | xargs)
        if [ "$left_side" = "$right_side" ]; then
            data="$left_side"
        fi
    fi
fi

### Building Status Message Section ###########################################
if [ $EXIT_STATUS -eq 0 ]; then
    # Success
    status_message="✅ Sync completed successfully
⏱️ Duration: $duration_text
📦 Data: $data
📄 Files: $files
🔍 Checks: $checks"
else
    # Failure
    error_info=$(tail -n 5 "$LOG_FILE" | grep -v "^\[")
    status_message="❌ Sync failed (Exit: $EXIT_STATUS)
⏱️ Duration: $duration_text
⚠️ Last error:
$error_info"
fi

# Optionally upload logs to PrivateBin
if [ "$ENABLE_PRIVATEBIN_UPLOAD" = "true" ]; then
    PRIVATEBIN_LINK=$(cat "$LOG_FILE" | privatebin create)
    PRIVATEBIN_MARKDOWN="[View Logs]($PRIVATEBIN_LINK)"
    status_message="$status_message
🔗 $PRIVATEBIN_MARKDOWN"
fi

### Send Final Notification & Cleanup Section ################################
if [ $EXIT_STATUS -eq 0 ]; then
    send_notification "Success" "$status_message" "65280"
    log_message "Sync completed successfully"
else
    send_notification "Failed" "$status_message" "16711680"
    log_message "Sync failed with exit status: $EXIT_STATUS"
fi

log_message "Operation completed"

exit $EXIT_STATUS

# Rclone Sync Script
# <https://github.com/engels74/arrsenal-of-scripts>
# This script syncs files via rclone to crypt remote
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
