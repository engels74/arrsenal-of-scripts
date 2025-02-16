#!/usr/bin/env zsh
# Fail2ban Monitor Dashboard (zsh)
#
# Requirements: sqlite3, fail2ban-client, tput, grep, sed, awk
#
# This script:
#   • Ensures that it’s run as root and that required dependencies are installed.
#   • Enters the alternate screen and hides the cursor.
#   • Displays a continuously updated dashboard with subtle calm colors.
#   • Shows only active bans in a nicely formatted table.
#   • Displays the total number of active banned IPs in the footer.
#
# ANSI color definitions (subtle colors):
BOLD="\033[1m"
RESET="\033[0m"
HEADER_COLOR="\033[36m"   # soft cyan
FOOTER_COLOR="\033[36m"   # soft cyan
TABLE_COLOR="\033[37m"    # light gray

# --- Root Check ---
if [[ $EUID -ne 0 ]]; then
  echo "This script must be run as root. Exiting."
  exit 1
fi

# --- Dependency Check ---
function check_dependencies() {
  local dependencies=(sqlite3 fail2ban-client tput grep sed awk)
  for dep in "${dependencies[@]}"; do
    if ! command -v "$dep" &> /dev/null; then
      echo "Dependency '$dep' is not installed. Please install it then try again."
      exit 1
    fi
  done
}
check_dependencies

# --- Enter Alternate Screen & Hide Cursor ---
tput smcup
tput civis

# --- Cleanup on Exit ---
function cleanup() {
  tput rmcup  # restore main screen
  tput cnorm  # show cursor
  exit
}
trap cleanup EXIT

# --- Global Variables ---
ACTIVE_BAN_COUNT=0
TABLE_OUTPUT=""

# --- Functions ---

# get_sqlite_bans:
#   Returns lines in the format "jail|ip|timeofban|bantime" from Fail2ban’s sqlite DB.
function get_sqlite_bans() {
  local db_path="/var/lib/fail2ban/fail2ban.sqlite3"
  if [[ -f "$db_path" ]]; then
    sqlite3 "$db_path" "SELECT jail, ip, timeofban, bantime FROM bans;"
  fi
}

# get_client_bans:
#   Returns a space-separated list of banned IPs using fail2ban-client (assumes the 'sshd' jail).
function get_client_bans() {
  local jail="sshd"
  local output
  output=$(fail2ban-client status "$jail")
  echo "$output" | grep -E "Banned IP list:" | sed 's/.*Banned IP list:[[:space:]]*//'
}

# format_time:
#   Converts a number of seconds to HH:MM:SS.
function format_time() {
  local secs=$1
  printf "%02d:%02d:%02d" $((secs/3600)) $(((secs%3600)/60)) $((secs%60))
}

# render_header:
#   Returns the colored header text.
function render_header() {
  echo "${HEADER_COLOR}${BOLD}=========================================================="
  echo "                     FAIL2BAN MONITOR                     "
  echo "==========================================================${RESET}"
}

# render_footer:
#   Returns the colored footer with ACTIVE_BAN_COUNT and timestamp.
function render_footer() {
  local now
  now=$(date +"%Y-%m-%d - %H:%M:%S")
  echo "${FOOTER_COLOR}${BOLD}=========================================================="
  echo "Total IPs banned: ${ACTIVE_BAN_COUNT}"
  echo "                   Updated: ${now}"
  echo "==========================================================${RESET}"
}

# render_table_content:
#   Builds the table (using Unicode box-drawing characters) of active bans.
#   Updates the globals TABLE_OUTPUT and ACTIVE_BAN_COUNT.
function render_table_content() {
  local current_time=$(date +%s)
  local client_ips_str=$(get_client_bans)
  local -a client_ips
  # Split the client list into an array.
  IFS=' ' read -r -A client_ips <<< "$client_ips_str"

  local -a table_lines
  table_lines+=("┌────────────────────┬───────────────┬────────────┐")
  table_lines+=("│     Banned IP      │ Ban Duration  │  Time Left │")
  table_lines+=("├────────────────────┼───────────────┼────────────┤")

  local -a active_ips
  local bans
  bans=$(get_sqlite_bans)

  while IFS="|" read -r jail ip timeofban bantime; do
    local ban_end=$(( timeofban + bantime ))
    local time_left=$(( ban_end - current_time ))
    (( time_left <= 0 )) && continue

    # Only include if the IP appears in the fail2ban-client list.
    local valid=0
    for cip in "${client_ips[@]}"; do
      if [[ "$cip" == "$ip" ]]; then
        valid=1
        break
      fi
    done
    (( valid == 0 )) && continue

    local ban_duration_formatted=$(format_time "$bantime")
    local time_left_formatted=$(format_time "$time_left")
    table_lines+=("│ $(printf "%-18s" "$ip") │ $(printf "%-13s" "$ban_duration_formatted") │ $(printf "%-10s" "$time_left_formatted") │")
    active_ips+=("$ip")
  done <<< "$bans"

  ACTIVE_BAN_COUNT=${#active_ips[@]}

  if (( ACTIVE_BAN_COUNT == 0 )); then
    table_lines+=("│ $(printf "%-48s" "No active bans.") │")
  fi

  table_lines+=("└────────────────────┴───────────────┴────────────┘")
  TABLE_OUTPUT="${TABLE_COLOR}$(printf "%s\n" "${table_lines[@]}")${RESET}"
}

# --- Main Loop ---
while true; do
  # Update table content.
  render_table_content

  # Build the complete dashboard block.
  dashboard=$(printf "%s\n\n%s\n\n%s\n" "$(render_header)" "$TABLE_OUTPUT" "$(render_footer)")

  # Get terminal dimensions.
  terminal_width=$(tput cols)
  terminal_height=$(tput lines)

  # Pad dashboard output to full width.
  padded_dashboard=$(echo "$dashboard" | awk -v w="$terminal_width" '{printf "%-" w "s\n", $0}')

  # Count the number of dashboard lines and add extra blank lines if needed.
  dashboard_lines=$(echo "$padded_dashboard" | wc -l)
  extra_lines=$(( terminal_height - dashboard_lines ))
  if (( extra_lines > 0 )); then
    for ((i=0; i < extra_lines; i++)); do
      padded_dashboard="${padded_dashboard}$(printf "%-${terminal_width}s\n" "")"
    done
  fi

  # Reposition the cursor to top-left without a full clear.
  tput cup 0 0
  # Write out the entire dashboard block in one atomic write.
  printf "%s" "$padded_dashboard"

  sleep 1
done

# -----------------------------------------------------------------------------
# License
# -----------------------------------------------------------------------------

# Fail2ban IP Monitor (zsh)
# <https://github.com/engels74/arrsenal-of-scripts>
# This script monitors and displays banned IPs from fail2ban
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