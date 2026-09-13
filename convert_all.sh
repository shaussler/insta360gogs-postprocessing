#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

usage() {
    cat <<EOF
Usage: $0 [OPTIONS] <input_dir> <output_dir>

Batch-convert all Insta360 videos in a directory.
Calls convert_one.sh for each unique video.

Arguments:
  input_dir    Directory containing source videos
  output_dir   Directory for converted files (must already exist)

Options:
  -h, --help             Show this help message
  --target TARGET        Output preset (default: tv-4k)
                           tv-4k, tv-2k, galaxy-s11, ipad, phone, instagram, reel
  --fov MODE             Override default FOV for target (ultra, mega, dewarp, linear)
  --no-stabilize         Skip Gyroflow stabilization; just convert to H265 and scale
  --quality LEVEL        Encoding quality (default: excellent)

Run '$SCRIPT_DIR/convert_one.sh --help' for target/quality details.
EOF

# Passthrough options are forwarded as-is to convert_one.sh.
}

PASSTHROUGH=()

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --target|--quality|--fov)
            PASSTHROUGH+=("$1")
            shift
            PASSTHROUGH+=("$1")
            shift
            ;;
        --no-stabilize)
            PASSTHROUGH+=("$1")
            shift
            ;;
        *)
            break
            ;;
    esac
done

if [ $# -ne 2 ]; then
    echo "Error: expected input_dir and output_dir" >&2
    usage
    exit 1
fi

SRC_DIR="$1"
DEST_DIR="$2"

if [ ! -d "$SRC_DIR" ]; then
    echo "Error: input directory does not exist: $SRC_DIR" >&2
    exit 1
fi

if [ ! -d "$DEST_DIR" ]; then
    echo "Error: output directory does not exist: $DEST_DIR" >&2
    exit 1
fi

# Collect unique base names from all .insv/.mp4 files
shopt -s nullglob
declare -A seen
count=0

for file in "$SRC_DIR"/*; do
    [ -f "$file" ] || continue

    case "$file" in
        *.insv|*.mp4) ;;
        *) continue ;;
    esac

    # Skip low-res previews
    [[ "$file" == *"LRV_"* ]] && continue

    # Skip second-lens files (_10_) — handled with _00_ pair by convert_one.sh
    [[ "$file" == *"_10_"* ]] && continue

    filename=$(basename "$file")
    base="${filename%.*}"

    if [ -z "${seen[$base]+_}" ]; then
        seen[$base]=1
        count=$((count + 1))
        echo "Processing: $base"
        "$SCRIPT_DIR/convert_one.sh" "${PASSTHROUGH[@]}" "$file" "$DEST_DIR"
    fi
done

shopt -u nullglob

if [ "$count" -eq 0 ]; then
    echo "No video files found in $SRC_DIR"
fi

echo "Batch processing finished!"
