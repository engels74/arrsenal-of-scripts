# arr-danish-audio-check

This project contains scripts for Radarr and Sonarr to check if the imported file has a Danish audio track. If no Danish audio track is found, the file is marked as failed, and appropriate actions are taken.

## Scripts

### Radarr Script: `danishAudioRadarr.sh`

This script checks for a Danish audio track in files managed by Radarr. If the file does not contain a Danish audio track, it marks the download as failed.

#### Usage

1. The path to `ffprobe` is set by default to `/app/bin/ffprobe`, which is used by hotio Docker images. You can modify this path if needed:
    ```bash
    FFPROBE="/app/bin/ffprobe"
    RADARR_API_URL="http://localhost:7878/api/v3"
    RADARR_API_KEY="your_api_key_here"
    ```

2. Ensure the script has executable permissions:
    ```bash
    chmod +x radarr/danishAudioRadarr.sh
    ```

3. Configure Radarr to execute the script after a download is completed:
    - Go to Radarr Settings.
    - Navigate to `Connect` -> `Connections`.
    - Click on `+` to add a new connection.
    - Choose `Custom Script`.
    - Fill in the required fields:
      - Name: `Danish Audio Check`
      - Path: `/path/to/radarr/danishAudioRadarr.sh`
      - Select "On Import" and "On Upgrade" in "Notification Triggers".
    - Save the connection.

### Sonarr Script: `danishAudioSonarr.sh`

This script checks for a Danish audio track in files managed by Sonarr. If the file does not contain a Danish audio track, it deletes the episode file and marks the download as failed.

#### Usage

1. The path to `ffprobe` is set by default to `/app/bin/ffprobe`, which is used by hotio Docker images. You can modify this path if needed:
    ```bash
    FFPROBE="/app/bin/ffprobe"
    SONARR_API_URL="http://localhost:8989/api/v3"
    SONARR_API_KEY="your_api_key_here"
    ```

2. Ensure the script has executable permissions:
    ```bash
    chmod +x sonarr/danishAudioSonarr.sh
    ```

3. Configure Sonarr to execute the script after a download is completed:
    - Go to Sonarr Settings.
    - Navigate to `Connect` -> `Connections`.
    - Click on `+` to add a new connection.
    - Choose `Custom Script`.
    - Fill in the required fields:
      - Name: `Danish Audio Check`
      - Path: `/path/to/sonarr/danishAudioSonarr.sh`
      - Select "On Import" and "On Upgrade" in "Notification Triggers".
    - Save the connection.

## Log Functions

Both scripts include log functions to provide debug and informational messages with timestamps:
- `log_debug()`
- `log_info()`

## License

This project is licensed under the AGPLv3 License. See the [LICENSE](LICENSE) file for details.
