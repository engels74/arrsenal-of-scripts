#!/usr/bin/env bash

# fail2ban-monitor.sh - Real-time Fail2ban IP Monitor
# 
# This script will continuously monitor and display IPs that are currently banned by
# Fail2ban. The display will be updated in real-time as new IPs are banned or existing
# bans are lifted. The script must be run as root (using sudo) since it needs to access
# the fail2ban database and communicate with the fail2ban-client.

# Constants
readonly F2B_DB="/var/lib/fail2ban/fail2ban.sqlite3"
readonly F2B_LOCAL_CONF="/etc/fail2ban/jail.local"
readonly F2B_CONF="/etc/fail2ban/jail.conf"
readonly REQUIRED_PACKAGES="sqlite3"
readonly UPDATE_INTERVAL=1

# Color definitions
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly NC='\033[0m' # No Color
readonly BOLD='\033[1m'

# Error handling
trap cleanup SIGINT SIGTERM EXIT

# Function: Display error message and exit
error_exit() {
    echo -e "${RED}Error: $1${NC}" >&2
    exit 1
}

# Function: Cleanup on exit
cleanup() {
    tput cnorm # Show cursor
    clear
    exit 0
}

# Function: Check if running as root
check_privileges() {
    if [ "$(id -u)" != "0" ]; then
        error_exit "This script must be run as root (sudo)"
    fi
}

# Function: Check for required dependencies
check_dependencies() {
    # Check for required packages
    for pkg in $REQUIRED_PACKAGES; do
        if ! command -v "$pkg" >/dev/null 2>&1; then
            error_exit "$pkg is required but not installed"
        fi
    done

    # Check fail2ban service status
    if ! systemctl is-active --quiet fail2ban; then
        error_exit "fail2ban service is not running"
    fi

    if [ ! -f "$F2B_DB" ]; then
        error_exit "Fail2ban database not found at $F2B_DB"
    fi

    if [ ! -f "$F2B_LOCAL_CONF" ] && [ ! -f "$F2B_CONF" ]; then
        error_exit "No fail2ban configuration found"
    fi
}

# Function: Get banned IPs from database
get_banned_ips() {
    local current_time=$(date +%s)
    sqlite3 "$F2B_DB" "
        SELECT 
            ip,
            bantime,
            timeofban,
            (timeofban + bantime - $current_time) as remaining,
            jail
        FROM bans 
        WHERE (timeofban + bantime) > $current_time
        ORDER BY remaining DESC;
    "
}

# Function: Get currently banned IPs from fail2ban-client
get_client_banned_ips() {
    local jail="$1"
    fail2ban-client status "$jail" | grep "Banned IP list:" | sed 's/.*Banned IP list:\s*//' | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'
}

# Function: Get all active jails
get_active_jails() {
    fail2ban-client status | grep "Jail list:" | sed 's/.*Jail list:\s*//' | tr ',' '\n' | tr -d ' '
}

# Function: Draw table header
draw_header() {
    echo -e "${BOLD}┌────────────────────────────────────────────────────────────────────┐${NC}"
    echo -e "${BOLD}│                     Fail2ban Real-time Monitor                     │${NC}"
    echo -e "${BOLD}├────────────────────────────────────────────────────────────────────┤${NC}"
    # Add table headers
    printf "${BOLD}│ %-15s │ %-14s │ %-14s │ %-14s │${NC}\n" "IP Address" "Ban Duration" "Remaining" "Jail"
    echo -e "${BOLD}├$(printf '─%.0s' $(seq 1 17))┬$(printf '─%.0s' $(seq 1 16))┬$(printf '─%.0s' $(seq 1 16))┬$(printf '─%.0s' $(seq 1 16))┤${NC}"
}

# Function: Format time duration
format_duration() {
    local seconds=$1
    local days=$((seconds/86400))
    local hours=$(((seconds%86400)/3600))
    local minutes=$(((seconds%3600)/60))
    local secs=$((seconds%60))
    
    if [ $days -gt 0 ]; then
        printf "%dd %02dh" $days $hours
    elif [ $hours -gt 0 ]; then
        printf "%02dh %02dm" $hours $minutes
    else
        printf "%02dm %02ds" $minutes $secs
    fi
}

