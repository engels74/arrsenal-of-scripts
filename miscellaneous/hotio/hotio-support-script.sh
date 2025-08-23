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
trap cleanup EXIT TERM

# ---------------------- utils ----------------------
color() { case "${1:-}" in red) echo -e "\033[31m${2}\033[0m";; green) echo -e "\033[32m${2}\033[0m";; yellow) echo -e "\033[33m${2}\033[0m";; blue) echo -e "\033[34m${2}\033[0m";; magenta) echo -e "\033[35m${2}\033[0m";; cyan) echo -e "\033[36m${2}\033[0m";; *) echo -e "$2";; esac }
log() { echo "$(color cyan "[${SCRIPT_NAME}]") $*"; }
die() { echo "$(color red "[ERROR]") $*"; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# UI output helper: write interactive messages to the real terminal when possible
ui_out() {
  if [[ -e /dev/tty ]]; then
    printf "%b\n" "$*" >/dev/tty
  else
    printf "%b\n" "$*" >&2
  fi
}

gum_or() { # gum_or <gum-subcommand-and-args...> -- <fallback-echo>
  if [[ -n "$GUM_BIN" ]]; then "$GUM_BIN" "$@"; else shift $(( $# )); fi
}

# Wrapper to ensure gum reads from the real TTY (important for curl | bash)
# We only redirect stdin from /dev/tty so stdout can be captured by callers.
gum_run() {
  if [[ -n "$GUM_BIN" ]]; then
    if [[ -e /dev/tty ]]; then
      "$GUM_BIN" "$@" </dev/tty
    else
      "$GUM_BIN" "$@"
    fi
  else
    return 1
  fi
}


spinner_run() { # spinner_run "msg" -- command args...
  local msg="$1"; shift; local dash="$1"; shift || true
  if [[ -n "$GUM_BIN" ]]; then
    gum_run spin --spinner line --title "$msg" -- "$@"
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
# Resolve a GitHub release asset matching a pattern. Prefer API, fallback to HTML scraping.
download_gh_asset_latest() {
  local owner="$1" repo="$2" pattern="$3" out="$4"
  # Try GitHub API (more stable than HTML scraping)
  local api="https://api.github.com/repos/${owner}/${repo}/releases/latest"
  local json="$TMP_DIR/${repo}-latest.json"
  if retry_curl "$api" "$json"; then
    local url
    url=$(grep -Eo '"browser_download_url": *"[^"]+"' "$json" | cut -d '"' -f4 | grep -E "$pattern" | head -n1 || true)
    if [[ -n "$url" ]]; then
      if retry_curl "$url" "$out"; then return 0; fi
    fi
  fi
  # Fallback: scrape releases page HTML
  local base="https://github.com/${owner}/${repo}/releases/latest"
  local page="$TMP_DIR/${repo}-latest.html"
  if ! retry_curl "$base" "$page"; then return 1; fi
  # Find first matching href to an asset
  local rel_url
  rel_url=$(grep -oE "/${owner}/${repo}/releases/download/[^\" ]*${pattern}[^\" ]*" "$page" | head -n1 || true)
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
  # Allow caller to provide a preinstalled gum path
  if [[ -n "${GUM_BIN:-}" && -x "$GUM_BIN" ]]; then return 0; fi
  if have gum; then GUM_BIN="$(command -v gum)"; return 0; fi
  local os arch; os=$(gum_map_os); arch=$(gum_map_arch)
  # Gum assets look like: gum_0.16.2_Linux_x86_64.tar.gz (version varies)
  local pattern="gum_.*_${os}_${arch}\.tar\.gz"
  local arc="$TMP_DIR/gum.tgz"; local ext="$TMP_DIR/gum"
  log "Attempting to fetch gum for ${os}/${arch} from GitHub releases..."
  if download_gh_asset_latest charmbracelet gum "$pattern" "$arc"; then
    if extract_if_archive "$arc" "$ext"; then
      # find gum binary
      local cand
      cand=$(find "$ext" -type f -name gum -print -quit 2>/dev/null || true)
      if [[ -n "$cand" ]]; then
        chmod +x "$cand" 2>/dev/null || true
        if [[ -x "$cand" ]]; then GUM_BIN="$cand"; log "gum ready: $GUM_BIN"; return 0; fi
      fi
      log "Downloaded gum archive but could not locate executable inside (pattern: $pattern)."
    else
      chmod +x "$arc" 2>/dev/null || true
      if file "$arc" | grep -qiE 'executable|Mach-O|ELF'; then GUM_BIN="$arc"; log "gum ready: $GUM_BIN"; return 0; fi
      log "Failed to extract gum archive. 'tar' may be missing or the archive format changed."
    fi
  else
    log "Failed to download gum from GitHub releases (pattern: $pattern)."
  fi
  # Provide actionable guidance and fall back to basic prompts
  log "$(color yellow "gum could not be prepared. Falling back to basic prompts.\n - OS/arch detected: ${os}/${arch}\n - If you already have gum installed, re-run with GUM_BIN=/path/to/gum before the command.\n - Or install gum from: https://github.com/charmbracelet/gum/releases")"
}

ensure_privatebin() {
  # Respect preinstalled binary first
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
        if [[ -n "$cand" ]]; then PVBIN_BIN="$cand"; break; fi
      else
        # maybe it's a raw binary
        chmod +x "$arc" 2>/dev/null || true
        if file "$arc" | grep -qiE 'executable|Mach-O|ELF'; then PVBIN_BIN="$arc"; break; fi
      fi
    fi
  done
  if [[ -z "$PVBIN_BIN" ]]; then
    log "privatebin CLI not available; will offer manual copy instead of auto-upload."
    return 1
  fi
  # Final sanity check
  if ! "$PVBIN_BIN" -v >/dev/null 2>&1; then
    log "Downloaded privatebin binary does not execute properly; falling back to manual copy."
    PVBIN_BIN=""; return 1
  fi
  return 0
}

# Verify privatebin against latest GitHub tag (best-effort)
verify_privatebin_version() {
  [[ -z "$PVBIN_BIN" ]] && return 1
  local full version tag short latest ref_url sha gh_short
  if ! full=$("$PVBIN_BIN" -v 2>/dev/null); then return 1; fi
  version=$(echo "$full" | awk '{print $3}')
  tag="${version%%-*}"
  short="${version##*-}"
  latest=$(curl -4fsSL --connect-timeout 10 https://api.github.com/repos/gearnode/privatebin/releases/latest 2>/dev/null | grep '"tag_name"' | awk -F '"' '{print $4}' || true)
  if [[ -z "$latest" ]]; then return 0; fi
  if [[ "$tag" != "$latest" ]]; then
    log "$(color yellow "privatebin tag ($tag) is not the latest ($latest). Consider updating.")"
    return 0
  fi
  ref_url="https://api.github.com/repos/gearnode/privatebin/git/refs/tags/${latest}"
  sha=$(curl -4fsSL --connect-timeout 10 "$ref_url" 2>/dev/null | grep '"sha"' | head -n1 | awk -F '"' '{print $4}' || true)
  if [[ -z "$sha" ]]; then return 0; fi
  gh_short="${sha:0:7}"
  if [[ "$gh_short" != "$short" ]]; then
    log "$(color yellow "privatebin commit ($short) differs from tag commit ($gh_short) for $latest.")"
  else
    log "privatebin OK: $latest-$gh_short"
  fi
}

# ---------------------- UX helpers ----------------------
clear_screen() { command -v clear >/dev/null && clear || printf "\n\n"; }

choose_container() {
  local name="" all rc
  if ! have docker; then die "Docker not found. Please install Docker and ensure the daemon is running."; fi
  if ! docker info >/dev/null 2>&1; then die "Docker daemon not running or not accessible for current user."; fi

  # Keep prompting until we get a valid container or the user cancels
  while true; do
    mapfile -t all < <(docker ps -a --format '{{.Names}}' | sort -u)
    if [[ ${#all[@]} -eq 0 ]]; then die "No containers found on this host."; fi

    if [[ -n "$GUM_BIN" ]]; then
      name=""
      if ! name=$(gum_run choose --limit 1 --height 15 --header "Select your container" -- "${all[@]}"); then
        rc=$?
        if (( rc == 130 )); then
          log "Cancelled by user (Ctrl+C). Exiting."
          exit 130
        else
          log "Selection cancelled. Exiting."
          exit 1
        fi
      fi
    else
      echo "Available containers:"; printf " - %s\n" "${all[@]}"
      if ! read -r -p "Enter container name (leave blank to cancel): " name </dev/tty; then
        log "Cancelled by user. Exiting."
        exit 130
      fi
      if [[ -z "$name" ]]; then
        log "No selection made. Exiting."
        exit 1
      fi
    fi

    if docker ps -a --format '{{.Names}}' | grep -Fxq "$name"; then
      printf "%s" "$name"
      return 0
    fi
    log "Container '$name' not found. Let's try again."
  done
}

confirm() {
  local prompt="$1"; local ok=""
  if [[ -n "$GUM_BIN" ]]; then gum_run confirm "$prompt" && return 0 || return 1; fi
  read -r -p "$prompt [y/N]: " ok; [[ "${ok,,}" == y* ]]
}

multiline_input() {
  local prompt="$1"; local min_len=${2:-0}; local text=""
  if [[ -n "$GUM_BIN" ]]; then
    # Show multi-line guidance above the input; placeholder cannot render newlines
    ui_out "$prompt"; ui_out ""
    if ! text=$(gum_run write --width 80 --height 12 --placeholder "Type here... (Ctrl+D to submit; Ctrl+E to open editor)"); then
      local rc=$?
      if (( rc == 130 )); then
        ui_out "$(color yellow "Cancelled by user (Ctrl+C). Exiting.")"
      else
        ui_out "$(color yellow "Input cancelled (exit $rc). Exiting.")"
      fi
      exit $rc
    fi
  else
    ui_out "$prompt"
    ui_out "End input with a single '.' on its own line:"
    local line
    # Read from the real terminal to support curl | bash
    while IFS= read -r line; do [[ "$line" == "." ]] && break; text+="${line}"$'\n'; done </dev/tty
  fi
  local len=${#text}
  if (( len < min_len )); then
    ui_out "$(color yellow "Please provide at least $min_len characters (you entered $len).")"
    multiline_input "$prompt" "$min_len"; return
  fi
  printf "%s" "$text"
}

# ---------------------- upload helpers ----------------------
privatebin_upload_file() { # privatebin_upload_file <file> -> URL (printed)
  local f="$1"; [[ -s "$f" ]] || return 1
  [[ -z "$PVBIN_BIN" ]] && return 1
  local out="$TMP_DIR/$(basename "$f").up"
  # Prefer configured bin with explicit formatter and expiry
  if ! cat "$f" | "$PVBIN_BIN" --config "$PRIVATEBIN_CFG" create --expire 1year --formatter plaintext >"$out" 2>/dev/null; then
    return 1
  fi
  grep -Eo 'https?://[^ ]+' "$out" | tail -n1
}

# ---------------------- main flow ----------------------
main() {
  clear_screen
  log "Welcome! This will help you craft a complete Hotio support request."
  ensure_connectivity
  spinner_run "Preparing interactive tools (gum)" -- bash -c 'true'; ensure_gum || true
  spinner_run "Preparing uploader (privatebin)" -- bash -c 'true'; ensure_privatebin || true
  verify_privatebin_version || true

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

  spinner_run "Collecting docker logs" -- bash -c "docker logs --timestamps --since=24h '$container' > '$logs_file' 2>&1 || true"
  spinner_run "Generating compose via docker-autocompose" -- bash -c "docker run --rm -v /var/run/docker.sock:/var/run/docker.sock:ro ghcr.io/red5d/docker-autocompose '$container' > '$comp_file' 2>/dev/null || true"

  # Step 3: Problem description
  log "Step 3/3: Describe the problem"
  local desc
  desc="$(multiline_input "Please describe your issue in detail.\nControls: Press Ctrl+D to submit, Ctrl+E to edit. Minimum 50 characters required.\n\nSuggested prompts:\n- What were you trying to do?\n- What did you expect?\n- What actually happened?\n- When did it start?\n- What have you already tried?" 50)"

  # Consent to upload
  echo
  log "Review:"
  echo " - Logs file: $logs_file ($(wc -c <"$logs_file" 2>/dev/null || echo 0) bytes)"
  echo " - Compose file: $comp_file ($(wc -c <"$comp_file" 2>/dev/null || echo 0) bytes)"
  echo
  local do_upload=false
  if confirm "Upload logs and compose to logs.notifiarr.com (1-year expiration)?"; then do_upload=true; fi

  # PrivateBin config (auto)
  echo '{"bin":[{"name":"","host":"https://logs.notifiarr.com","expire":"1year"}]}' > "$PRIVATEBIN_CFG"

  local logs_url="" comp_url=""
  if $do_upload && [[ -n "$PVBIN_BIN" ]]; then
    if [[ -s "$logs_file" ]]; then
      spinner_run "Uploading logs to PrivateBin" -- bash -c "$PVBIN_BIN --config \"$PRIVATEBIN_CFG\" create --expire 1year --formatter plaintext -o json < \"$logs_file\" > \"$TMP_DIR/logs.up\" 2>/dev/null || true"
      logs_url="$(grep -Eo 'https?://[^"]+' "$TMP_DIR/logs.up" | tail -n1 || true)"
    fi
    if [[ -s "$comp_file" ]]; then
      spinner_run "Uploading compose to PrivateBin" -- bash -c "$PVBIN_BIN --config \"$PRIVATEBIN_CFG\" create --expire 1year --formatter plaintext -o json < \"$comp_file\" > \"$TMP_DIR/compose.up\" 2>/dev/null || true"
      comp_url="$(grep -Eo 'https?://[^"]+' "$TMP_DIR/compose.up" | tail -n1 || true)"
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

