#!/bin/bash

# Script: Update Compose File URLs
# Purpose: This script updates URLs in YAML files (e.g., `compose.yml`, `docker-compose.yaml`, etc.)
#          to reflect the repository move from `walkxcode/dashboard-icons` to `homarr-labs/dashboard-icons`
#          and the format change from PNG to WEBP. It creates a backup of the original files,
#          allows previewing changes, and optionally overwrites the original files after confirmation.
#
# Features:
# - Supports all common YAML file extensions (*.yml, *.yaml).
# - Interactive directory input (source, test, backup).
# - Confirmation before creating directories.
# - Backup of original files for safety.
# - Preview changes before overwriting.
# - Colorful output for better readability.
# - Error handling and user-friendly prompts.

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'  # Changed from dark blue to cyan for better readability
NC='\033[0m' # No Color

# Function to display error messages and exit
error() {
  echo -e "${RED}[ERROR]${NC} $1"
  exit 1
}

# Function to display success messages
success() {
  echo -e "${GREEN}[SUCCESS]${NC} $1"
}

# Function to display info messages
info() {
  echo -e "${CYAN}[INFO]${NC} $1"  # Changed from BLUE to CYAN
}

# Function to display warning messages
warning() {
  echo -e "${YELLOW}[WARNING]${NC} $1"
}

# Check if sed is installed
if ! command -v sed &> /dev/null; then
  error "sed is not installed. Please install sed and try again."
fi

# Prompt for source directory
echo -e "${CYAN}Enter the source directory (where your YAML files are located):${NC}"
read -r SOURCE_DIR

# Check if source directory exists
if [ ! -d "$SOURCE_DIR" ]; then
  error "The directory '$SOURCE_DIR' does not exist."
fi

# Prompt for test directory
echo -e "${CYAN}Enter the test directory (where modified files will be saved):${NC}"
read -r TEST_DIR

# Prompt for backup directory
echo -e "${CYAN}Enter the backup directory (where original files will be backed up):${NC}"
read -r BACKUP_DIR

# Confirm directory creation
echo -e "${YELLOW}The following directories will be created:${NC}"
echo -e "Test Directory: ${CYAN}$TEST_DIR${NC}"
echo -e "Backup Directory: ${CYAN}$BACKUP_DIR${NC}"
echo -e "${YELLOW}Are you sure you want to create these directories? (y/n):${NC}"
read -r CONFIRM_CREATE

if [[ "$CONFIRM_CREATE" =~ ^[Yy]$ ]]; then
  # Create the test and backup directory structures
  info "Creating test and backup directory structures..."
  mkdir -p "$TEST_DIR" || error "Failed to create test directory."
  mkdir -p "$BACKUP_DIR" || error "Failed to create backup directory."
else
  info "Directory creation aborted. Exiting script."
  exit 0
fi

# Backup original YAML files
info "Backing up original YAML files..."
find "$SOURCE_DIR" -type f \( -name '*.yml' -o -name '*.yaml' \) | while read -r file; do
  # Define the relative path for the backup directory
  RELATIVE_PATH="${file#$SOURCE_DIR/}"
  BACKUP_FILE="$BACKUP_DIR/$RELATIVE_PATH"

  # Create the directory structure in the backup directory
  mkdir -p "$(dirname "$BACKUP_FILE")" || error "Failed to create directory structure in backup directory."

  # Copy the original file to the backup directory
  cp "$file" "$BACKUP_FILE" || error "Failed to backup: $file"
  success "Backed up: $file → $BACKUP_FILE"
done

# Find all YAML files and process them
info "Processing YAML files..."
find "$SOURCE_DIR" -type f \( -name '*.yml' -o -name '*.yaml' \) | while read -r file; do
  # Define the relative path for the test directory
  RELATIVE_PATH="${file#$SOURCE_DIR/}"
  TEST_FILE="$TEST_DIR/$RELATIVE_PATH"

  # Create the directory structure in the test directory
  mkdir -p "$(dirname "$TEST_FILE")" || error "Failed to create directory structure in test directory."

  # Use sed to modify the specific URL
  sed -E 's|https://cdn\.jsdelivr\.net/gh/walkxcode/dashboard-icons/png/(.*)\.png|https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/webp/\1.webp|g' "$file" > "$TEST_FILE"

  # Check if the file was modified successfully
  if [ $? -eq 0 ]; then
    success "Processed: $file → $TEST_FILE"
  else
    warning "Failed to process: $file"
  fi
done

# Ask the user if they want to preview the changes
echo -e "${YELLOW}Do you want to preview the changes before overwriting? (y/n):${NC}"
read -r PREVIEW

if [[ "$PREVIEW" =~ ^[Yy]$ ]]; then
  info "Here are the changes:"
  find "$TEST_DIR" -type f \( -name '*.yml' -o -name '*.yaml' \) | while read -r test_file; do
    echo -e "${CYAN}=== ${test_file} ===${NC}"
    cat "$test_file"
    echo -e "\n"
  done
fi

# Ask for confirmation before overwriting original files
echo -e "${YELLOW}Do you want to overwrite the original files with the modified ones? (y/n):${NC}"
read -r OVERWRITE

if [[ "$OVERWRITE" =~ ^[Yy]$ ]]; then
  info "Overwriting original files..."
  rsync -av --remove-source-files "$TEST_DIR/" "$SOURCE_DIR/" || error "Failed to overwrite original files."
  success "Original files have been overwritten."
else
  info "Original files were not overwritten. Modified files are located in: $TEST_DIR"
fi

echo -e "${GREEN}Script execution complete.${NC}"

# -----------------------------------------------------------------------------
# License
# -----------------------------------------------------------------------------

# Dashboard Icons Script
# <https://github.com/engels74/arrsenal-of-scripts>
# This script modifies the URL of dashboard icons in YAML files
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
