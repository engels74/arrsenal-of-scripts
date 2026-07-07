#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# ///
"""Tests for overengineered-backup-script.py.

Stdlib-only (unittest), so this runs with a plain `python test_backup_script.py`
(Python 3.14+) or `uv run test_backup_script.py`. Tests that need external
tools (age, GNU tar) skip themselves when the tool is unavailable.
"""

import importlib.util
import logging
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).parent / "overengineered-backup-script.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("backup_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_script()

AGE = mod.find_command("age")
AGE_KEYGEN = mod.find_command("age-keygen")


def _tar_is_gnu() -> bool:
    return mod.tar_is_gnu(mod.resolve_tar())


def setUpModule() -> None:
    logging.disable(logging.CRITICAL)


def tearDownModule() -> None:
    logging.disable(logging.NOTSET)


class ScriptTestCase(unittest.TestCase):
    """Base: isolates the module globals each test mutates."""

    def setUp(self) -> None:
        mod.config = mod.Config()
        mod.dry_run_mode = False
        mod.backup_state = mod.BackupState()
        mod._lock_owned = False
        self.tmp = Path(tempfile.mkdtemp(prefix="backup-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class TestFormatHelpers(ScriptTestCase):
    def test_format_bytes(self):
        self.assertEqual(mod.format_bytes(None), "0 B")
        self.assertEqual(mod.format_bytes(0), "0 B")
        self.assertEqual(mod.format_bytes(512), "512.00 B")
        self.assertEqual(mod.format_bytes(2048), "2.00 KB")
        self.assertEqual(mod.format_bytes(5 * 1024**3), "5.00 GB")
        self.assertEqual(mod.format_bytes(3 * 1024**4), "3.00 TB")

    def test_format_duration(self):
        self.assertEqual(mod.format_duration(0), "0h:0m:0s")
        self.assertEqual(mod.format_duration(3723), "1h:2m:3s")
        self.assertEqual(mod.format_duration(59.9), "0h:0m:59s")


class TestPathHelpers(ScriptTestCase):
    def test_existing_dir_returns_itself(self):
        self.assertEqual(mod.nearest_existing_dir(self.tmp), self.tmp)

    def test_missing_dir_returns_existing_ancestor(self):
        self.assertEqual(
            mod.nearest_existing_dir(self.tmp / "not" / "yet" / "created"), self.tmp
        )

    def test_tar_is_gnu_rejects_missing_binary(self):
        self.assertFalse(mod.tar_is_gnu("/nonexistent/definitely-not-tar"))


class TestPipelineSpawnFailure(ScriptTestCase):
    def test_failed_spawn_kills_earlier_stages(self):
        stages = [
            ("cat", ["/bin/cat"]),
            ("missing", ["/nonexistent/definitely-not-a-binary"]),
        ]
        with open(os.devnull, "rb") as devnull:
            with self.assertRaises(OSError):
                mod.run_pipeline(stages, timeout=10, stdin_first=devnull)
        # The already-started stage must be killed and untracked.
        self.assertEqual(len(mod._active_processes), 0)


class TestPreFlightDryRun(ScriptTestCase):
    def test_invalid_compression_tool_is_aggregated_not_raised(self):
        mod.dry_run_mode = True
        mod.config.compression_tool = "zstd"
        mod.pre_flight_checks()  # must not raise in dry-run mode
        self.assertTrue(mod.backup_state.errors)
        self.assertIn("Invalid compression_tool", mod.backup_state.errors[0])

    def test_dry_run_problems_are_recorded_as_errors(self):
        # On any non-root test machine at least the root check fails, so the
        # dry run must record an error (=> non-zero exit) instead of only
        # logging warnings.
        if os.geteuid() == 0:
            self.skipTest("running as root; no guaranteed pre-flight problem")
        mod.dry_run_mode = True
        mod.pre_flight_checks()
        self.assertTrue(mod.backup_state.has_critical_errors)


class TestRcloneRetryClassification(ScriptTestCase):
    def test_non_retryable_codes(self):
        for code in (1, 3, 4, 7):
            self.assertFalse(mod._is_retryable_rclone_error(code), code)

    def test_retryable_codes(self):
        for code in (2, 5, 6, 8, 9):
            self.assertTrue(mod._is_retryable_rclone_error(code), code)


class TestDetermineFinalStatus(ScriptTestCase):
    def test_failure_wins(self):
        status, color, _ = mod.determine_final_status(False, ["a warning"])
        self.assertEqual(status, "Failed")
        self.assertEqual(color, mod.COLOR_RED)

    def test_success_with_warnings_is_yellow(self):
        status, color, _ = mod.determine_final_status(True, ["a warning"])
        self.assertEqual(status, "Completed with Warnings")
        self.assertEqual(color, mod.COLOR_YELLOW)

    def test_clean_success_is_green(self):
        status, color, _ = mod.determine_final_status(True, [])
        self.assertEqual(status, "Success")
        self.assertEqual(color, mod.COLOR_GREEN)


class TestConfigLoading(ScriptTestCase):
    def _write(self, content: str) -> Path:
        path = self.tmp / "config.toml"
        path.write_text(content)
        return path

    def test_missing_default_path_uses_defaults(self):
        # Point the default path into the temp dir so the test does not pick
        # up a real /etc/backup-script.toml on machines that have one.
        original = mod.DEFAULT_CONFIG_PATH
        mod.DEFAULT_CONFIG_PATH = self.tmp / "absent.toml"
        self.addCleanup(setattr, mod, "DEFAULT_CONFIG_PATH", original)
        cfg = mod.load_config(None)
        self.assertEqual(cfg, mod.Config())

    def test_missing_explicit_path_is_error(self):
        with self.assertRaises(mod.ConfigError):
            mod.load_config(self.tmp / "nope.toml")

    def test_values_override_defaults(self):
        path = self._write(
            """
[retention]
backups = 9

[backup]
sources = ["/a", "/b"]
compression_tool = "gzip"

[docker]
enabled = false

[paths]
backup_root_dir = "/custom/backups"
"""
        )
        cfg = mod.load_config(path)
        self.assertEqual(cfg.retention_backups, 9)
        self.assertEqual(cfg.backup_sources, [Path("/a"), Path("/b")])
        self.assertEqual(cfg.compression_tool, "gzip")
        self.assertFalse(cfg.docker_enabled)
        self.assertEqual(cfg.backup_root_dir, Path("/custom/backups"))
        # Untouched settings keep their defaults.
        self.assertEqual(cfg.retention_logs, mod.Config().retention_logs)

    def test_unknown_section_rejected(self):
        path = self._write("[typo_section]\nfoo = 1\n")
        with self.assertRaises(mod.ConfigError):
            mod.load_config(path)

    def test_unknown_key_rejected(self):
        path = self._write("[retention]\nbackupz = 3\n")
        with self.assertRaises(mod.ConfigError):
            mod.load_config(path)

    def test_wrong_type_rejected(self):
        path = self._write('[retention]\nbackups = "three"\n')
        with self.assertRaises(mod.ConfigError):
            mod.load_config(path)
        path = self._write("[docker]\nenabled = 1\n")
        with self.assertRaises(mod.ConfigError):
            mod.load_config(path)

    def test_env_overrides_secrets(self):
        path = self._write('[uptime_kuma]\npassword = "from-file"\n')
        os.environ[mod.ENV_UPTIME_KUMA_PASSWORD] = "from-env"
        os.environ[mod.ENV_DISCORD_WEBHOOK_URL] = "https://discord.example/hook"
        self.addCleanup(os.environ.pop, mod.ENV_UPTIME_KUMA_PASSWORD, None)
        self.addCleanup(os.environ.pop, mod.ENV_DISCORD_WEBHOOK_URL, None)
        cfg = mod.load_config(path)
        self.assertEqual(cfg.uptime_kuma_password, "from-env")
        self.assertEqual(cfg.discord_webhook_url, "https://discord.example/hook")

    def test_default_config_toml_round_trips_to_defaults(self):
        # The generated example config must parse and reproduce the defaults
        # exactly - this keeps --print-default-config in sync with Config.
        for env in (mod.ENV_UPTIME_KUMA_PASSWORD, mod.ENV_DISCORD_WEBHOOK_URL):
            os.environ.pop(env, None)
        path = self._write(mod.default_config_toml())
        cfg = mod.load_config(path)
        self.assertEqual(cfg, mod.Config())


class TestRotation(ScriptTestCase):
    def _make_files(self, names: list[str]) -> list[Path]:
        paths = []
        for i, name in enumerate(names):
            p = self.tmp / name
            p.write_text("x")
            # Deterministic mtimes: later in the list = newer.
            ts = time.time() - (len(names) - i) * 60
            os.utime(p, (ts, ts))
            paths.append(p)
        return paths

    def test_keeps_newest_n(self):
        files = self._make_files([f"{i}_backup.tar.gz.age" for i in range(5)])
        removed = mod.rotate_items(self.tmp, "*.tar.gz.age", 2)
        self.assertEqual(sorted(p.name for p in removed), sorted(p.name for p in files[:3]))
        remaining = sorted(p.name for p in self.tmp.glob("*.age"))
        self.assertEqual(remaining, sorted(p.name for p in files[3:]))

    def test_multiple_patterns_rotate_together(self):
        old = self._make_files(["old1_backup.tar.gz.enc", "old2_backup.tar.gz.enc"])
        new = self._make_files(["new1_backup.tar.gz.age", "new2_backup.tar.gz.age"])
        # old files were created first but _make_files re-bases mtimes per
        # call; force the .enc files to be oldest explicitly.
        for p in old:
            os.utime(p, (time.time() - 10_000, time.time() - 10_000))
        removed = mod.rotate_items(self.tmp, ["*.tar.gz.age", "*.tar.gz.enc"], 2)
        self.assertEqual(sorted(p.name for p in removed), sorted(p.name for p in old))
        self.assertTrue(all(p.exists() for p in new))

    def test_dry_run_removes_nothing(self):
        files = self._make_files([f"{i}_backup.tar.gz.age" for i in range(4)])
        mod.dry_run_mode = True
        removed = mod.rotate_items(self.tmp, "*.tar.gz.age", 1)
        self.assertEqual(len(removed), 3)
        self.assertTrue(all(p.exists() for p in files))

    def test_directories_are_ignored(self):
        (self.tmp / "dir_backup.tar.gz.age").mkdir()
        removed = mod.rotate_items(self.tmp, "*.tar.gz.age", 0)
        self.assertEqual(removed, [])
        self.assertTrue((self.tmp / "dir_backup.tar.gz.age").is_dir())

    def test_orphan_manifest_cleanup(self):
        backup = self.tmp / "a_backup.tar.gz.age"
        backup.write_text("x")
        kept = self.tmp / "a_backup.tar.gz.age.sha256"
        kept.write_text("y")
        orphan = self.tmp / "gone_backup.tar.gz.age.sha256"
        orphan.write_text("z")
        mod.cleanup_orphan_manifests(self.tmp)
        self.assertTrue(kept.exists())
        self.assertFalse(orphan.exists())


class TestLocking(ScriptTestCase):
    def setUp(self):
        super().setUp()
        self.lock_path = self.tmp / "test.lock"

    def test_acquire_writes_pid_and_cleans_up(self):
        with mod.acquire_lock(self.lock_path):
            self.assertTrue(self.lock_path.exists())
            self.assertEqual(int(self.lock_path.read_text()), os.getpid())
        self.assertFalse(self.lock_path.exists())

    def test_contention_does_not_delete_winners_lock(self):
        # Simulate another *live* process holding the lock. PID 1 always
        # exists and os.kill(1, 0) raises PermissionError => "alive".
        self.lock_path.write_text("1")
        ctx = mod.acquire_lock(self.lock_path)
        with self.assertRaises(FileExistsError):
            ctx.__enter__()
        # The critical regression check: the loser must NOT have removed
        # the winner's lock file on its way out.
        self.assertTrue(self.lock_path.exists())
        self.assertEqual(self.lock_path.read_text(), "1")

    def test_stale_lock_is_taken_over(self):
        # A PID that cannot exist on any sane system.
        self.lock_path.write_text("99999999")
        with mod.acquire_lock(self.lock_path):
            self.assertEqual(int(self.lock_path.read_text()), os.getpid())
        self.assertFalse(self.lock_path.exists())

    def test_lock_without_pid_uses_age(self):
        self.lock_path.write_text("")
        # Fresh empty lock: not stale.
        self.assertFalse(mod.is_stale_lock(self.lock_path))
        # Ancient empty lock: stale.
        old = time.time() - mod.config.script_overall_timeout - 100
        os.utime(self.lock_path, (old, old))
        self.assertTrue(mod.is_stale_lock(self.lock_path))


class TestSha256Manifest(ScriptTestCase):
    def test_manifest_content(self):
        import hashlib

        backup = self.tmp / "b_backup.tar.gz.age"
        backup.write_bytes(b"some backup bytes")
        manifest = mod.write_sha256_manifest(backup)
        self.assertIsNotNone(manifest)
        digest, name = manifest.read_text().split()
        self.assertEqual(digest, hashlib.sha256(b"some backup bytes").hexdigest())
        self.assertEqual(name, backup.name)

    def test_dry_run_writes_nothing(self):
        mod.dry_run_mode = True
        backup = self.tmp / "b_backup.tar.gz.age"
        backup.write_bytes(b"x")
        self.assertIsNone(mod.write_sha256_manifest(backup))
        self.assertFalse(Path(str(backup) + ".sha256").exists())


@unittest.skipUnless(AGE and AGE_KEYGEN, "age/age-keygen not installed")
class TestRestoreRoundTrip(ScriptTestCase):
    """Round-trip through verify_backup and run_restore using an archive
    built with plain tar flags (works with both GNU tar and bsdtar)."""

    def setUp(self):
        super().setUp()
        self.identity = self.tmp / "key.txt"
        subprocess.run(
            [AGE_KEYGEN, "-o", str(self.identity)],
            check=True,
            capture_output=True,
            timeout=30,
        )
        self.identity.chmod(0o600)

        self.src = self.tmp / "src"
        (self.src / "sub").mkdir(parents=True)
        (self.src / "hello.txt").write_text("hello world\n")
        (self.src / "sub" / "nested.txt").write_text("nested content\n")

        # Build fixture archive: tar | gzip | age (no GNU-only flags).
        self.backup_file = self.tmp / "fixture_backup.tar.gz.age"
        tar = subprocess.Popen(
            [mod.resolve_tar(), "-cf", "-", "-C", str(self.tmp), "src"],
            stdout=subprocess.PIPE,
        )
        gz = subprocess.Popen(
            ["gzip", "-3"], stdin=tar.stdout, stdout=subprocess.PIPE
        )
        age = subprocess.Popen(
            [AGE, "-e", "-i", str(self.identity), "-o", str(self.backup_file)],
            stdin=gz.stdout,
        )
        tar.stdout.close()
        gz.stdout.close()
        self.assertEqual(age.wait(timeout=60), 0)
        self.assertEqual(gz.wait(timeout=10), 0)
        self.assertEqual(tar.wait(timeout=10), 0)

        mod.config.age_identity_file = self.identity
        mod.config.compression_tool = "gzip"

    def test_verify_backup_succeeds(self):
        self.assertTrue(mod.verify_backup(self.backup_file))

    def test_verify_backup_detects_corruption(self):
        corrupted = self.tmp / "corrupt_backup.tar.gz.age"
        data = bytearray(self.backup_file.read_bytes())
        data[len(data) // 2] ^= 0xFF
        corrupted.write_bytes(bytes(data))
        with self.assertRaises(mod.BackupVerificationError):
            mod.verify_backup(corrupted)

    def test_restore_list(self):
        rc = mod.run_restore(
            backup_file=self.backup_file,
            output_dir=None,
            list_only=True,
            force=False,
            identity=self.identity,
            password_file=self.tmp / "unused",
        )
        self.assertEqual(rc, 0)

    def test_restore_extract_and_compare(self):
        out = self.tmp / "restored"
        rc = mod.run_restore(
            backup_file=self.backup_file,
            output_dir=out,
            list_only=False,
            force=False,
            identity=self.identity,
            password_file=self.tmp / "unused",
        )
        self.assertEqual(rc, 0)
        self.assertEqual((out / "src" / "hello.txt").read_text(), "hello world\n")
        self.assertEqual(
            (out / "src" / "sub" / "nested.txt").read_text(), "nested content\n"
        )

    def test_restore_refuses_nonempty_output_dir(self):
        out = self.tmp / "occupied"
        out.mkdir()
        (out / "existing.txt").write_text("do not clobber")
        rc = mod.run_restore(
            backup_file=self.backup_file,
            output_dir=out,
            list_only=False,
            force=False,
            identity=self.identity,
            password_file=self.tmp / "unused",
        )
        self.assertEqual(rc, 1)
        self.assertEqual((out / "existing.txt").read_text(), "do not clobber")

    def test_restore_rejects_unknown_format(self):
        weird = self.tmp / "backup.tar.zst"
        weird.write_bytes(b"x")
        rc = mod.run_restore(
            backup_file=weird,
            output_dir=None,
            list_only=True,
            force=False,
            identity=self.identity,
            password_file=self.tmp / "unused",
        )
        self.assertEqual(rc, 1)


@unittest.skipUnless(
    AGE and AGE_KEYGEN and _tar_is_gnu(), "age and GNU tar required"
)
class TestCreateBackupEndToEnd(ScriptTestCase):
    """Full create_backup -> verify_backup -> restore cycle. Needs GNU tar
    (the streaming pipeline uses GNU-only flags), so this runs on the server
    and on machines with brew's gnu-tar, and skips elsewhere."""

    def test_create_verify_restore(self):
        identity = self.tmp / "key.txt"
        subprocess.run(
            [AGE_KEYGEN, "-o", str(identity)],
            check=True,
            capture_output=True,
            timeout=30,
        )
        identity.chmod(0o600)

        src = self.tmp / "data"
        src.mkdir()
        (src / "a.txt").write_text("alpha")
        excluded = src / "excluded"
        excluded.mkdir()
        (excluded / "b.txt").write_text("should not appear")

        mod.config.age_identity_file = identity
        mod.config.compression_tool = "gzip"
        mod.config.backup_sources = [src]
        mod.config.backup_exclusions = [excluded]
        mod.config.plex_data_dir = self.tmp / "no-plex-here"

        backup_file = self.tmp / "e2e_backup.tar.gz.age"
        self.assertTrue(mod.create_backup(backup_file))
        self.assertTrue(mod.verify_backup(backup_file))

        out = self.tmp / "restored"
        rc = mod.run_restore(
            backup_file=backup_file,
            output_dir=out,
            list_only=False,
            force=False,
            identity=identity,
            password_file=self.tmp / "unused",
        )
        self.assertEqual(rc, 0)
        restored_root = out / str(src.resolve()).lstrip("/")
        self.assertEqual((restored_root / "a.txt").read_text(), "alpha")
        self.assertFalse((restored_root / "excluded").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
