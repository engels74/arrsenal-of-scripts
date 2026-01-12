# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

A collection of bash and Python scripts for server automation, Docker container management, and the Servarr app suite (Sonarr, Radarr, etc.). Scripts are designed to run in Docker containers (hotio images) or on bare-metal servers.

## Project Structure

- `arr-scripts/` - Sonarr/Radarr custom scripts triggered by import events (check Danish audio tracks via ffprobe)
- `miscellaneous/hotio/` - Docker migration helper and support-request generator for hotio containers
- `server-scripts/backup/` - Server backup solutions (shell scripts for tar/7z, Python backup with rclone sync)
- `server-scripts/fail2ban/` - Fail2ban monitoring utilities
- `server-scripts/rclone/` - Rclone sync automation
- `game-servers-script/` - Game server utilities (Valheim mod checker)

## Running Tests

```bash
# Python backup script tests
python server-scripts/backup/python/test_backup_script.py
```

## Script Conventions

### Bash Scripts
- Use `#!/usr/bin/env bash` shebang
- Use `set -euo pipefail` for strict error handling
- Log functions with timestamps: `log_debug()`, `log_info()`, `die()`
- Support `--dry-run` flag where applicable
- Use `gum` for interactive TUI elements in hotio scripts

### Python Scripts
- Python 3.13+ required (uses inline script metadata with `# /// script`)
- Dependencies declared in script header for use with `uv run`
- Uses TypedDict for structured type definitions
- Supports `--dry-run` / `-d` flag

## Key Technical Patterns

### Arr Scripts (Sonarr/Radarr)
- Triggered as custom scripts on import/upgrade events
- Use environment variables from Sonarr/Radarr (e.g., `radarr_eventtype`, `radarr_moviefile_path`)
- Default ffprobe path: `/app/bin/ffprobe` (hotio Docker images)
- API interactions use curl with `X-Api-Key` header

### Hotio Support Script
- Downloads dependencies (gum, privatebin) to temp directory, cleans up on exit
- Uses `gum_run()` wrapper for reliable TUI in piped environments (`curl | bash`)
- IPv4-only network calls (`curl -4`)
- Uploads to logs.notifiarr.com via privatebin CLI

### Hotio Migrate Script
- Requires Mike Farah's `yq` (Go-based, not Python wrapper)
- Modifies Docker Compose YAML files for VPN/DNS migration
- Creates `.bak` backups before changes

### Python Backup Script
- Creates encrypted backups: tar -> pigz/gzip -> openssl AES-256-CBC
- Password read from file (`/root/.backup_password`)
- Integrates with: Discord webhooks, PrivateBin, Uptime Kuma maintenance windows
- Manages Docker services via compose files (priority restart for Plex)
- Uses rclone for off-site sync with JSON log parsing

## License

AGPLv3
