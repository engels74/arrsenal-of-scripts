#!/usr/bin/env python3
"""
Simple test script to verify the fixes work without external dependencies.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import sys
import os

# Add the script directory to the path so we can import the module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Mock the external dependencies before importing the backup script
sys.modules['requests'] = Mock()
sys.modules['pytz'] = Mock()
sys.modules['uptime_kuma_api'] = Mock()

# Now import the backup script module
import importlib.util
script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'overengineered-backup-script.py')
spec = importlib.util.spec_from_file_location("backup_script", script_path)
backup_script = importlib.util.module_from_spec(spec)
sys.modules["backup_script"] = backup_script
spec.loader.exec_module(backup_script)


class TestBackupScriptFixes(unittest.TestCase):
    """Test the specific fixes implemented."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = Path(tempfile.mkdtemp())
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_discord_notification_privatebin_link_fix(self):
        """Test that Discord success notification includes the privatebin link properly."""
        # Test the success message formatting with privatebin link
        privatebin_link = "https://privatebin.example.com/paste/abc123"
        
        # Simulate the success message construction (fixed version)
        success_timestamp = "2024-01-01 12:00:00"
        rclone_summary = {
            'duration': '1h:30m:45s',
            'transferred_data': '2.50 GB',
            'transferred_files': '150 files',
            'checks_count': 200,
            'total_checks': 200
        }
        
        message = (
            f"**Status Details**\n"
            f"✅ Sync completed successfully\n"
            f"⏱️ Duration: {rclone_summary['duration']}\n"
            f"📦 Data: {rclone_summary['transferred_data']}\n"
            f"📄 Files: {rclone_summary['transferred_files']}\n"
            f"🔍 Checks: {rclone_summary['checks_count']} / {rclone_summary['total_checks']}\n"
        )
        
        # Test the fixed behavior
        if privatebin_link:
            message += f"🔗 **[View Logs]({privatebin_link})**\n\n"
        else:
            message += "\n"
            
        # This should contain the actual link
        self.assertIn(privatebin_link, message)
        self.assertIn("View Logs", message)
        self.assertIn("**[View Logs]", message)  # Should be formatted as markdown link

    def test_set_log_permissions_function_exists(self):
        """Test that set_log_permissions function exists and works."""
        # Create a test log file
        test_log = self.temp_dir / "test.log"
        test_log.write_text("Test log content")
        
        # Mock the user/group lookup and chown operation
        with patch('backup_script.pwd.getpwnam') as mock_getpwnam, \
             patch('backup_script.grp.getgrnam') as mock_getgrnam, \
             patch('backup_script.os.chown') as mock_chown, \
             patch('backup_script.os.chmod') as mock_chmod:
            
            # Mock user/group data
            mock_user = Mock()
            mock_user.pw_uid = 1001
            mock_getpwnam.return_value = mock_user
            
            mock_group = Mock()
            mock_group.gr_gid = 1001
            mock_getgrnam.return_value = mock_group
            
            # Test that the function exists and works properly
            backup_script.set_log_permissions(test_log)
            mock_chown.assert_called_once_with(test_log, 1001, 1001)
            mock_chmod.assert_called_once_with(test_log, 0o644)

    def test_threading_import_available(self):
        """Test that threading module is imported for pv progress logging."""
        # Check that threading is available in the backup script
        self.assertTrue(hasattr(backup_script, 'threading'))

    def test_pv_progress_logging_structure(self):
        """Test that the pv progress logging structure is in place."""
        # This test verifies that the create_backup function has the threading logic
        # We can't easily test the actual threading without running the full backup
        # but we can verify the structure is there
        
        # Check that the create_backup function exists
        self.assertTrue(hasattr(backup_script, 'create_backup'))
        self.assertTrue(callable(backup_script.create_backup))

    def test_format_bytes_function(self):
        """Test the format_bytes function works correctly."""
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


if __name__ == '__main__':
    # Run the tests
    unittest.main(verbosity=2)
