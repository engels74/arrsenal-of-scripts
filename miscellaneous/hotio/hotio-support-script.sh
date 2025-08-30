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
# - Hard dependency on gum; exit immediately if gum is not available or fails

set -euo pipefail
IFS=$'\n\t'

# ---------------------- globals ----------------------
SCRIPT_NAME="hotio-support-script"
TMP_DIR="$(mktemp -d -t ${SCRIPT_NAME}.XXXXXX)"
CLEANUP_CMDS=()
OS="$(uname -s)"
ARCH_RAW="$(uname -m)"

GUM_BIN=""      # path to gum if available
PVBIN_BIN=""    # path to privatebin if available
PRIVATEBIN_CFG="/tmp/privatebin-config.json"
DISCORD_CHANNEL_URL="https://discord.gg/hotio"

# ---------------------- cleanup ----------------------
cleanup() {
  local code=$?
  cleanup_terminal
  for cmd in "${CLEANUP_CMDS[@]:-}"; do eval "$cmd" || true; done
  rm -rf "$TMP_DIR" 2>/dev/null || true
  exit $code
}
trap cleanup EXIT TERM

# ---------------------- utils ----------------------
# Pure gum-based styling functions
gum_style_color() { 
  local color="$1"; local text="$2"
  if [[ -n "$GUM_BIN" ]]; then
    case "$color" in
      red) gum_run style --foreground 196 "$text";;
      green) gum_run style --foreground 46 "$text";;
      yellow) gum_run style --foreground 226 "$text";;
      blue) gum_run style --foreground 33 "$text";;
      magenta) gum_run style --foreground 201 "$text";;
      cyan) gum_run style --foreground 51 "$text";;
      *) gum_run style "$text";;
    esac
  else
    # Fallback when gum is not available yet (during bootstrap)
    printf "%s\n" "$text"
  fi
}

log() { 
  if [[ -n "$GUM_BIN" ]]; then
    local prefix
    prefix=$(gum_run style --foreground 51 "[${SCRIPT_NAME}]")
    gum_run style "$prefix $*"
  else
    # Fallback when gum is not available yet (during bootstrap)
    printf "[%s] %s\n" "$SCRIPT_NAME" "$*"
  fi
}

die() { 
  if [[ -n "$GUM_BIN" ]]; then
    local prefix
    prefix=$(gum_run style --foreground 196 "[ERROR]")
    gum_run style "$prefix $*" >&2
  else
    # Fallback when gum is not available yet (during bootstrap)
    printf "[ERROR] %s\n" "$*" >&2
  fi
  exit 1
}

have() { command -v "$1" >/dev/null 2>&1; }


# Reset terminal to clean state - gum-compatible version
reset_terminal_state() {
  # Gum handles terminal state internally, minimal cleanup needed
  if is_interactive_terminal; then
    # Just consume any pending terminal input
    read -t 0.1 -N 100 </dev/tty 2>/dev/null || true
  fi
}

# Check if we're in a proper interactive terminal environment
is_interactive_terminal() {
  [[ -t 0 && -t 1 && -t 2 ]] && [[ -e /dev/tty ]] 2>/dev/null
}

# Enhanced terminal cleanup - gum-compatible version  
cleanup_terminal() {
  # Gum-compatible cleanup without escape sequences
  if is_interactive_terminal; then
    # Just flush any pending terminal input/responses
    read -t 0.1 -N 1000 </dev/tty 2>/dev/null || true
  fi
}


