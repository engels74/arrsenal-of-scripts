#!/bin/bash

# --- Script Description ---
# Purpose: Guides the user through updating DNS and VPN environment variables
#          within Docker Compose YAML files, specifically targeting services
#          that utilize 'hotio' or 'engels74' images. This facilitates migration
#          to newer configuration standards for these containers.
# Action:  Recursively finds *.yml/*.yaml files, identifies relevant services,
#          interactively prompts for desired VPN/Unbound settings (VPN_ENABLED,
#          VPN_NAMESERVERS, UNBOUND_ENABLED, UNBOUND_NAMESERVERS), modifies
#          the selected YAML files using 'yq', creates backups (.bak) before
#          saving changes, and shows a diff for review and confirmation.

# --- Configuration ---
DEFAULT_SEARCH_DIR="."

# --- Helper Functions ---

print_color() {
    local color_code=""
    case "$1" in
        red) color_code="\033[0;31m" ;;
        green) color_code="\033[0;32m" ;;
        yellow) color_code="\033[0;33m" ;;
        blue) color_code="\033[0;34m" ;;
        magenta) color_code="\033[0;35m" ;;
        cyan) color_code="\033[0;36m" ;;
        reset) color_code="\033[0m" ;;
        *) echo "$2"; return ;;
    esac
    local reset_code="\033[0m"
    printf "%b%s%b\n" "${color_code}" "$2" "${reset_code}"
}

ask_yes_no() {
    local prompt="$1"
    local default="$2"
    local answer
    while true; do
        printf "%b%s%b" "$(print_color yellow '')" "$prompt" "$(print_color reset '')"
        if [[ "$default" == "y" ]]; then
            read -p " [Y/n]: " answer
            answer=${answer:-y}
        elif [[ "$default" == "n" ]]; then
            read -p " [y/N]: " answer
            answer=${answer:-n}
        else
            read -p " [y/n]: " answer
        fi
        case "$answer" in
            [Yy]* ) return 0;;
            [Nn]* ) return 1;;
            * ) print_color red "Please answer yes (y) or no (n).";;
        esac
    done
}

