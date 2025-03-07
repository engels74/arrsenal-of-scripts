#!/bin/bash

# =================================================================
# Valheim Mod Update Checker
# =================================================================
# Description:
#   This script checks for updates to installed Valheim mods by comparing
#   local versions with the latest versions available on Thunderstore.
#   It supports checking multiple mod folders and displays results in a
#   formatted table with color-coded status indicators.
#
# Usage:
#   Simply configure the MOD_FOLDERS array with paths to directories
#   containing mod folders. Each mod folder should follow the naming format:
#   Author-ModName-Version (e.g., Azumatt-AAA_Crafting-1.6.4)
#
# Requirements:
#   - curl: For API requests
#   - jq: For JSON parsing
# =================================================================

# ===== Configuration Variables =====
# Log configuration
LOG_DIR="$(dirname "$0")/logs"  # Logs directory in the same location as the script
LOG_FILENAME="modCheck.log"     # Just the filename
LOG_FILE="${LOG_DIR}/${LOG_FILENAME}"

# Define mod folders to check - add as many as needed
MOD_FOLDERS=(
    "/opt/docker-all/gameServers/valheim/srv01/data/bepinex/BepInEx/plugins"
    "/opt/docker-all/gameServers/valheim/srv01/config/bepinex/AzuAntiCheat_Greylist"
    "/opt/docker-all/gameServers/valheim/srv01/config/bepinex/AzuAntiCheat_Whitelist"
    # Add more folders as needed
)

# API configuration
API_URL="https://thunderstore.io/c/valheim/api/v1/package/"

# ===== Color Definitions =====
# ANSI color codes
RESET="\e[0m"
BOLD="\e[1m"
RED="\e[31m"
GREEN="\e[32m"
YELLOW="\e[33m"
BLUE="\e[34m"
MAGENTA="\e[35m"
CYAN="\e[36m"

# Table formatting
TABLE_HEADER="${BOLD}${CYAN}"
TABLE_UP_TO_DATE="${GREEN}"
TABLE_UPDATE_AVAILABLE="${YELLOW}"
TABLE_WARNING="${RED}"
TABLE_DIVIDER="${BLUE}"

# ===== Functions =====
# Function to log messages
log_message() {
    echo -e "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"
}