# Enhanced gum wrapper for reliable operation in all environments (interactive and piped)
# Optimized for curl | bash scenarios while maintaining full functionality
gum_run() {
  if [[ -n "$GUM_BIN" ]]; then
    # Store original environment
    local orig_term="${TERM:-}"
    local orig_colorterm="${COLORTERM:-}"
    local orig_no_color="${NO_COLOR:-}"
    
    if [[ -e /dev/tty ]] 2>/dev/null; then
      if is_interactive_terminal; then
        # Full interactive mode with optimal gum environment
        export GUM_INPUT_CURSOR_FOREGROUND="212"
        export GUM_INPUT_PROMPT_FOREGROUND="240"
        "$GUM_BIN" "$@" </dev/tty
      else
        # Piped environment - configure for reliable output without capability queries
        export TERM="${TERM:-xterm-256color}"
        export COLORTERM="truecolor"
        export NO_COLOR=""
        export GUM_INPUT_CURSOR_FOREGROUND="212"
        export GUM_INPUT_PROMPT_FOREGROUND="240"
        # Execute gum reading from the real terminal to avoid consuming script stdin
        "$GUM_BIN" "$@" </dev/tty 2>/dev/null
      fi
    else
      # No TTY - minimal configuration for headless environments
      export TERM="xterm-256color"
      export NO_COLOR=""
      "$GUM_BIN" "$@" 2>/dev/null
    fi
    
    # Restore original environment
    if [[ -n "$orig_term" ]]; then
      export TERM="$orig_term"
    else
      unset TERM 2>/dev/null || true
    fi
    if [[ -n "$orig_colorterm" ]]; then
      export COLORTERM="$orig_colorterm"
    else
      unset COLORTERM 2>/dev/null || true
    fi
    if [[ -n "$orig_no_color" ]]; then
      export NO_COLOR="$orig_no_color"
    else
      unset NO_COLOR 2>/dev/null || true
    fi
  else
    return 1
  fi
}


spinner_run() { # spinner_run "msg" -- command args...
  local msg="$1"; shift
  if [[ "${1:-}" == "--" ]]; then shift; fi
  gum_run spin --spinner line --title "$msg" -- "$@" || die "gum failed during: $msg"
}

# Safe file size helper: prints 0 if file is missing, otherwise bytes
file_size_bytes() {
  local f="$1"
  if [[ -e "$f" ]]; then
    # Prefer stat for correctness; fall back to wc
    if [[ "$OS" == "Darwin" ]]; then
      stat -f%z -- "$f" 2>/dev/null || wc -c <"$f" 2>/dev/null || echo 0
    else
      stat -c%s -- "$f" 2>/dev/null || wc -c <"$f" 2>/dev/null || echo 0
    fi
  else
    echo 0
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

  # Attempt to download gum to a temporary directory (no persistent install)
  local os arch; os=$(gum_map_os); arch=$(gum_map_arch)
  local pattern="gum_.*_${os}_${arch}\.tar\.gz"
  local arc="$TMP_DIR/gum.tgz"; local ext="$TMP_DIR/gum"
  log "gum not found; attempting to fetch gum for ${os}/${arch} from GitHub releases..."
  if download_gh_asset_latest charmbracelet gum "$pattern" "$arc"; then
    if extract_if_archive "$arc" "$ext"; then
      # find gum binary in extracted contents
      local cand
      cand=$(find "$ext" -type f -name gum -print -quit 2>/dev/null || true)
      if [[ -n "$cand" ]]; then
        chmod +x "$cand" 2>/dev/null || true
        if [[ -x "$cand" ]]; then GUM_BIN="$cand"; log "gum ready: $GUM_BIN"; return 0; fi
      fi
      log "Downloaded gum archive but could not locate executable inside (pattern: $pattern)."
    else
      # maybe it's a raw binary
      chmod +x "$arc" 2>/dev/null || true
      if file "$arc" | grep -qiE 'executable|Mach-O|ELF'; then GUM_BIN="$arc"; log "gum ready: $GUM_BIN"; return 0; fi
      log "Failed to extract gum archive. 'tar' may be missing or the archive format changed."
    fi
  else
    log "Failed to download gum from GitHub releases (pattern: $pattern)."
  fi

  # Fail fast with guidance
  die "gum could not be prepared.\n - OS/arch detected: ${os}/${arch}\n - If you already have gum installed, re-run with GUM_BIN=/path/to/gum before the command.\n - Or install gum from: https://github.com/charmbracelet/gum/releases"
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
    log "$(gum_style_color yellow "privatebin tag ($tag) is not the latest ($latest). Consider updating.")"
    return 0
  fi
  ref_url="https://api.github.com/repos/gearnode/privatebin/git/refs/tags/${latest}"
  sha=$(curl -4fsSL --connect-timeout 10 "$ref_url" 2>/dev/null | grep '"sha"' | head -n1 | awk -F '"' '{print $4}' || true)
  if [[ -z "$sha" ]]; then return 0; fi
  gh_short="${sha:0:7}"
  if [[ "$gh_short" != "$short" ]]; then
    log "$(gum_style_color yellow "privatebin commit ($short) differs from tag commit ($gh_short) for $latest.")"
  else
    log "privatebin OK: $latest-$gh_short"
  fi
}

