#!/usr/bin/env bash
# hotio-support-script.sh
# A guided helper to produce a great Hotio support request
#
# How to run (single command):
#   curl -4fsSL https://raw.githubusercontent.com/engels74/arrsenal-of-scripts/refs/heads/main/miscellaneous/hotio/hotio-support-script.sh | bash
#
# Quick dry-run locally:
#   bash miscellaneous/hotio/hotio-support-script.sh --dry-run
#
# What this script does:
# - Guides you to pick a container, collects logs + docker-autocompose output
# - Uploads both to https://logs.notifiarr.com via privatebin (1-year expiry)
# - Outputs a Discord-ready message, with clipboard support when available
#
# Design constraints:
# - No persistent installs. Downloads tools into a temp dir and cleans up on exit
# - IPv4-only network calls with retries
# - Graceful fallback if gum or privatebin CLI are not present

set -euo pipefail
IFS=$'\n\t'

# ---------------------- globals ----------------------
SCRIPT_NAME="hotio-support-script"
TMP_DIR="$(mktemp -d -t ${SCRIPT_NAME}.XXXXXX)"
CLEANUP_CMDS=()
OS="$(uname -s)"
ARCH_RAW="$(uname -m)"
DRY_RUN=false
GUM_BIN=""      # path to gum if available
PVBIN_BIN=""    # path to privatebin if available
PRIVATEBIN_CFG="/tmp/privatebin-config.json"
DISCORD_CHANNEL_URL="https://discord.gg/hotio"

# ---------------------- cleanup ----------------------
cleanup() {
  local code=$?
  for cmd in "${CLEANUP_CMDS[@]:-}"; do eval "$cmd" || true; done
  rm -rf "$TMP_DIR" 2>/dev/null || true
  exit $code
}
trap cleanup EXIT INT TERM

# ---------------------- utils ----------------------
color() { case "${1:-}" in red) echo -e "\033[31m${2}\033[0m";; green) echo -e "\033[32m${2}\033[0m";; yellow) echo -e "\033[33m${2}\033[0m";; blue) echo -e "\033[34m${2}\033[0m";; magenta) echo -e "\033[35m${2}\033[0m";; cyan) echo -e "\033[36m${2}\033[0m";; *) echo -e "$2";; esac }
log() { echo "$(color cyan "[${SCRIPT_NAME}]") $*"; }
die() { echo "$(color red "[ERROR]") $*"; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

gum_or() { # gum_or <gum-subcommand-and-args...> -- <fallback-echo>
  if [[ -n "$GUM_BIN" ]]; then "$GUM_BIN" "$@"; else shift $(( $# )); fi
}

spinner_run() { # spinner_run "msg" -- command args...
  local msg="$1"; shift; local dash="$1"; shift || true
  if [[ -n "$GUM_BIN" ]]; then
    "$GUM_BIN" spin --spinner line --title "$msg" -- "$@"
  else
    log "$msg"; "$@"
  fi
}

retry_curl() { # retry_curl URL OUTFILE
  local url="$1"; local out="$2"; local tries=3; local delay=2; local i
  for ((i=1;i<=tries;i++)); do
    if curl -4fsSL --connect-timeout 10 --retry 0 "$url" -o "$out" 2>/dev/null; then return 0; fi
    sleep "$delay"; delay=$((delay*2))
  done
  return 1
}

ensure_connectivity() { if ! curl -4fsSL --connect-timeout 5 https://logs.notifiarr.com >/dev/null 2>&1; then
    log "No IPv4 internet connectivity to logs.notifiarr.com; uploads may fail. We'll continue and let you copy locally if needed."
  fi
}

map_arch() { case "$ARCH_RAW" in x86_64|amd64) echo amd64;; aarch64|arm64) echo arm64;; i386|i686) echo i386;; armv7*|armhf) echo armv7;; *) echo "$ARCH_RAW";; esac }
map_os() { case "$OS" in Linux) echo linux;; Darwin) echo darwin;; *) echo "$OS";; esac }
# Gum release asset tokens differ (capitalized OS, different arch token)
gum_map_arch() { case "$ARCH_RAW" in x86_64|amd64) echo x86_64;; aarch64|arm64) echo arm64;; i386|i686) echo i386;; armv7*|armhf) echo armv7;; *) echo "$ARCH_RAW";; esac }
gum_map_os() { case "$OS" in Linux) echo Linux;; Darwin) echo Darwin;; *) echo "$OS";; esac }

# ---------------------- downloads ----------------------
# Scrape GitHub latest release page for an asset matching a pattern (best effort)
download_gh_asset_latest() {
  local owner="$1" repo="$2" pattern="$3" out="$4"
  local base="https://github.com/${owner}/${repo}/releases/latest"
  local page="$TMP_DIR/${repo}-latest.html"
  if ! retry_curl "$base" "$page"; then return 1; fi
  # Find first matching href to an asset
  local rel_url
  rel_url=$(grep -oE "/${owner}/${repo}/releases/download/[^"]*${pattern}[^"]*" "$page" | head -n1 || true)
  [[ -z "$rel_url" ]] && return 1
  retry_curl "https://github.com${rel_url}" "$out"
}

