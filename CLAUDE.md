# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

## Repository shape

- This is a collection of independent, directly deployed scripts, not one application. Preserve
  each script's interpreter, embedded configuration, dependencies, and operating assumptions;
  there is no shared build or dependency manifest.
- Most shell scripts are intentionally configured by editing constants near the top of that
  script. Do not introduce a shared configuration layer unless the task explicitly spans scripts.
- `server-scripts/backup/python/overengineered-backup-script.py` is the exception: runtime
  configuration is TOML loaded from `/etc/backup-script.toml` by default, with secrets overridden
  by `BACKUP_UPTIME_KUMA_PASSWORD` and `BACKUP_DISCORD_WEBHOOK_URL`.

## Verification

- Bash syntax: `find arr-scripts game-servers-script miscellaneous server-scripts -type f -name '*.sh' -print0 | xargs -0 -n1 bash -n`
- Zsh syntax: `zsh -n server-scripts/fail2ban/fail2ban-monitor.zsh`
- Backup suite: `uv run server-scripts/backup/python/test_backup_script.py`
- One backup test: `uv run server-scripts/backup/python/test_backup_script.py TestConfigLoading.test_values_override_defaults`
- Claude diagnostic self-test: `python3 miscellaneous/claude/claude-diag.py --self-test`
- There is no repository-wide lint, typecheck, build, or test command.

## Expensive-to-guess constraints

- The backup Python files require Python 3.14 through their PEP 723 metadata. Keep the metadata
  and `uv run` execution path when changing dependencies.
- Backup archives are an `age`-encrypted `tar | pigz/gzip | age` stream. Preserve streaming and
  post-quantum identity support; use the verification skill below for changes to this pipeline.
- Backup test cases needing `age`, `age-keygen -pq`, or GNU tar skip when unavailable. Read the
  full test output before treating a green suite as integration coverage.
- The Radarr/Sonarr audio-check scripts delete imported media and mark history failed on a
  `Download` event. Do not exercise that branch against a live service; use syntax checks or
  controlled mocks.
- `miscellaneous/claude/claude-diag.py --publish` uploads the redacted report to PasteMyst as an
  anonymous public/unlisted-style paste. Use `--self-test` for local validation instead.
- `arr-scripts/README.md` names `danishAudioRadarr.sh` and `danishAudioSonarr.sh`, but the tracked
  files are `radarr/radarr-check-for-danish-audio.sh` and
  `sonarr/sonarr-check-for-danish-audio.sh`. Use the tracked paths.

## Skills

- `.claude/skills/verify-backup-script/SKILL.md` — targeted and full validation for the Python
  backup pipeline. Read before changing its configuration schema, archive pipeline, restore
  behavior, locking, retention, Docker lifecycle, or integrations.
