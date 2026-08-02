# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Shape

A collection of **independent, self-contained sysadmin scripts** — not a package. There is no
build system, no dependency manifest at the root, no CI (`.github/` does not exist), and no shared
library. Each script is distributed on its own (copied to a server, or piped from
`raw.githubusercontent.com/engels74/arrsenal-of-scripts/refs/heads/main/<path>`).

Consequence: **never factor shared code into a common module.** A script must keep working when
that single file is downloaded in isolation. The only cross-file dependency in the repo is
`test_backup_script.py`, which loads the backup script by path via `importlib.util`.

Actively maintained (recent commits, has tests/self-tests):

| Path | Notes |
| --- | --- |
| `server-scripts/backup/python/overengineered-backup-script.py` | Flagship. 3.2k lines, 63 unit tests, type-checked. |
| `miscellaneous/claude/claude-diag.py` | Redacting diagnostic reporter, stdlib-only, has `--self-test`. |
| `miscellaneous/hotio/hotio-support-script.sh` | `set -euo pipefail`, hard dependency on `gum`, no persistent installs. |
| `server-scripts/rclone/rclone-sync-script.sh` | Top-of-file "Configuration Section" constants. |

Everything else (`arr-scripts/`, `game-servers-script/`, `server-scripts/fail2ban/`,
`server-scripts/backup/{7z,tar}-server-backup-script.sh`, `miscellaneous/dashboard-icons-script/`,
`miscellaneous/hotio/hotio-migrate-script.sh`) has been untouched since April 2025. Treat these as
stable/dormant: fix what is asked, do not modernize them opportunistically. The two shell backup
scripts are older standalone alternatives, **not** components of the Python one.

## Essential Commands

Run from `server-scripts/backup/python/`:

```bash
uv run test_backup_script.py                                  # full suite (63 tests, stdlib unittest)
uv run test_backup_script.py TestConfigLoading                # one class
uv run test_backup_script.py TestRotation.test_keeps_newest_n # one test
uvx basedpyright overengineered-backup-script.py              # type check (kept at 0 errors)
./overengineered-backup-script.py --dry-run --verbose         # full preflight preview, no changes
./overengineered-backup-script.py --print-default-config      # emit a commented example TOML
```

Both Python files carry a PEP 723 `# /// script` header requiring Python >= 3.14 — `uv run`
resolves `requests` and `uptime-kuma-api` automatically; there is no `pyproject.toml`,
`requirements.txt`, or lockfile. Tests needing `age` or GNU `tar` skip themselves when the binary
is missing, so a green run on a machine without them is not full coverage.

Other validation:

```bash
python3 miscellaneous/claude/claude-diag.py --self-test          # must print "RESULT: OK"
bash miscellaneous/hotio/hotio-support-script.sh --dry-run
```

**Do not run `ruff format` or `ruff check` on this repo.** No ruff config is checked in and the
existing code does not conform (verified: 41 lint findings, 3 files would be reformatted). Match
the surrounding style by hand instead. `basedpyright` is the only type/lint gate with evidence of
use (`# pyright: ignore[...]` suppressions in-tree, plus commit `fe024d3`); it runs on defaults
since there is no config file.

## Backup Script Architecture

Single module, organised into banner-delimited sections (`# ----- Configuration -----`, etc.).
Mutable module-level globals set once at startup and read everywhere: `config`, `dry_run_mode`,
`backup_state`, `log`.

- **Config precedence**: `Config` dataclass defaults -> TOML file (`/etc/backup-script.toml`) ->
  environment secrets -> CLI flags. Unknown TOML sections/keys are a hard `ConfigError` (typo
  guard), so the schema must be kept complete.
- **Run flow**: `main` -> `cmd_backup` -> lock acquisition -> `_run_backup`, stepping through
  `BackupStage` (PREFLIGHT, CONTAINER_SHUTDOWN, BACKUP_CREATION, BACKUP_VERIFICATION,
  CONTAINER_RESTART_PLEX, RCLONE_SYNC, CONTAINER_RESTART_ALL, CLEANUP). `BackupState.add_error()` /
  `add_warning()` tag messages with the current stage and drive the final Discord notification and
  the exit code via `determine_final_status`.
- **Data path**: streaming `tar | pigz/gzip | age` pipeline (`run_pipeline`), never a temporary
  uncompressed archive. Restore is the same pipeline reversed via `run_restore`.
- **Subprocess discipline**: every long-running child is registered in the process tracker so
  signal handlers and the watchdog can terminate the whole pipeline.

