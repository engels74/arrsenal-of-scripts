#!/usr/bin/env bash

# Script: radarr_touch_file_mtime.sh
# Purpose: Updates the modification timestamp of movie files to the current time. This is useful when
#          Plex mis-sorts files due to incorrect dates in metadata from downloaders (e.g., NZB or torrent indexers).
#          Radarr provides the file path via "$radarr_moviefile_path".
#
# How to Set Up in Radarr:
# 1. Save this script as `radarr_touch_file_mtime.sh` and make it executable:
#    chmod +x /path/to/radarr_touch_file_mtime.sh
#
# 2. Add it as a Custom Script in Radarr:
#    - Go to Settings > Connect > Add > Custom Script.
#    - Name: Update File Modification Time
#    - Path: /path/to/radarr_touch_file_mtime.sh
#    - Triggers: Enable "On Import" and "On Upgrade".
#
# 3. Done! Radarr will now run this script on file import/upgrade, ensuring the file's mod time
#    is updated to the current time, which helps Plex sort files more reliably.

touch "$radarr_moviefile_path"
exit 0