extract_if_archive() {
  local archive="$1" dest="$2"; mkdir -p "$dest"
  case "$archive" in
    *.tar.gz|*.tgz) tar -xzf "$archive" -C "$dest" 2>/dev/null || return 1;;
    *.zip) if have unzip; then unzip -o "$archive" -d "$dest" >/dev/null; else return 1; fi;;
    *) return 2;;
  esac
}

ensure_gum() {
  if have gum; then GUM_BIN="$(command -v gum)"; return 0; fi
  local os arch; os=$(gum_map_os); arch=$(gum_map_arch)
  # Gum assets look like: gum_0.16.2_Linux_x86_64.tar.gz (version varies)
  local pattern="gum_.*_${os}_${arch}\.tar\.gz"
  local arc="$TMP_DIR/gum.tgz"; local ext="$TMP_DIR/gum"
  if download_gh_asset_latest charmbracelet gum "$pattern" "$arc"; then
    if extract_if_archive "$arc" "$ext"; then
      # find gum binary
      local cand
      cand=$(find "$ext" -type f -name gum -perm -u+x -print -quit 2>/dev/null || true)
      if [[ -n "$cand" ]]; then GUM_BIN="$cand"; return 0; fi
    fi
  fi
  # No brittle direct URL fallback; silently continue without gum
  log "gum not available; continuing with basic prompts."
}

ensure_privatebin() {
  if have privatebin; then PVBIN_BIN="$(command -v privatebin)"; return 0; fi
  local os arch; os=$(map_os); arch=$(map_arch)
  # PrivateBin assets look like: privatebin_2.1.0_linux_amd64.tar.gz (version varies)
  # Try tar.gz then zip
  local pattern_tgz="privatebin_.*_${os}_${arch}\.tar\.gz"
  local pattern_zip="privatebin_.*_${os}_${arch}\.zip"
  local arc ext cand
  for p in "$pattern_tgz" "$pattern_zip"; do
    arc="$TMP_DIR/privatebin_asset"
    if download_gh_asset_latest gearnode privatebin "$p" "$arc"; then
      ext="$TMP_DIR/pv"
      if extract_if_archive "$arc" "$ext"; then
        cand=$(find "$ext" -type f -name privatebin -perm -u+x -print -quit 2>/dev/null || true)
        if [[ -n "$cand" ]]; then PVBIN_BIN="$cand"; return 0; fi
      else
        # maybe it's a raw binary
        chmod +x "$arc" 2>/dev/null || true
        if file "$arc" | grep -qiE 'executable|Mach-O|ELF'; then PVBIN_BIN="$arc"; return 0; fi
      fi
    fi
  done
  # No brittle raw URL fallback
  log "privatebin CLI not available; will offer manual copy instead of auto-upload."
}

# ---------------------- UX helpers ----------------------
clear_screen() { command -v clear >/dev/null && clear || printf "\n\n"; }