## Common Change Workflows

**Adding or renaming a backup-script config option** — four locations, all in
`overengineered-backup-script.py`:

1. Add the field to the `Config` dataclass with its default (grouped by `# [section]` comment).
2. Map the TOML `"section" -> {"key": "attr"}` pair in `_TOML_SCHEMA`; `_apply_toml` infers the
   expected type from the default's runtime type, so the default must have the intended type.
   Path-valued lists must also be listed in `_PATH_LIST_ATTRS`.
3. Emit the key in the `default_config_toml()` f-string template, in the same section.
4. Run `uv run test_backup_script.py TestConfigLoading`. Its
   `test_default_config_toml_round_trips_to_defaults` asserts the generated config re-parses to
   `Config()` exactly — it catches a wrong value in the template but **not** an omitted key, so
   check step 3 by eye.

When *removing* a key, add a `(section, key)` entry to `_REMOVED_TOML_KEYS` with an upgrade hint;
otherwise existing deployments only get a generic "unknown key" error (see the
`encryption.legacy_password_file` entry for the pattern).

**Calling an external binary from the backup script**: resolve it first.

```python
result = run_command([resolve_command("docker"), "ps", "-q"], timeout=30)
```

Under `sudo`, `secure_path` strips Homebrew/Linuxbrew from `PATH`. `find_command()` /
`resolve_command()` fall back to the brew directories and cache the lookup; a bare `"docker"` or
`"rclone"` in an argv list will fail in production. Use `resolve_tar()` for tar — the pipeline uses
GNU-only flags, and `tar_is_gnu()` gates on that.

**Anything destructive** must sit behind the `dry_run_mode` global (26 references). New code paths
that delete, write, upload, restart containers, or open a maintenance window need the same guard.

**New pre-flight validation** goes inside `pre_flight_checks()` and reports through the local
`problem()` helper, never `raise` — in dry-run mode `problem()` accumulates findings so
`--dry-run` answers "will tonight's run work?" in one pass. Tests named
`*_is_aggregated_not_raised` enforce this.

## Conventions and Gotchas

- **Secrets never land in `Config` defaults.** `uptime_kuma_password` and `discord_webhook_url`
  default to `""` and are populated from `BACKUP_UPTIME_KUMA_PASSWORD` /
  `BACKUP_DISCORD_WEBHOOK_URL`. The non-secret defaults in `Config` are the author's real
  hostnames and paths — that is intentional, but do not extend the pattern to credentials.
- **`claude-diag.py` publishes to a public paste service** with `--publish`. Any new field added to
  the report must pass through `Redactor`, and `SELF_TEST_FIXTURE` should gain a case for any new
  secret shape; `--self-test` fails loudly on leaked substrings.
- **Test isolation**: `ScriptTestCase.setUp` resets `mod.config`, `mod.dry_run_mode`,
  `mod.backup_state`, and `mod._lock_owned`. A new module-level global that tests mutate must be
  reset there too, or tests will leak state into each other.
- **`arr-scripts/README.md` is stale**: it documents `danishAudioRadarr.sh` /
  `danishAudioSonarr.sh`, renamed long ago (commit `ce53824`) to
  `radarr-check-for-danish-audio.sh` / `sonarr-check-for-danish-audio.sh`. Trust the filenames on
  disk; fix the README only if the task touches those scripts.
- **`fail2ban-monitor.sh` and `fail2ban-monitor.zsh` are separate implementations**, not a shared
  script with two shebangs. A behavioural fix to one does not propagate; state explicitly which
  variant you changed.
- **Commit messages use Conventional Commits** with the component as scope — `fix(backup):`,
  `chore(renovate):`, `feat(backup)!:` for breaking changes, `docs:`. 34 of the last 40 commits
  follow this; older history predates it.

## Additional Documentation

- `.augment/rules/python-314-pro.md` — Read before non-trivial Python work on the two `.py` files;
  it is the Python 3.14 style reference this repo is written against (PEP 695 type syntax,
  `TypeIs`, deferred annotations). Caveat: its `pyproject.toml` snippets for ruff/basedpyright are
  aspirational — none of that config exists here, so do not add it as part of an unrelated change.
- `arr-scripts/README.md` — Read when wiring the Danish-audio scripts into Radarr/Sonarr as Custom
  Script connections; the connection-setup steps are current even though the filenames are not.
- `server-scripts/backup/python/overengineered-backup-script.py` lines 10-43 — Read before touching
  encryption or restore; the header documents the age post-quantum key setup and the exact CLI
  invocations.