# ---------------------- UX helpers ----------------------
clear_screen() { 
  # Clean terminal state first
  cleanup_terminal
  
  # Clear screen appropriately for environment
  if is_interactive_terminal && command -v clear >/dev/null 2>&1; then
    clear 2>/dev/null || true
  else
    # Fallback for non-interactive/piped environments - minimal output
    printf "\n" 2>/dev/null || true
  fi
  
  # Ensure clean state after clearing
  cleanup_terminal
}

choose_container() {
  local name="" all rc
  if ! have docker; then die "Docker not found. Please install Docker and ensure the daemon is running."; fi
  if ! docker info >/dev/null 2>&1; then die "Docker daemon not running or not accessible for current user."; fi

  # Keep prompting until we get a valid container or the user cancels
  while true; do
    mapfile -t all < <(docker ps -a --format '{{.Names}}' | sort -u)
    if [[ ${#all[@]} -eq 0 ]]; then die "No containers found on this host."; fi

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

    if docker ps -a --format '{{.Names}}' | grep -Fxq "$name"; then
      printf "%s" "$name"
      return 0
    fi
    log "Container '$name' not found. Let's try again."
  done
}

confirm() {
  local prompt="$1"
  gum_run confirm "$prompt"
}

multiline_input() {
  local prompt="$1"; local min_len=${2:-0}; local text=""
  # Show multi-line guidance above the input; placeholder cannot render newlines
  gum_run style "$prompt"
  echo
  if ! text=$(gum_run write --width 80 --height 12 --placeholder "Type here... (Ctrl+D to submit; Ctrl+E to open editor)"); then
    local rc=$?
    if (( rc == 130 )); then
      gum_style_color yellow "Cancelled by user (Ctrl+C). Exiting."
    else
      gum_style_color yellow "Input cancelled (exit $rc). Exiting."
    fi
    exit $rc
  fi
  local len=${#text}
  if (( len < min_len )); then
    gum_style_color yellow "Please provide at least $min_len characters (you entered $len)."
    multiline_input "$prompt" "$min_len"; return
  fi
  printf "%s" "$text"
}

# Single-line input helper with fallback and min length
input_single() { # input_single "Prompt" [default] [min_len]
  local prompt="$1"; local def="${2:-}"; local min_len=${3:-0}; local ans=""
  gum_run style "$prompt"
  ans=$(gum_run input --placeholder "$def" --value "$def") || { gum_style_color yellow "Input cancelled. Exiting."; exit 1; }
  [[ -z "$ans" ]] && ans="$def"
  local len=${#ans}
  if (( len < min_len )); then
    gum_style_color yellow "Please provide at least $min_len characters (you entered $len)."
    input_single "$prompt" "$def" "$min_len"; return
  fi
  printf "%s" "$ans"
}

# Choose-one helper with gum or numbered fallback
choose_one() { # choose_one "Prompt" option1 option2 ...
  local prompt="$1"; shift; local options=("$@")
  if (( ${#options[@]} == 0 )); then return 1; fi
  gum_run style "$prompt"
  gum_run choose --limit 1 --height 10 -- "${options[@]}"
}

# Welcome Screen #1: Pre-execution
show_pre_execution_welcome() {
  clear_screen
  local repo_url="https://github.com/engels74/arrsenal-of-scripts/blob/main/miscellaneous/hotio/hotio-support-script.sh"
  gum_run style \
    --border double --margin "1 2" --padding "1 3" \
    --foreground "201" --border-foreground "201" \
    "Hotio Support Helper — Guided Collection & Safe Uploads" \
    "" \
    "What this will do:" \
    " - Help you choose a Docker container" \
    " - Collect all available container logs and a docker-autocompose snapshot" \
    " - Automatically upload them to logs.notifiarr.com (expires in 1 year)" \
    "" \
    "Downloads (temporary, removed on exit):" \
    " - gum (for nicer prompts) — fetched only if not found" \
    " - privatebin CLI — fetched only if not found" \
    " - All stored under: $TMP_DIR" \
    "" \
    "Security: Review the source here: $repo_url" \
    "No persistent installs. Temporary files are cleaned up automatically."
  echo
  if ! confirm "Continue?"; then
    log "Aborted by user before any network/download operations."
    exit 0
  fi
}

# Welcome Screen #2: Main menu welcome (after dependencies ready)
show_main_menu_welcome() {
  clear_screen
  
  # Output welcome message using gum
  gum_run style \
    --border double --margin "1 2" --padding "1 3" \
    --foreground "212" --background "236" \
    "Hotio Support Helper" \
    "" \
    "Create a complete, Discord-ready support post in minutes." \
    "We'll gather logs and an auto-compose snapshot and automatically" \
    "upload them securely to logs.notifiarr.com."

  local sel
  sel=$(gum_run choose --limit 1 --height 3 --header "Start now?" -- "Begin" "Exit") || { log "Cancelled."; exit 1; }
  if [[ "$sel" == "Exit" ]]; then
    log "Goodbye!"
    exit 0
  fi
}



# Step 3 overview screen shown before collecting inputs
show_step3_overview() {
  clear_screen
  
  # Safe display of step 3 header
  gum_run style \
    --border double --margin "1 2" --padding "1 3" \
    --foreground "117" --border-foreground "117" \
    --bold \
    "✨ Step 3: Create Your Support Post"
  
  echo
  
  # Safe display of collection info
  gum_run style \
    --border rounded --margin "0 2" --padding "1 2" \
    --foreground "150" --border-foreground "150" \
    "📋 What we'll collect:" \
    "" \
    "• Title (one line)" \
    "• Problem Details (what happened vs expected)" \
    "• Optional Error Snippet (auto-formatted as code)" \
    "" \
    "🔧 Auto-generated for you:" \
    "• Environment details (image, OS/Arch)" \
    "• Links to uploaded logs and compose files" \
    "• Container name prefix"
  
  echo
  gum_run confirm "Ready to create your post?" || { log "Cancelled."; exit 1; }
}


# ---------------------- upload helpers ----------------------
privatebin_upload_file() { # privatebin_upload_file <file> -> URL (printed)
  local f="$1"; [[ -s "$f" ]] || return 1
  [[ -z "$PVBIN_BIN" ]] && return 1
  local out
  out="$TMP_DIR/$(basename "$f").up"
  # Prefer configured bin with explicit formatter and expiry
  if ! cat "$f" | "$PVBIN_BIN" --config "$PRIVATEBIN_CFG" create --expire 1year --formatter plaintext >"$out" 2>/dev/null; then
    return 1
  fi
  grep -Eo 'https?://[^ ]+' "$out" | tail -n1
}

# ---------------------- main flow ----------------------
main() {
  clear_screen
  ensure_gum
  show_pre_execution_welcome
  log "Welcome! This will help you craft a complete Hotio support request."
  ensure_connectivity
  spinner_run "Preparing uploader (privatebin)" -- bash -c 'true'; ensure_privatebin || true
  verify_privatebin_version || true
  show_main_menu_welcome
  cleanup_terminal

  # Dry-run option for quick local testing (skips network and uploads)
  if [[ "${1:-}" == "--dry-run" ]]; then
    log "Dry run: skipping container selection and uploads."
    exit 0
  fi

  # Step 1: Container
  echo
  gum_run style \
    --border rounded --margin "1" --padding "0 2" \
    --foreground "117" --border-foreground "117" \
    --bold \
    "🐳 Step 1/3: Select Docker Container"
  echo
  local container; container="$(choose_container)"

  # Step 2: Collect logs and compose
  cleanup_terminal
  echo
  gum_run style \
    --border rounded --margin "1" --padding "0 2" \
    --foreground "150" --border-foreground "150" \
    --bold \
    "📦 Step 2/3: Collecting Data" \
    "" \
    "• Gathering container logs" \
    "• Generating compose snapshot" \
    "• Auto-uploading to logs.notifiarr.com"
  echo
  local logs_file="$TMP_DIR/${container}_logs.txt"
  local comp_file="$TMP_DIR/${container}_compose.yaml"

  spinner_run "Collecting docker logs" -- bash -c "docker logs --timestamps '$container' > '$logs_file' 2>&1 || true"
  spinner_run "Generating compose via docker-autocompose" -- bash -c "docker run --rm -v /var/run/docker.sock:/var/run/docker.sock:ro ghcr.io/red5d/docker-autocompose '$container' > '$comp_file' 2>/dev/null || true"

  # Step 3: Problem description (interactive)
  cleanup_terminal
  echo
  gum_run style \
    --border rounded --margin "1" --padding "0 2" \
    --foreground "216" --border-foreground "216" \
    --bold \
    "✍️  Step 3/3: Describe Your Problem"
  show_step3_overview
  local q_title q_details q_error image_tag
  
  # Enhanced styled title prompt
  gum_run style \
    --border rounded --margin "1" --padding "1 2" \
    --foreground "117" --border-foreground "117" \
    --bold \
    "📝 Title Guidelines" \
    "" \
    "• Keep it concise (one line)" \
    "• We'll automatically prepend the container name" \
    "• Focus on the main issue or question"
  q_title="$(input_single "Enter your title:" "" 10)"
  
  # Enhanced styled problem details prompt
  gum_run style \
    --border rounded --margin "1" --padding "1 2" \
    --foreground "150" --border-foreground "150" \
    --bold \
    "🔍 Problem Details Guidelines" \
    "" \
    "What to include:" \
    "• What you did, what you expected, what actually happened" \
    "• Short, relevant facts (versions, settings) if needed" \
    "• Key context and recent changes" \
    "" \
    "What NOT to include:" \
    "• Entire logs (we upload them for you)" \
    "• Secrets or tokens"
  q_details="$(multiline_input "Describe your problem in detail:" 10)"
  
  # Enhanced styled error snippet prompt
  gum_run style \
    --border rounded --margin "1" --padding "1 2" \
    --foreground "216" --border-foreground "216" \
    --bold \
    "⚠️  Error Snippet Guidelines" \
    "" \
    "• Paste only the most relevant error lines (5-20 lines)" \
    "• We'll automatically format them as code blocks" \
    "• Skip this if no specific errors to highlight"
  q_error="$(multiline_input "Optional - paste relevant error lines:" 0)"
  image_tag="$(docker inspect -f '{{.Config.Image}}' "$container" 2>/dev/null || true)"

  # Uploads will proceed automatically.
  echo
  gum_run style \
    --border rounded --margin "1" --padding "1 2" \
    --foreground "117" --border-foreground "117" \
    --bold \
    "📊 Review Collected Data" \
    "" \
    "• Logs file: $(file_size_bytes "$logs_file") bytes" \
    "• Compose file: $(file_size_bytes "$comp_file") bytes" \
    "• Ready for secure upload to logs.notifiarr.com"
  echo

  # PrivateBin config (auto)
  echo '{"bin":[{"name":"","host":"https://logs.notifiarr.com","expire":"1year"}]}' > "$PRIVATEBIN_CFG"

  local logs_url="" comp_url=""
  if [[ -n "$PVBIN_BIN" ]]; then
    if [[ -s "$logs_file" ]]; then
      spinner_run "Uploading logs to PrivateBin" -- bash -c "$PVBIN_BIN --config \"$PRIVATEBIN_CFG\" create --expire 1year --formatter plaintext -o json < \"$logs_file\" > \"$TMP_DIR/logs.up\" 2>/dev/null || true"
      logs_url="$(grep -Eo 'https?://[^"]+' "$TMP_DIR/logs.up" | tail -n1 || true)"
    fi
    if [[ -s "$comp_file" ]]; then
      spinner_run "Uploading compose to PrivateBin" -- bash -c "$PVBIN_BIN --config \"$PRIVATEBIN_CFG\" create --expire 1year --formatter plaintext -o json < \"$comp_file\" > \"$TMP_DIR/compose.up\" 2>/dev/null || true"
      comp_url="$(grep -Eo 'https?://[^"]+' "$TMP_DIR/compose.up" | tail -n1 || true)"
    fi
  else
    log "Skipping upload: privatebin CLI unavailable."
  fi

  # Final output
  cleanup_terminal
  echo
  gum_run style \
    --border double --margin "1" --padding "1 3" \
    --foreground "117" --border-foreground "117" \
    --bold \
    "🎉 Your Discord-Ready Support Post" \
    "" \
    "Copy everything between the scissor lines below:"
  echo
  gum_run style \
    --foreground "150" --bold \
    "---------------- Copy from here ----------------"
  # Generate the final support post content using gum
  local logs_line comp_line error_section=""
  
  if [[ -n "$logs_url" ]]; then
    logs_line=" - Logs: $logs_url"
  else
    logs_line=" - Logs: (upload unavailable/failed) -> attach '$logs_file' or upload to https://logs.notifiarr.com"
  fi
  
  if [[ -n "$comp_url" ]]; then
    comp_line=" - Compose (auto): $comp_url"
  else
    comp_line=" - Compose: (upload unavailable/failed) -> attach '$comp_file' or upload to https://logs.notifiarr.com"
  fi
  
  if [[ -n "$q_error" ]]; then
    error_section="Error Snippet:"$'\n''```'$'\n'"$q_error"$'\n''```'$'\n'
  fi
  
  gum_run format -- \
    "# [${container}] ${q_title}" \
    "" \
    "**Environment:**" \
    " - Image: ${image_tag:-unknown}" \
    " - OS/Arch: ${OS}/${ARCH_RAW}" \
    "" \
    "**Links:**" \
    "$logs_line" \
    "$comp_line" \
    "" \
    "**Problem Details:**" \
    "$q_details" \
    "" \
    "$error_section"
  gum_run style \
    --foreground "150" --bold \
    "---------------- Copy to here ----------------"

  # Clipboard (optional) - generate plain text version
  local clipboard_content error_clip=""
  
  if [[ -n "$q_error" ]]; then
    error_clip="Error Snippet:"$'\n''```'$'\n'"$q_error"$'\n''```'$'\n'
  fi
  
  clipboard_content="[${container}] ${q_title}"$'\n\n'"Environment:"$'\n'" - Image: ${image_tag:-unknown}"$'\n'" - OS/Arch: ${OS}/${ARCH_RAW}"$'\n\n'"Links:"$'\n'"$logs_line"$'\n'"$comp_line"$'\n\n'"Problem Details:"$'\n'"$q_details"$'\n\n'"$error_clip"
  
  if have pbcopy; then
    printf "%s" "$clipboard_content" | pbcopy
    log "Copied to clipboard (pbcopy)."
  elif have xclip; then
    printf "%s" "$clipboard_content" | xclip -selection clipboard
    log "Copied to clipboard (xclip)."
  fi

  # Add spacing before final messages  
  gum_run style ""
  log "Post this in the hotio Discord: $DISCORD_CHANNEL_URL"
  log "All temporary files will be removed on exit."
}

main "$@"