choose_container() {
  local name=""; local all
  if ! have docker; then die "Docker not found. Please install Docker and ensure the daemon is running."; fi
  if ! docker info >/dev/null 2>&1; then die "Docker daemon not running or not accessible for current user."; fi
  mapfile -t all < <(docker ps -a --format '{{.Names}}' | sort)
  if [[ ${#all[@]} -eq 0 ]]; then die "No containers found on this host."; fi
  if [[ -n "$GUM_BIN" ]]; then
    name=$("$GUM_BIN" choose --limit 1 --height 15 --header "Select your container" -- "${all[@]}") || true
  fi
  if [[ -z "$name" ]]; then
    echo "Available containers:"; printf " - %s\n" "${all[@]}"
    read -r -p "Enter container name: " name
  fi
  if ! docker ps -a --format '{{.Names}}' | grep -Fxq "$name"; then
    log "Container '$name' not found. Let's try again."
    choose_container; return
  fi
  echo "$name"
}

confirm() {
  local prompt="$1"; local ok=""
  if [[ -n "$GUM_BIN" ]]; then "$GUM_BIN" confirm "$prompt" && return 0 || return 1; fi
  read -r -p "$prompt [y/N]: " ok; [[ "${ok,,}" == y* ]]
}

multiline_input() {
  local prompt="$1"; local min_len=${2:-0}; local text=""
  if [[ -n "$GUM_BIN" ]]; then
    text=$("$GUM_BIN" write --width 80 --height 12 --placeholder "$prompt") || true
  else
    echo "$prompt"; echo "End input with a single '.' on its own line:"; local line
    while IFS= read -r line; do [[ "$line" == "." ]] && break; text+="$line\n"; done
  fi
  local len=${#text}
  if (( len < min_len )); then
    log "Please provide at least $min_len characters (you entered $len)."
    multiline_input "$prompt" "$min_len"; return
  fi
  printf "%s" "$text"
}

# ---------------------- main flow ----------------------
main() {
  clear_screen
  log "Welcome! This will help you craft a complete Hotio support request."
  ensure_connectivity
  spinner_run "Preparing interactive tools (gum)" -- bash -c 'true'; ensure_gum || true
  spinner_run "Preparing uploader (privatebin)" -- bash -c 'true'; ensure_privatebin || true

  # Dry-run option for quick local testing (skips network and uploads)
  if [[ "${1:-}" == "--dry-run" ]]; then
    log "Dry run: skipping container selection and uploads."
    exit 0
  fi

  # Step 1: Container
  log "Step 1/3: Select the Docker container to diagnose"
  local container; container="$(choose_container)"

  # Step 2: Collect logs and compose
  log "Step 2/3: Collecting logs and container compose (read-only)"
  log "Note: You will be asked before uploading anything."
  local logs_file="$TMP_DIR/${container}_logs.txt"
  local comp_file="$TMP_DIR/${container}_compose.yaml"
  if ! confirm "Proceed to gather logs for '$container'?"; then die "Aborted by user."; fi

  spinner_run "Collecting docker logs" -- bash -c "docker logs --timestamps --since=24h '$container' > '$logs_file' 2>&1 || true"
  spinner_run "Generating compose via docker-autocompose" -- bash -c "docker run --rm -v /var/run/docker.sock:/var/run/docker.sock:ro ghcr.io/red5d/docker-autocompose '$container' > '$comp_file' 2>/dev/null || true"

  # Step 3: Problem description
  log "Step 3/3: Describe the problem"
  local desc
  desc="$(multiline_input "Please describe your issue in detail. Suggested prompts:\n- What were you trying to do?\n- What did you expect?\n- What actually happened?\n- When did it start?\n- What have you already tried?" 50)"

  # Consent to upload
  echo
  log "Review:"
  echo " - Logs file: $logs_file ($(wc -c <"$logs_file" 2>/dev/null || echo 0) bytes)"
  echo " - Compose file: $comp_file ($(wc -c <"$comp_file" 2>/dev/null || echo 0) bytes)"
  echo
  local do_upload=false
  if confirm "Upload logs and compose to logs.notifiarr.com (1-year expiration)?"; then do_upload=true; fi

  # PrivateBin config
  echo '{"bin":[{"name":"hotio-support","host":"https://logs.notifiarr.com","expire":"1year"}]}' > "$PRIVATEBIN_CFG"

  local logs_url="" comp_url=""
  if $do_upload && [[ -n "$PVBIN_BIN" ]]; then
    if [[ -s "$logs_file" ]]; then
      spinner_run "Uploading logs to PrivateBin" -- bash -c "cat '$logs_file' | '$PVBIN_BIN' --config '$PRIVATEBIN_CFG' create --expire 1year --formatter plaintext > '$TMP_DIR/logs.up' 2>/dev/null || true"
      logs_url="$(grep -Eo 'https?://[^ ]+' "$TMP_DIR/logs.up" | tail -n1 || true)"
    fi
    if [[ -s "$comp_file" ]]; then
      spinner_run "Uploading compose to PrivateBin" -- bash -c "cat '$comp_file' | '$PVBIN_BIN' --config '$PRIVATEBIN_CFG' create --expire 1year --formatter plaintext > '$TMP_DIR/compose.up' 2>/dev/null || true"
      comp_url="$(grep -Eo 'https?://[^ ]+' "$TMP_DIR/compose.up" | tail -n1 || true)"
    fi
  elif $do_upload; then
    log "Skipping upload: privatebin CLI unavailable."
  fi

  # Final output
  echo
  log "Your Discord-ready support request (copy everything between lines):"
  echo "---------------- 8< ----------------"
  echo "Problem Description:"; echo
  echo "$desc"; echo
  if [[ -n "$logs_url" ]]; then echo "Docker Logs: $logs_url"; else echo "Docker Logs: (not uploaded) -> attach '$logs_file' or upload to https://logs.notifiarr.com"; fi
  if [[ -n "$comp_url" ]]; then echo "Docker Compose (autogenerated): $comp_url"; else echo "Docker Compose: (not uploaded) -> attach '$comp_file' or upload to https://logs.notifiarr.com"; fi
  echo; echo "Reminder: Ensure your request follows hotio's rules (include exact image tag, relevant env vars, and steps to reproduce)."
  echo "---------------- 8< ----------------"

  # Clipboard (optional)
  if have pbcopy; then
    { echo "Problem Description:"; echo; echo "$desc"; echo; echo "Docker Logs: ${logs_url:-"(not uploaded)"}"; echo "Docker Compose: ${comp_url:-"(not uploaded)"}"; echo; echo "Reminder: Ensure your request follows hotio's rules."; } | pbcopy
    log "Copied to clipboard (pbcopy)."
  elif have xclip; then
    { echo "Problem Description:"; echo; echo "$desc"; echo; echo "Docker Logs: ${logs_url:-"(not uploaded)"}"; echo "Docker Compose: ${comp_url:-"(not uploaded)"}"; echo; echo "Reminder: Ensure your request follows hotio's rules."; } | xclip -selection clipboard
    log "Copied to clipboard (xclip)."
  fi

  echo
  log "Post this in the hotio Discord: $DISCORD_CHANNEL_URL"
  log "All temporary files will be removed on exit."
}

main "$@"

