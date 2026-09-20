#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEST_DIR="$(dirname "$0")"
TEST_INPUT="$TEST_DIR/test.mp4"
TEST_OUTPUT="$TEST_DIR/output"
DEBUG_DIR="$TEST_DIR/debug"

rm -rf "$TEST_OUTPUT" "$DEBUG_DIR"
mkdir -p "$TEST_OUTPUT" "$DEBUG_DIR"

targets="${1:-instagram}"

for target in $targets; do
    echo "=== Testing target: $target ==="
    "$SCRIPT_DIR/convert_one.sh" --test --debug "$DEBUG_DIR" --target "$target" "$TEST_INPUT" "$TEST_OUTPUT"
    echo
done

echo "All targets completed!"
echo
echo "Output files:"
ls -lh "$TEST_OUTPUT"
echo
echo "Debug frames:"
ls -lh "$DEBUG_DIR"