# Function to write to log file only (no console output)
log_only() {
    echo -e "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

# Function to print a table divider
print_divider() {
    echo -e "${TABLE_DIVIDER}+------------------------------+--------------------+---------------+---------------+${RESET}"
}

# Function to print table header
print_header() {
    print_divider
    echo -e "${TABLE_HEADER}| Mod Name                     | Author             | Installed     | Latest        |${RESET}"
    print_divider
}

# Function to print a table row
print_row() {
    local color=$1
    local name=$2
    local author=$3
    local installed=$4
    local latest=$5
    
    # Truncate fields if they're too long
    name="${name:0:28}"
    author="${author:0:18}"
    installed="${installed:0:13}"
    latest="${latest:0:13}"
    
    # Pad fields to fixed width
    printf -v name "%-28s" "$name"
    printf -v author "%-18s" "$author"
    printf -v installed "%-13s" "$installed"
    printf -v latest "%-13s" "$latest"
    
    echo -e "${color}| ${name} | ${author} | ${installed} | ${latest} |${RESET}"
}

# Function to print summary table
print_summary() {
    echo -e "\n${BOLD}${CYAN}=== SUMMARY ===${RESET}"
    echo -e "${TABLE_DIVIDER}+------------------------------+--------------------------------------------------+${RESET}"
    echo -e "${BOLD}| Category                     | Count                                              |${RESET}"
    echo -e "${TABLE_DIVIDER}+------------------------------+--------------------------------------------------+${RESET}"
    echo -e "${TABLE_UP_TO_DATE}| Up to date                   | $1                                                  |${RESET}"
    echo -e "${TABLE_UPDATE_AVAILABLE}| Updates available            | $2                                                  |${RESET}"
    echo -e "${TABLE_WARNING}| Warnings/Errors              | $3                                                  |${RESET}"
    echo -e "${BOLD}| Total mods checked           | $4                                                  |${RESET}"
    echo -e "${TABLE_DIVIDER}+------------------------------+--------------------------------------------------+${RESET}"
}

# ===== Main Script =====
# Create log directory if it doesn't exist
mkdir -p "$LOG_DIR"
# Clear log file before starting new check
> "$LOG_FILE"

# Start script execution
log_message "${BOLD}${CYAN}Starting mod update check${RESET}"
echo ""

# Create a temporary file to store the API response
TMP_FILE=$(mktemp)
log_message "${BLUE}Fetching data from Valheim Thunderstore API...${RESET}"
curl -s "$API_URL" > "$TMP_FILE"

if [ $? -ne 0 ]; then
    log_message "${RED}ERROR: Failed to fetch data from Thunderstore API${RESET}"
    rm -f "$TMP_FILE"
    exit 1
fi

# Create a mapping file for faster lookups
MAPPING_FILE=$(mktemp)
jq -r '.[] | .owner + "," + .name + "," + .versions[0].version_number' "$TMP_FILE" > "$MAPPING_FILE"

# Create a list to hold all found mods to avoid checking duplicates
MODS_LIST=$(mktemp)

# First, collect all mod directories from all paths
for DIR in "${MOD_FOLDERS[@]}"; do
    if [ -d "$DIR" ]; then
        find "$DIR" -maxdepth 1 -type d -not -path "$DIR" -printf "%f\n" >> "$MODS_LIST"
        log_message "${BLUE}Checking directory: $DIR${RESET}"
    else
        log_message "${YELLOW}WARNING: Directory $DIR does not exist. Skipping.${RESET}"
    fi
done

# Sort the list and remove duplicates
UNIQUE_MODS_LIST=$(mktemp)
sort "$MODS_LIST" | uniq > "$UNIQUE_MODS_LIST"

# Process each unique mod
MOD_COUNT=$(wc -l < "$UNIQUE_MODS_LIST")
log_message "${CYAN}Found ${BOLD}$MOD_COUNT${RESET}${CYAN} unique mods. Checking for updates...${RESET}"
echo ""

# Counter for mods that need updating
UPDATE_NEEDED=0
UP_TO_DATE=0
WARNINGS=0

# Arrays to store mod data for table display
declare -a TABLE_ROWS=()
declare -a LOG_ENTRIES=()

# Process all mods and store data for display
while read -r MOD_NAME; do
    # Parse author, name, and version from directory name
    if [[ "$MOD_NAME" =~ (.*)-(.*)-(.*) ]]; then
        AUTHOR="${BASH_REMATCH[1]}"
        NAME="${BASH_REMATCH[2]}"
        INSTALLED_VERSION="${BASH_REMATCH[3]}"
        
        # Look up the latest version from our mapping file
        LATEST_VERSION=$(grep -i "^${AUTHOR},${NAME}," "$MAPPING_FILE" | cut -d',' -f3)
        
        if [[ -z "$LATEST_VERSION" ]]; then
            TABLE_ROWS+=("${TABLE_WARNING}|${NAME}|${AUTHOR}|${INSTALLED_VERSION}|Not found")
            LOG_ENTRIES+=("${YELLOW}WARNING: Could not find mod $AUTHOR/$NAME in Thunderstore API${RESET}")
            ((WARNINGS++))
        else
            if [[ "$INSTALLED_VERSION" != "$LATEST_VERSION" ]]; then
                TABLE_ROWS+=("${TABLE_UPDATE_AVAILABLE}|${NAME}|${AUTHOR}|${INSTALLED_VERSION}|${LATEST_VERSION}")
                LOG_ENTRIES+=("${YELLOW}UPDATE AVAILABLE: $AUTHOR/$NAME - Installed: v$INSTALLED_VERSION, Latest: v$LATEST_VERSION${RESET}")
                ((UPDATE_NEEDED++))
            else
                TABLE_ROWS+=("${TABLE_UP_TO_DATE}|${NAME}|${AUTHOR}|${INSTALLED_VERSION}|${LATEST_VERSION}")
                LOG_ENTRIES+=("${GREEN}UP TO DATE: $AUTHOR/$NAME - v$INSTALLED_VERSION${RESET}")
                ((UP_TO_DATE++))
            fi
        fi
    else
        TABLE_ROWS+=("${TABLE_WARNING}|${MOD_NAME}|Unknown|Unknown|Unknown")
        LOG_ENTRIES+=("${RED}WARNING: Could not parse mod information from directory: $MOD_NAME${RESET}")
        ((WARNINGS++))
    fi
done < "$UNIQUE_MODS_LIST"

# Print table header
print_header

# Print all table rows
for row in "${TABLE_ROWS[@]}"; do
    IFS='|' read -r color name author installed latest <<< "$row"
    print_row "$color" "$name" "$author" "$installed" "$latest"
done

# Print summary table
print_summary "$UP_TO_DATE" "$UPDATE_NEEDED" "$WARNINGS" "$MOD_COUNT"

# Write all log entries to the log file
for entry in "${LOG_ENTRIES[@]}"; do
    log_only "$entry"
done

# Clean up
rm -f "$TMP_FILE" "$MODS_LIST" "$UNIQUE_MODS_LIST" "$MAPPING_FILE"
log_message "${BOLD}${CYAN}Mod update check completed${RESET}"

# -----------------------------------------------------------------------------
# License
# -----------------------------------------------------------------------------

# Valheim Mod Update Checker
# <https://github.com/engels74/arrsenal-of-scripts>
# This script checks for updates for Valheim mods on ThunderStore.io via its API
# Copyright (C) 2025 - engels74
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
