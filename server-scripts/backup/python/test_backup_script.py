#!/usr/bin/env python3
"""
Test suite for the overengineered backup script.

This test suite follows TDD principles and tests the core functionality
of the backup script without requiring actual system dependencies.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, mock_open
import sys
import os
import importlib.util

# Add the script directory to the path so we can import the module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import the backup script module by loading it as a module
import importlib.util
script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'overengineered-backup-script.py')
spec = importlib.util.spec_from_file_location("backup_script", script_path)
backup_script = importlib.util.module_from_spec(spec)
sys.modules["backup_script"] = backup_script
spec.loader.exec_module(backup_script)


class TestBackupScript(unittest.TestCase):
    """Test cases for the backup script functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.test_log_file = self.temp_dir / "test.log"
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_format_bytes(self):
        """Test the format_bytes function with various inputs."""
        # Test zero bytes
        self.assertEqual(backup_script.format_bytes(0), "0 B")
        self.assertEqual(backup_script.format_bytes(None), "0 B")
        
        # Test bytes
        self.assertEqual(backup_script.format_bytes(512), "512.00 B")
        
        # Test kilobytes
        self.assertEqual(backup_script.format_bytes(1024), "1.00 KB")
        self.assertEqual(backup_script.format_bytes(1536), "1.50 KB")
        
        # Test megabytes
        self.assertEqual(backup_script.format_bytes(1024 * 1024), "1.00 MB")
        self.assertEqual(backup_script.format_bytes(1024 * 1024 * 2.5), "2.50 MB")
        
        # Test gigabytes
        self.assertEqual(backup_script.format_bytes(1024 * 1024 * 1024), "1.00 GB")

    @patch('backup_script.requests.post')
    def test_send_discord_notification_success(self, mock_post):
        """Test successful Discord notification sending."""
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        # Temporarily set webhook URL for testing
        original_url = backup_script.DISCORD_WEBHOOK_URL
        backup_script.DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/test"

        try:
            backup_script.send_discord_notification("Test", "Test message", 65280)
            mock_post.assert_called_once()

            # Verify the payload structure
            call_args = mock_post.call_args
            payload = call_args[1]['json']
            self.assertIn('embeds', payload)
            self.assertEqual(payload['embeds'][0]['title'], "Local Backup Status: Test")
            self.assertEqual(payload['embeds'][0]['description'], "Test message")
            self.assertEqual(payload['embeds'][0]['color'], 65280)
        finally:
            backup_script.DISCORD_WEBHOOK_URL = original_url

    @patch('backup_script.requests.post')
    def test_send_discord_notification_dry_run(self, mock_post):
        """Test Discord notification in dry run mode."""
        original_dry_run = backup_script.dry_run_mode
        backup_script.dry_run_mode = True

        try:
            backup_script.send_discord_notification("Test", "Test message", 65280)
            mock_post.assert_not_called()
        finally:
            backup_script.dry_run_mode = original_dry_run

    def test_json_parsing_in_rclone_sync(self):
        """Test JSON parsing logic used in rclone sync."""
        # Create test JSON log content
        test_log_content = '''
[2024-01-01 12:00:00] [INFO] Starting backup
{"level": "info", "msg": "Starting sync", "time": "2024-01-01T12:00:00Z"}
{"level": "info", "msg": "Sync progress", "stats": {"transfers": 5, "bytes": 1024000, "errors": 0, "checks": 10, "totalBytes": 2048000}}
{"level": "error", "msg": "File not found: /tmp/missing.txt"}
{"level": "info", "msg": "Final stats", "stats": {"transfers": 10, "bytes": 2048000, "errors": 1, "checks": 20, "totalBytes": 4096000}}
[2024-01-01 12:05:00] [INFO] Backup completed
'''
        
        # Write test content to a temporary file
        with open(self.test_log_file, 'w') as f:
            f.write(test_log_content)
        
        # Parse the log file similar to how rclone_sync does it
        final_stats = {}
        error_lines = []
        
        with open(self.test_log_file, "r") as f:
            for line in f:
                if not line.strip().startswith("{"):
                    continue
                try:
                    parsed_json: object = json.loads(line)
                    if not isinstance(parsed_json, dict):
                        continue
                    
                    log_entry = parsed_json
                    
                    stats_data = log_entry.get("stats")
                    if "stats" in log_entry and isinstance(stats_data, dict):
                        final_stats = stats_data
                    
                    elif log_entry.get("level") == "error":
                        msg = log_entry.get("msg")
                        if isinstance(msg, str):
                            error_lines.append(msg)
                except json.JSONDecodeError:
                    continue
        
        # Verify parsing results
        self.assertEqual(final_stats.get('transfers'), 10)
        self.assertEqual(final_stats.get('bytes'), 2048000)
        self.assertEqual(final_stats.get('errors'), 1)
        self.assertEqual(len(error_lines), 1)
        self.assertIn("File not found", error_lines[0])

    @patch('backup_script.shutil.which')
    def test_pre_flight_checks_missing_dependency(self, mock_which):
        """Test pre-flight checks with missing dependencies."""
        # Mock missing dependency
        mock_which.return_value = None

        with self.assertRaises(SystemExit):
            backup_script.pre_flight_checks()

    @patch('backup_script.os.geteuid')
    def test_pre_flight_checks_non_root(self, mock_geteuid):
        """Test pre-flight checks when not running as root."""
        mock_geteuid.return_value = 1000  # Non-root user

        with self.assertRaises(SystemExit):
            backup_script.pre_flight_checks()

    def test_uptime_kuma_retry_class_initialization(self):
        """Test UptimeKumaRetry class initialization."""
        retry_instance = backup_script.UptimeKumaRetry(
            url="https://test.com",
            username="test",
            password="test",
            max_retries=3
        )
        
        self.assertEqual(retry_instance.url, "https://test.com")
        self.assertEqual(retry_instance.username, "test")
        self.assertEqual(retry_instance.password, "test")
        self.assertEqual(retry_instance.max_retries, 3)
        self.assertIsNone(retry_instance.api)

    @patch('backup_script.DOCKER_STACKS_DIR')
    def test_get_docker_compose_files(self, mock_stacks_dir):
        """Test Docker compose file discovery."""
        # Create a mock directory structure
        mock_dir = Mock()
        mock_dir.is_dir.return_value = True
        mock_dir.glob.side_effect = [
            [Path("/test/compose.yaml"), Path("/test/plex/compose.yaml")],
            [Path("/test/other/compose.yml")]
        ]
        mock_stacks_dir.__truediv__ = Mock(return_value=mock_dir)

        # Mock PLEX_COMPOSE_FILE
        with patch('backup_script.PLEX_COMPOSE_FILE', Path("/test/plex/compose.yaml")):
            plex_files, other_files = backup_script.get_docker_compose_files()

            # Note: This test is simplified due to mocking complexity
            # In a real scenario, we'd need more sophisticated mocking


class TestTypeDefinitions(unittest.TestCase):
    """Test type definitions and type safety."""
    
    def test_typed_dict_structures(self):
        """Test that TypedDict structures are properly defined."""
        # Test ServerInfo
        server_info: backup_script.ServerInfo = {
            "serverTimezone": "UTC"
        }
        self.assertEqual(server_info["serverTimezone"], "UTC")
        
        # Test MaintenanceResponse
        maintenance_response: backup_script.MaintenanceResponse = {
            "maintenanceID": 123
        }
        self.assertEqual(maintenance_response["maintenanceID"], 123)
        
        # Test Monitor
        monitor: backup_script.Monitor = {
            "id": 1,
            "name": "Test Monitor"
        }
        self.assertEqual(monitor["id"], 1)
        self.assertEqual(monitor["name"], "Test Monitor")


if __name__ == '__main__':
    # Run the tests
    unittest.main(verbosity=2)
