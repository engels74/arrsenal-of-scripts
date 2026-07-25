---
name: verify-backup-script
description: Verify changes to the Python backup script, including config, unit tests, and tool-gated archive integration tests.
---

# Verify the Python backup script

1. Work from the repository root. The production script and test suite require Python 3.14 via
   their PEP 723 metadata, so invoke them through `uv run`.
2. When changing configuration, keep these three surfaces aligned in
   `server-scripts/backup/python/overengineered-backup-script.py`:
   - `Config`
   - `_TOML_SCHEMA`
   - `default_config_toml()`
   Add or update `TestConfigLoading` coverage in
   `server-scripts/backup/python/test_backup_script.py`.
3. Run the narrowest affected case:
   `uv run server-scripts/backup/python/test_backup_script.py TestConfigLoading.test_values_override_defaults`
   Replace the class and method with the affected test's names.
4. Run the full suite:
   `uv run server-scripts/backup/python/test_backup_script.py`
5. Inspect the output for skips. Tests that require `age`, `age-keygen -pq`, or GNU tar skip
   themselves when those tools are unavailable; a passing suite with skips does not verify the
   encrypt/verify/restore pipeline.
6. For configuration changes, also verify that the generated example is valid:
   `uv run server-scripts/backup/python/overengineered-backup-script.py --print-default-config`
   The command must exit successfully and include the changed key in its correct TOML section.
