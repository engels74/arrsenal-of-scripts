#!/usr/bin/env bash

# Script: sonarr_touch_file_mtime.sh
# Purpose: Updates the modification timestamp of episode files to the current time. This is useful when
#          Plex mis-sorts files due to incorrect dates in metadata from downloaders (e.g., NZB or torrent indexers).
#          Sonarr provides the file path via "$sonarr_episodefile_path".
#
# How to Set Up in Sonarr:
# 1. Save this script as `sonarr_touch_file_mtime.sh` and make it executable:
#    chmod +x /path/to/sonarr_touch_file_mtime.sh
#
# 2. Add it as a Custom Script in Sonarr:
#    - Go to Settings > Connect > Add > Custom Script.
#    - Name: Update File Modification Time
#    - Path: /path/to/sonarr_touch_file_mtime.sh
#    - Triggers: Enable "On Import" and "On Upgrade".
#
# 3. Done! Sonarr will now run this script on file import/upgrade, ensuring the file's mod time
#    is updated to the current time, which helps Plex sort files more reliably.

touch "$sonarr_episodefile_path"
exit 0