# Function: Get state hash that includes both database and fail2ban-client IPs
get_state_hash() {
    local state=""
    
    # Add database IPs to state
    while IFS="|" read -r ip bantime timeofban remaining jail; do
        if [ ! -z "$ip" ]; then
            state+="DB:$ip:$jail:$remaining"
        fi
    done < <(get_banned_ips)
    
    # Add fail2ban-client IPs to state
    while read -r jail; do
        while read -r ip; do
            if [ ! -z "$ip" ]; then
                state+="CLIENT:$ip:$jail"
            fi
        done < <(get_client_banned_ips "$jail")
    done < <(get_active_jails)
    
    # Sort the state to ensure consistent hashing
    echo "$state" | sort | md5sum
}

# Function: Display banned IPs
display_bans() {
    local current_hash
    local previous_hash=""
    local lines_to_clear=0
    
    # Save cursor position and hide it
    tput civis
    
    # Initial clear and draw
    clear
    
    while true; do
        current_hash=$(get_state_hash)
        
        # Only update display if state has changed
        if [[ "$current_hash" != "$previous_hash" ]]; then
            # Save cursor position
            tput sc
            
            # Move to top of screen
            tput cup 0 0
            
            # Draw header
            draw_header
            
            # Create arrays to track IPs
            declare -A displayed_ips
            declare -A client_ips
            
            # Display IPs from SQLite with full timing information
            local db_ips=0
            while IFS="|" read -r ip bantime timeofban remaining jail; do
                if [ ! -z "$ip" ]; then
                    local duration=$(format_duration $bantime)
                    local remain=$(format_duration $remaining)
                    printf "${BOLD}│${NC} %-15s ${BOLD}│${NC} %-14s ${BOLD}│${NC} %-14s ${BOLD}│${NC} %-14s ${BOLD}│${NC}\n" \
                           "$ip" "$duration" "$remain" "$jail"
                    displayed_ips["$ip"]=1
                    ((db_ips++))
                fi
            done < <(get_banned_ips)
            
            # Get all client IPs first
            while read -r jail; do
                while read -r ip; do
                    if [ ! -z "$ip" ] && [ -z "${displayed_ips[$ip]}" ]; then
                        client_ips["$ip"]="$jail"
                    fi
                done < <(get_client_banned_ips "$jail")
            done < <(get_active_jails)
            
            # Display client IPs
            for ip in "${!client_ips[@]}"; do
                jail="${client_ips[$ip]}"
                printf "${BOLD}│${NC} %-15s ${BOLD}│${NC} %-14s ${BOLD}│${NC} %-14s ${BOLD}│${NC} %-14s ${BOLD}│${NC}\n" \
                       "$ip" "unknown" "active" "$jail"
            done
            
            # Close the table
            echo -e "${BOLD}└$(printf '─%.0s' $(seq 1 17))┴$(printf '─%.0s' $(seq 1 16))┴$(printf '─%.0s' $(seq 1 16))┴$(printf '─%.0s' $(seq 1 16))┘${NC}"
            
            # Clear to end of screen before adding status lines
            tput ed
            
            # Add status line and quit message with proper spacing
            echo -e "\n${YELLOW}Total Banned IPs: $((db_ips + ${#client_ips[@]}))${NC} │ ${GREEN}Last Updated: $(date '+%Y-%m-%d %H:%M:%S')${NC}"
            echo -e "\n${BOLD}Press 'q' to quit${NC}"
            
            # Calculate total lines for next clear
            lines_to_clear=$((db_ips + ${#client_ips[@]} + 8))
            
            # Restore cursor position
            tput rc
            
            previous_hash=$current_hash
        fi
        
        # Check for quit command
        read -t 1 -N 1 input
        if [[ $input = "q" ]] || [[ $input = "Q" ]]; then
            break
        fi
    done
    
    # Show cursor before exiting
    tput cnorm
}

# Main execution
main() {
    check_privileges
    check_dependencies
    trap cleanup EXIT
    trap 'error_exit "Script interrupted."' INT TERM
    
    # Clear screen once at start
    clear
    display_bans
}

main

# Fail2ban IP Monitor
# <https://github.com/engels74/arrsenal-of-scripts>
# This script monitors and displays banned IPs from fail2ban
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
