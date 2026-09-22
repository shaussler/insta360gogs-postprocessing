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
  --target TARGET        Output preset (default: tv-2k)
                           tv-4k       3840x2160 16:9   mega   (4K TV)
                           tv-2k       2560x1440 16:9   mega   (2K TV)
                           galaxy-s11  2560x1600 16:10  mega   (Samsung Tab S11)
                           ipad        2732x2048 4:3    dewarp (iPad)
                           phone       1080x1920 9:16   ultra  (Phone vertical)
                           instagram   1080x1080 1:1    ultra  (Instagram square)
                           reel        1080x1350 4:5    ultra  (TikTok/Reels portrait)
                           raw         original  original none   (Raw H.265 passthrough)
  --fov MODE             Override default FOV for target (default: set by target)
                           ultra   Max FOV without black edges (slider 1.0)
                           mega    ~92% of max FOV (slider 0.92)
                           dewarp  ~85% of max FOV (slider 0.85)
                           linear  ~75% of max FOV (slider 0.75)
   --stabilization LEVEL  Gyroflow stabilization strength (default: high)
                            none      No stabilization, no Gyroflow processing
                            standard  Light smoothing, minimal crop
                            high      Moderate smoothing, moderate crop (recommended)
                            max       Maximum smoothing, heavy crop
   --quality LEVEL        Encoding quality (default: good)
                            max        CRF 18, slow, 20M gyro  — visually lossless
                            excellent  CRF 20, slow, 16M gyro  — indistinguishable
                            good       CRF 24, fast,  8M gyro  — great, smaller files
                            acceptable CRF 28, fast,  5M gyro  — noticeable on close look

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
        --target|--quality|--fov|--stabilization)
            PASSTHROUGH+=("$1")
            shift
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

    # Skip thumbnail/arb files
    [[ "$file" == *.arb ]] && continue

    # Error on second-lens files (_10_) — should not exist
    if [[ "$file" == *"_10_"* ]]; then
        echo "Error: unexpected second-lens file: $file" >&2
        exit 1
    fi

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
