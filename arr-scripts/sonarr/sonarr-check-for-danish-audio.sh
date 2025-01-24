#!/usr/bin/env bash

# Path to ffprobe
FFPROBE="/app/bin/ffprobe"

# Sonarr API details
SONARR_API_URL="http://localhost:8989/api/v3"
SONARR_API_KEY="your_api_key_here"

# Log function to add timestamps
log_debug() {
    echo "$(date +'%Y-%m-%d %H:%M:%S') | $1" >&1
}

log_info() {
    echo "$(date +'%Y-%m-%d %H:%M:%S') | $1" >&2
}

# Function to check if the file contains a Danish audio track
check_danish_audio() {
    local file_path="$1"
    local audio_info

    log_debug "Checking for Danish audio track in file: $file_path"

    if [[ ! -x "$FFPROBE" ]]; then
        log_info "Error: ffprobe not found at $FFPROBE"
        return 1
    fi

    audio_info=$("$FFPROBE" -v error -show_entries stream=index,codec_name,codec_type,codec_tag_string,codec_tag,language:stream_tags=language -of default=noprint_wrappers=1 "$file_path")

    if [[ -z "$audio_info" ]]; then
        log_info "No audio streams found or unable to read file."
        return 1
    fi

    local in_audio_stream=0

    while IFS= read -r line; do
        if [[ "$line" == "codec_type=audio" ]]; then
            in_audio_stream=1
        elif [[ "$line" == "codec_type="* ]]; then
            in_audio_stream=0
        elif [[ "$in_audio_stream" -eq 1 && "$line" == *"language=dan"* ]]; then
            log_debug "Danish audio track found."
            return 0
        fi
    done <<< "$audio_info"

    log_info "No Danish audio track found."
    return 1
}

# Function to mark the download as failed using the correct history record ID
mark_download_failed() {
    local history_id="$1"
    log_debug "Marking history entry with ID: $history_id as failed"
    
    response=$(curl -s -H "X-Api-Key: $SONARR_API_KEY" -X POST \
        "$SONARR_API_URL/history/failed/$history_id")

    if [[ "$response" == "" ]]; then
        log_debug "History entry with ID: $history_id marked as failed"
        return 0
    else
        log_info "Response: $response"
        return 0  # Marking as failed is successful, avoid logging failure here
    fi
}

# Function to find the history record ID using the download ID
find_history_record_id() {
    local download_id="$1"
    local history_records
    local history_id

    history_records=$(curl -s -H "X-Api-Key: $SONARR_API_KEY" "$SONARR_API_URL/history")

    history_id=$(echo "$history_records" | jq -r --arg download_id "$download_id" '.records[] | select(.downloadId == $download_id) | .id' | head -n 1)

    if [[ -z "$history_id" ]]; then
        log_info "Error: No history record found for download ID: $download_id"
        return 1
    fi

    echo "$history_id"
    return 0
}

# Function to delete a specific episode file in Sonarr
delete_episode_file() {
    local episode_file_id="$1"
    log_debug "Deleting episode file with ID: $episode_file_id"
    
    response=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE \
        -H "X-Api-Key: $SONARR_API_KEY" \
        "$SONARR_API_URL/episodefile/$episode_file_id")

    if [[ "$response" -eq 200 ]]; then
        log_debug "Episode file with ID $episode_file_id deleted successfully."
        return 0
    else
        log_info "Failed to delete episode file with ID $episode_file_id. HTTP status code: $response"
        return 1
    fi
}

# Main script execution
log_debug "EventType: ${sonarr_eventtype}"
log_debug "sonarr_episodefile_path: ${sonarr_episodefile_path:-not set}"
log_debug "sonarr_series_id: ${sonarr_series_id:-not set}"
log_debug "sonarr_download_id: ${sonarr_download_id:-not set}"
log_debug "sonarr_episodefile_scenename: ${sonarr_episodefile_scenename:-not set}"
log_debug "sonarr_download_client: ${sonarr_download_client:-not set}"
log_debug "sonarr_episodefile_id: ${sonarr_episodefile_id:-not set}"

if [[ "$sonarr_eventtype" == "Download" ]]; then
    if [[ -z "$sonarr_episodefile_path" ]]; then
        log_info "Error: No file path provided."
        exit 1
    fi

    if [[ ! -f "$sonarr_episodefile_path" ]]; then
        log_info "Error: File does not exist."
        exit 1
    fi

    check_danish_audio "$sonarr_episodefile_path"
    result=$?

    if [[ $result -eq 1 ]]; then
        log_info "No Danish audio track found. Removing the episode file and marking the download as failed."

        if [[ -z "$sonarr_episodefile_id" ]]; then
            log_info "Error: Episode file ID not provided by Sonarr."
            exit 1
        fi

        delete_episode_file "$sonarr_episodefile_id"
        remove_result=$?
        if [[ $remove_result -eq 0 ]]; then
            log_debug "Successfully removed episode file ID: $sonarr_episodefile_id"
        else
            log_info "Failed to remove episode file ID: $sonarr_episodefile_id"
        fi

        history_id=$(find_history_record_id "$sonarr_download_id")
        if [[ $? -eq 0 ]]; then
            mark_download_failed "$history_id"
            mark_result=$?
            if [[ $mark_result -eq 0 ]]; then
                log_debug "Successfully marked download as failed for history ID: $history_id"
            else
                log_info "Failed to mark download as failed for history ID: $history_id"
            fi
        else
            log_info "Failed to find history ID for download ID: $sonarr_download_id"
        fi
        exit 1
    fi
fi

if [[ "$sonarr_eventtype" == "Test" ]]; then
    log_debug "Test event received, script is working correctly."
    exit 0
fi

log_info "Unsupported event type: ${sonarr_eventtype}"
exit 1

# -----------------------------------------------------------------------------
# License
# -----------------------------------------------------------------------------

# Danish Audio Script
# <https://github.com/engels74/arrsenal-of-scripts>
# This script checks for Danish audio in downloaded files
# Copyright (C) 2024 - engels74
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# Contact: engels74@tuta.io
