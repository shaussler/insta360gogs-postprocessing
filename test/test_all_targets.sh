#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEST_INPUT="$(dirname "$0")/test.mp4"
TEST_OUTPUT="$(dirname "$0")/output"

rm -rf "$TEST_OUTPUT"
mkdir -p "$TEST_OUTPUT"

targets="tv-2k galaxy-s11 ipad phone instagram reel"

for target in $targets; do
    echo "=== Testing target: $target ==="
    "$SCRIPT_DIR/convert_one.sh" --target "$target" "$TEST_INPUT" "$TEST_OUTPUT"
    echo
done

echo "All targets completed!"
ls -lh "$TEST_OUTPUT"