print_header() {
    local width=65
    local inner_width=$((width - 2))
    center_text() {
        local text="$1"
        local plain_text=$(echo -e "$text" | sed 's/\x1b\[[0-9;]*m//g')
        local text_len=${#plain_text}
        local pad_len=$(((inner_width - text_len) / 2))
        [[ $pad_len -lt 0 ]] && pad_len=0
        local remaining_pad=$((inner_width - text_len - pad_len))
        [[ $remaining_pad -lt 0 ]] && remaining_pad=0
        printf "%*s%s%*s" $pad_len "" "$text" $remaining_pad ""
    }
    local tl="╭" tr="╮" bl="╰" br="╯" hz="─" vt="│" lc="├" rc="┤"
    local separator=$(printf "%s" "$lc"; printf "%0.s$hz" $(seq 1 $inner_width); printf "%s" "$rc")
    local border_color="blue"
    local title_color="cyan"
    local subtitle_color="blue"
    local desc_color="yellow"
    local warn_color="red"
    local tip_color="green"
    local section_color="magenta"
    print_color "$border_color" "${tl}$(printf "%0.s$hz" $(seq 1 $inner_width))${tr}"
    print_color "$border_color" "${vt}$(printf '%*s' $inner_width '')${vt}"
    local title1_text="engels74 / hotio"
    local title2_text="Unbound / DNS / VPN Migration Helper"
    local centered_title1=$(center_text "$(print_color "$title_color" "$title1_text")")
    local centered_title2=$(center_text "$(print_color "$subtitle_color" "$title2_text")")
    print_color "$border_color" "${vt}${centered_title1}${vt}"
    print_color "$border_color" "${vt}${centered_title2}${vt}"
    print_color "$border_color" "${vt}$(printf '%*s' $inner_width '')${vt}"
    print_color "$border_color" "$separator"
    local desc1="This script guides you through updating DNS/VPN environment"
    local desc2="variables for hotio/engels74 Docker containers."
    local desc_pad_width=$((inner_width - 1))
    print_color "$border_color" "${vt} $(print_color "$desc_color" "$(printf "%-*s" $desc_pad_width "$desc1")")${vt}"
    print_color "$border_color" "${vt} $(print_color "$desc_color" "$(printf "%-*s" $desc_pad_width "$desc2")")${vt}"
    local imp_label=" IMPORTANT "
    local imp_label_plain_len=${#imp_label}
    local imp_pad=$(((inner_width - imp_label_plain_len) / 2))
    local imp_sep1=$(printf "%0.s$hz" $(seq 1 $imp_pad))
    local imp_sep2=$(printf "%0.s$hz" $(seq 1 $((inner_width - imp_pad - imp_label_plain_len))))
    print_color "$border_color" "${lc}${imp_sep1}$(print_color "$section_color" "$imp_label")${imp_sep2}${rc}"
    local warn1="! This script WILL NOW MODIFY your files (with backup)."
    local warn2="! You will see a preview and must confirm before saving."
    local tip1="✓ Always back up configuration files before making changes!"
    local base_pad_width=$((inner_width - 1))
    local warn_pad_width=$base_pad_width
    local tip_pad_width=$((base_pad_width + 2))
    print_color "$border_color" "${vt} $(print_color "$warn_color" "$(printf "%-*s" $warn_pad_width "$warn1")")${vt}"
    print_color "$border_color" "${vt} $(print_color "$warn_color" "$(printf "%-*s" $warn_pad_width "$warn2")")${vt}"
    print_color "$border_color" "${vt} $(print_color "$tip_color" "$(printf "%-*s" $tip_pad_width "$tip1")")${vt}"
    print_color "$border_color" "${bl}$(printf "%0.s$hz" $(seq 1 $inner_width))${br}"
    echo
}

check_dependency() {
    if ! command -v yq &> /dev/null; then
        print_color red "Error: 'yq' command not found."
        print_color red "This script requires the Go-based 'yq' by Mike Farah (https://github.com/mikefarah/yq)."
        print_color yellow "Installation instructions: https://github.com/mikefarah/yq#install"
        print_color yellow "Please ensure you install the correct version, NOT the Python wrapper often found in apt."
        exit 1
    fi
    yq_identity_string=$(yq -V 2>&1)
    yq_check_exit_code=$?
    if [ $yq_check_exit_code -ne 0 ]; then
        print_color red "Error: 'yq -V' command failed to execute."
        print_color red "Exit code: $yq_check_exit_code"
        print_color red "This script requires the Go-based 'yq' by Mike Farah."
        print_color yellow "Please ensure 'yq' is installed correctly and accessible in your PATH."
        print_color yellow "Installation instructions: https://github.com/mikefarah/yq#install"
        exit 1
    fi
    if ! echo "$yq_identity_string" | grep -q -i 'mikefarah'; then
        print_color red "Error: Incorrect 'yq' detected."
        print_color red "This script requires the Go-based 'yq' by Mike Farah (https://github.com/mikefarah/yq)."
        print_color red "The 'yq' found in your PATH seems to be a different tool (e.g., the Python wrapper from kislyuk)."
        print_color yellow "Please uninstall the incorrect yq and install the correct one."
        print_color yellow "Installation instructions: https://github.com/mikefarah/yq#install"
        print_color yellow "Example (check link for latest): wget https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -O /usr/bin/yq && chmod +x /usr/bin/yq"
        exit 1
    else
        print_color green "✓ Correct 'yq' (Mike Farah version) detected:"
        print_color green "  $yq_identity_string"
        echo
    fi
}

# --- NEW: Function to backup a file ---
backup_file() {
    local file="$1"
    local backup="${file}.bak.$(date +%Y%m%d%H%M%S)"
    cp "$file" "$backup"
    print_color green "Backup created: $backup"
}

# --- MODIFIED: Function to pretty print YAML diff, ignoring whitespace ---
show_yaml_diff() {
    local orig="$1"
    local mod="$2"
    if command -v diff &>/dev/null; then
        print_color magenta "YAML diff (original vs. proposed - ignoring whitespace changes):"
        # Use -w flag to ignore whitespace differences
        diff --color=always -w -u "$orig" "$mod" || true
    else
        print_color yellow "Diff tool not found, showing full new YAML:"
        cat "$mod"
    fi
}

# --- Main Script Logic ---

print_header
check_dependency

read -p "$(print_color yellow "Enter directory containing your docker-compose YAML files [${DEFAULT_SEARCH_DIR}]: ")" SEARCH_DIR
SEARCH_DIR=${SEARCH_DIR:-$DEFAULT_SEARCH_DIR}

if [ ! -d "$SEARCH_DIR" ]; then
    print_color red "Error: Directory '$SEARCH_DIR' not found."
    exit 1
fi

echo
print_color blue "Searching for *.yml and *.yaml files recursively in '$SEARCH_DIR'..."
mapfile -t files < <(find "$SEARCH_DIR" \( -name "*.yml" -o -name "*.yaml" \) -type f | sort)

if [ ${#files[@]} -eq 0 ]; then
    print_color red "No docker-compose YAML files (*.yml or *.yaml) found in '$SEARCH_DIR' or its subdirectories."
    exit 1
fi

print_color green "Found files (sorted):"
for f in "${files[@]}"; do
    relative_path="${f#$SEARCH_DIR/}"
    relative_path="${relative_path#./}"
    echo " - $relative_path"
done
echo

for file in "${files[@]}"; do
    relative_path="${file#$SEARCH_DIR/}"
    relative_path="${relative_path#./}"
    echo
    print_color cyan "--- Processing File: $relative_path ---"

    if ! ask_yes_no "Do you want to process this file?" "y"; then
        print_color yellow "Skipping file: $relative_path"
        continue
    fi

    echo
    print_color blue "Detecting services in '$relative_path' using yq..."
    yq_output=$(yq e '.services | keys | .[]' "$file" 2>&1)
    yq_exit_code=$?
    all_services=()
    if [ $yq_exit_code -eq 0 ] && [ -n "$yq_output" ] && [[ "$yq_output" != "null" ]]; then
         mapfile -t all_services < <(echo "$yq_output")
    else
        print_color yellow "Warning: Could not detect services using yq in '$relative_path'."
        if [ $yq_exit_code -ne 0 ]; then
             print_color yellow "  yq error code: $yq_exit_code"
             print_color yellow "  Check if the file is valid YAML and readable."
        else
             print_color yellow "  Reason: No services found under the 'services:' key or the file is empty/invalid."
        fi
        print_color yellow "Skipping service configuration for this file."
        continue
    fi

    if [ ${#all_services[@]} -eq 0 ]; then
         print_color yellow "No services found under the 'services:' key in '$relative_path'."
         continue
    fi

    print_color blue "Filtering for services using 'hotio/' or 'engels74/' images..."
    services_to_process=()
    for service in "${all_services[@]}"; do
        image_name=$(yq e ".services.$service.image" "$file" 2> /dev/null)
        image_exit_code=$?
        if [[ $image_exit_code -eq 0 && -n "$image_name" && ( "$image_name" == *"hotio/"* || "$image_name" == *"engels74/"* ) ]]; then
            services_to_process+=("$service")
        fi
    done

    if [ ${#services_to_process[@]} -eq 0 ]; then
         print_color yellow "No services using 'hotio/' or 'engels74/' images found in '$relative_path'."
         continue
    fi

    echo
    print_color magenta "Relevant services found in '$relative_path':"
    for s in "${services_to_process[@]}"; do
        echo " - $s"
    done
    echo

    # --- MODIFIED: Flag to track if any changes were proposed for the *entire file* ---
    file_modified_flag=0
    # --- MODIFIED: Create a single temp file per YAML file ---
    tmp_yaml=$(mktemp)
    cp "$file" "$tmp_yaml"

    for service in "${services_to_process[@]}"; do
        echo
        print_color blue "---== Configuring Service: '$service' in '$relative_path' ==---"

        env_vars_to_add=()
        # Define legacy vars to potentially remove (as comments initially)
        env_vars_to_remove_check=("# UNBOUND_ENABLED" "# VPN_NAMESERVERS" "# UNBOUND_NAMESERVERS")

        if ask_yes_no "1. Use VPN for '$service'?" "n"; then
            env_vars_to_add+=("VPN_ENABLED=true")
            # Mark legacy vars related to VPN OFF as not needed
            env_vars_to_remove_check=("${env_vars_to_remove_check[@]/# UNBOUND_ENABLED}")
            env_vars_to_remove_check=("${env_vars_to_remove_check[@]/# UNBOUND_NAMESERVERS}")
            print_color green " -> VPN Enabled. Setting VPN_ENABLED=true."
            print_color yellow "    (Requires 'wg0.conf' in '/config/wireguard' volume mount)"
            echo
            print_color blue "2. VPN DNS Setup (VPN_NAMESERVERS):"
            echo "   How should DNS be handled when VPN is ACTIVE?"
            echo "   Options:"
            echo "     - 'wg'          : Use DNS from VPN provider (wg0.conf)."
            echo "     - IP/DoT        : Use specific servers (e.g., 8.8.8.8, 1.1.1.1@853#cloudflare-dns.com)."
            echo "     - <empty>       : Use Unbound in recursive mode (no upstream)."
            echo "     - Combination   : Mix options (e.g., 'wg,1.1.1.1')."
            print_color yellow "     (Note: DoT '@' overrides regular IPs. 'wg' requires VPN_ENABLED=true)"
            read -p "$(print_color yellow "   Enter VPN_NAMESERVERS value: ")" vpn_dns_value
            env_vars_to_add+=("VPN_NAMESERVERS=${vpn_dns_value}")
            # Mark legacy VPN_NAMESERVERS as not needed (we're setting it)
            env_vars_to_remove_check=("${env_vars_to_remove_check[@]/# VPN_NAMESERVERS}")
            print_color green " -> Setting VPN_NAMESERVERS=${vpn_dns_value}"
        else
            env_vars_to_add+=("# VPN_ENABLED=false  # Or remove this line entirely")
            # Mark legacy VPN_NAMESERVERS as not needed (VPN is off)
            env_vars_to_remove_check=("${env_vars_to_remove_check[@]/# VPN_NAMESERVERS}")
            print_color green " -> VPN Disabled. Ensure VPN_ENABLED is false or removed."
            echo
            if ask_yes_no "2. Use Unbound DNS resolver for '$service' (when VPN is OFF)?" "n"; then
                env_vars_to_add+=("UNBOUND_ENABLED=true")
                # Mark legacy UNBOUND_ENABLED as not needed
                env_vars_to_remove_check=("${env_vars_to_remove_check[@]/# UNBOUND_ENABLED}")
                print_color green " -> Unbound Enabled (VPN OFF). Setting UNBOUND_ENABLED=true."
                echo
                print_color blue "3. Unbound DNS Setup (UNBOUND_NAMESERVERS):"
                echo "   How should Unbound DNS be handled when VPN is OFF?"
                echo "   Options:"
                echo "     - IP/DoT        : Use specific servers (e.g., 8.8.8.8, 1.1.1.1@853#cloudflare-dns.com)."
                echo "     - <empty>       : Use Unbound in recursive mode (no upstream)."
                print_color yellow "     (Note: DoT '@' overrides regular IPs. 'wg' is NOT valid here)"
                read -p "$(print_color yellow "   Enter UNBOUND_NAMESERVERS value: ")" unbound_dns_value
                if [[ "$unbound_dns_value" == *"wg"* ]]; then
                    print_color red "   Warning: 'wg' is not a valid option for UNBOUND_NAMESERVERS. Please correct manually if needed."
                fi
                env_vars_to_add+=("UNBOUND_NAMESERVERS=${unbound_dns_value}")
                # Mark legacy UNBOUND_NAMESERVERS as not needed
                env_vars_to_remove_check=("${env_vars_to_remove_check[@]/# UNBOUND_NAMESERVERS}")
                print_color green " -> Setting UNBOUND_NAMESERVERS=${unbound_dns_value}"
            else
                env_vars_to_add+=("# UNBOUND_ENABLED=false # Or remove this line entirely")
                # Mark legacy UNBOUND vars as not needed (Unbound is off)
                env_vars_to_remove_check=("${env_vars_to_remove_check[@]/# UNBOUND_ENABLED}")
                env_vars_to_remove_check=("${env_vars_to_remove_check[@]/# UNBOUND_NAMESERVERS}")
                print_color green " -> Unbound Disabled (VPN OFF). Ensure UNBOUND_ENABLED is false or removed."
                print_color yellow "    (Container will use default Docker/host DNS)"
            fi
        fi

        # --- MODIFIED: Apply changes for this service to the single temp file ---
        # 1. Remove legacy vars if present (only those marked for removal)
        for var_to_remove in "${env_vars_to_remove_check[@]}"; do
             # Only process if it's still marked (i.e., not filtered out by logic above)
            if [[ "$var_to_remove" == "#"* ]]; then
                var_name=$(echo "$var_to_remove" | sed 's/^# *//;s/ .*//')
                # Use yq to delete the specific variable if it exists in the environment list
                # This handles cases like VAR, VAR=, VAR=true, VAR=false
                yq e -i "del(.services.$service.environment[] | select(capture(\"^${var_name}(=.*)?$\") | length > 0))" "$tmp_yaml" 2>/dev/null
            fi
        done

        # 2. Add/set new vars
        for var in "${env_vars_to_add[@]}"; do
            if [[ "$var" == \#* ]]; then
                continue # skip commented suggestions
            fi
            var_name=$(echo "$var" | cut -d= -f1)
            var_value=$(echo "$var" | cut -d= -f2-)
            # Remove any existing entry for this var first to ensure replacement
            yq e -i "del(.services.$service.environment[] | select(capture(\"^${var_name}(=.*)?$\") | length > 0))" "$tmp_yaml" 2>/dev/null
            # Add the new entry
            yq e -i ".services.$service.environment += [\"$var_name=$var_value\"]" "$tmp_yaml"
        done

        # --- MODIFIED: Set flag indicating potential changes ---
        file_modified_flag=1

        read -p "$(print_color yellow "Press Enter to process next service or finish file...")"
    done # End service loop

    # --- MODIFIED: Show diff and confirm *once* per file, only if changes were proposed ---
    if [ "$file_modified_flag" -eq 1 ]; then
        # Clean up empty environment arrays (optional, for neatness) - Do this *after* all services are processed
        yq e -i '(.services[] | select(.environment == [])) |= del(.environment)' "$tmp_yaml"

        echo
        print_color cyan "--- Proposed Final YAML for '$relative_path' ---"
        show_yaml_diff "$file" "$tmp_yaml"
        echo
        if ask_yes_no "Do you want to apply ALL proposed changes to '$relative_path'?" "y"; then
            backup_file "$file"
            mv "$tmp_yaml" "$file"
            print_color green "Changes applied to $relative_path!"
        else
            print_color yellow "No changes made to $relative_path."
            rm "$tmp_yaml"
        fi
    else
        print_color yellow "No configuration changes were proposed for any service in '$relative_path'."
        rm "$tmp_yaml" # Clean up temp file even if no changes
    fi

done # End file loop

echo
print_color green "--- Script Finished ---"
print_color yellow "Review your files and restart your containers as needed."
print_color yellow "Always test your configuration!"

exit 0
