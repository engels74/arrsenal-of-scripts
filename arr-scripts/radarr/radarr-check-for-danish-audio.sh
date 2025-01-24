#!/usr/bin/env bash

# Path to ffprobe
FFPROBE="/app/bin/ffprobe"

# Radarr API details
RADARR_API_URL="http://localhost:7878/api/v3"
RADARR_API_KEY="your_api_key_here"

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
    
    response=$(curl -s -H "X-Api-Key: $RADARR_API_KEY" -X POST \
        "$RADARR_API_URL/history/failed/$history_id")

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

    history_records=$(curl -s -H "X-Api-Key: $RADARR_API_KEY" "$RADARR_API_URL/history?eventType=1&downloadId=$download_id")

    history_id=$(echo "$history_records" | jq -r '.records[0].id')

    if [[ -z "$history_id" ]]; then
        log_info "Error: No history record found for download ID: $download_id"
        return 1
    fi

    echo "$history_id"
    return 0
}

# Function to delete a specific movie file in Radarr
delete_movie_file() {
    local movie_file_id="$1"
    log_debug "Deleting movie file with ID: $movie_file_id"
    
    response=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE \
        -H "X-Api-Key: $RADARR_API_KEY" \
        "$RADARR_API_URL/moviefile/$movie_file_id")

    if [[ "$response" -eq 200 ]]; then
        log_debug "Movie file with ID $movie_file_id deleted successfully."
        return 0
    else
        log_info "Failed to delete movie file with ID $movie_file_id. HTTP status code: $response"
        return 1
    fi
}

# Main script execution
log_debug "EventType: ${radarr_eventtype}"
log_debug "radarr_moviefile_path: ${radarr_moviefile_path:-not set}"
log_debug "radarr_movie_id: ${radarr_movie_id:-not set}"
log_debug "radarr_download_id: ${radarr_download_id:-not set}"
log_debug "radarr_moviefile_scenename: ${radarr_moviefile_scenename:-not set}"
log_debug "radarr_download_client: ${radarr_download_client:-not set}"

if [[ "$radarr_eventtype" == "Download" ]]; then
    if [[ -z "$radarr_moviefile_path" ]]; then
        log_info "Error: No file path provided."
        exit 1
    fi

    if [[ ! -f "$radarr_moviefile_path" ]]; then
        log_info "Error: File does not exist."
        exit 1
    fi

    check_danish_audio "$radarr_moviefile_path"
    result=$?

    if [[ $result -eq 1 ]]; then
        log_info "No Danish audio track found. Removing the movie file and marking the download as failed."

        movie_files=$(curl -s -H "X-Api-Key: $RADARR_API_KEY" "$RADARR_API_URL/moviefile?movieId=$radarr_movie_id")

        if [[ $? -ne 0 ]]; then
            log_info "Error: Failed to fetch movie files. Check API key and permissions."
            exit 1
        fi

        for file in $(echo "$movie_files" | jq -r '.[] | @base64'); do
            _jq() {
                echo "${file}" | base64 --decode | jq -r "${1}"
            }

            movie_file_id=$(_jq '.id')
            movie_file_path=$(_jq '.relativePath')

            log_debug "Found movie file ID: $movie_file_id for path: $movie_file_path"
            if [[ "$movie_file_path" == "$radarr_moviefile_path" ]]; then
                delete_movie_file "$movie_file_id"
                remove_result=$?
                if [[ $remove_result -eq 0 ]]; then
                    log_debug "Successfully removed movie file ID: $movie_file_id"
                else
                    log_info "Failed to remove movie file ID: $movie_file_id"
                fi
                break
            fi
        done

        history_id=$(find_history_record_id "$radarr_download_id")
        if [[ $? -eq 0 ]]; then
            mark_download_failed "$history_id"
            mark_result=$?
            if [[ $mark_result -eq 0 ]]; then
                log_debug "Successfully marked download as failed for history ID: $history_id"
                delete_movie_file "$movie_file_id"
                delete_result=$?
                if [[ $delete_result -eq 0 ]]; then
                    log_debug "Successfully deleted movie file ID: $movie_file_id"
                else
                    log_info "Failed to delete movie file ID: $movie_file_id"
                fi
            else
                log_info "Failed to mark download as failed for history ID: $history_id"
            fi
        else
            log_info "Failed to find history ID for download ID: $radarr_download_id"
        fi
        exit 1
    fi
fi

if [[ "$radarr_eventtype" == "Test" ]]; then
    log_debug "Test event received, script is working correctly."
    exit 0
fi

log_info "Unsupported event type: ${radarr_eventtype}"
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
