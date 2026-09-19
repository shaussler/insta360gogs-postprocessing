#!/bin/bash
set -euo pipefail

usage() {
    cat <<EOF
Usage: $0 [OPTIONS] <input_file> <output_dir>

Convert a single Insta360 video.

Arguments:
  input_file   Path to input video file (e.g. /path/to/PRO_VID_20260912_180521_00_012.mp4)
  output_dir   Directory for converted files (must already exist)

Options:
  -h, --help             Show this help message
  --target TARGET        Output preset for target device (default: tv-2k)
                           tv-4k       3840x2160 16:9   ultra  (4K TV)
                           tv-2k       2560x1440 16:9   ultra  (2K TV)
                           galaxy-s11  2560x1600 16:10  mega   (Samsung Tab S11)
                           ipad        2732x2048 4:3    dewarp (iPad)
                           phone       1080x1920 9:16   linear (Phone vertical)
                           instagram   1080x1080 1:1    linear (Instagram square)
                           reel        1080x1350 4:5    linear (TikTok/Reels portrait)
  --fov MODE             Override default FOV for target (default: set by target)
                           ultra   Maximum view (~120°), full sensor coverage
                           mega    Wide view (~100°), mild edge stretching
                           dewarp  Balanced (~90°), minimal distortion
                           linear  Natural perspective (~75°), tightest crop
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
EOF
}

TARGET="tv-2k"
NO_STABILIZE=0
STABILIZATION="high"
QUALITY="good"
FOV=""

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --target)
            shift
            TARGET="${1:-}"
            case "$TARGET" in
                tv-4k|tv-2k|galaxy-s11|ipad|phone|instagram|reel) ;;
                *) echo "Error: --target must be tv-4k, tv-2k, galaxy-s11, ipad, phone, instagram, or reel" >&2; exit 1 ;;
            esac
            shift
            ;;
        --fov)
            shift
            FOV="${1:-}"
            case "$FOV" in
                ultra|mega|dewarp|linear) ;;
                *) echo "Error: --fov must be ultra, mega, dewarp, or linear" >&2; exit 1 ;;
            esac
            shift
            ;;
        --stabilization)
            shift
            STABILIZATION="${1:-}"
            case "$STABILIZATION" in
                none)   NO_STABILIZE=1 ;;
                standard|high|max) NO_STABILIZE=0 ;;
                *) echo "Error: --stabilization must be none, standard, high, or max" >&2; exit 1 ;;
            esac
            shift
            ;;
        --quality)
            shift
            QUALITY="${1:-}"
            case "$QUALITY" in
                max|excellent|good|acceptable) ;;
                *) echo "Error: --quality must be max, excellent, good, or acceptable" >&2; exit 1 ;;
            esac
            shift
            ;;
        *)
            break
            ;;
    esac
done

# Quality settings
case "$QUALITY" in
    max)
        X265_CRF=18
        X265_PRESET="slow"
        AAC_BITRATE="192k"
        GYRO_BITRATE=20
        ;;
    excellent)
        X265_CRF=20
        X265_PRESET="slow"
        AAC_BITRATE="192k"
        GYRO_BITRATE=16
        ;;
    good)
        X265_CRF=24
        X265_PRESET="fast"
        AAC_BITRATE="128k"
        GYRO_BITRATE=8
        ;;
    acceptable)
        X265_CRF=28
        X265_PRESET="fast"
        AAC_BITRATE="96k"
        GYRO_BITRATE=5
        ;;
esac

# Target presets: resolution, aspect, default FOV
case "$TARGET" in
    tv-4k)      OUT_W=3840; OUT_H=2160; ASPECT="16:9";  DEF_FOV="ultra"  ;;
    tv-2k)      OUT_W=2560; OUT_H=1440; ASPECT="16:9";  DEF_FOV="ultra"  ;;
    galaxy-s11) OUT_W=2560; OUT_H=1600; ASPECT="16:10"; DEF_FOV="mega"   ;;
    ipad)       OUT_W=2732; OUT_H=2048; ASPECT="4:3";   DEF_FOV="dewarp" ;;
    phone)      OUT_W=1080; OUT_H=1920; ASPECT="9:16";  DEF_FOV="linear" ;;
    instagram)  OUT_W=1080; OUT_H=1080; ASPECT="1:1";   DEF_FOV="linear" ;;
    reel)       OUT_W=1080; OUT_H=1350; ASPECT="4:5";   DEF_FOV="linear" ;;
esac

# Use explicit FOV if provided, otherwise use target default
if [ -z "$FOV" ]; then
    FOV="$DEF_FOV"
fi

# FOV mode for defish_insta360.py
case "$FOV" in
    ultra|mega|dewarp|linear) ;;
    *) echo "ERROR: internal error -- unknown FOV value: $FOV" >&2; exit 1 ;;
esac

# Stabilization presets for Gyroflow (smoothness values)
# Maps to Insta360 FlowState levels: none/standard/high/max
STAB_PRESET=""
case "$STABILIZATION" in
    none)      STAB_PRESET="" ;;
    standard)  STAB_PRESET="{ 'version': 2, 'stabilization': { 'smoothing_params': [{ 'name': 'smoothness', 'value': 0.25 }] }}" ;;
    high)      STAB_PRESET="" ;;  # Gyroflow default (smoothness=0.5) matches "high"
    max)       STAB_PRESET="{ 'version': 2, 'stabilization': { 'smoothing_params': [{ 'name': 'smoothness', 'value': 1.0 }] }}" ;;
    *)         echo "ERROR: internal error -- unknown stabilization level: $STABILIZATION" >&2; exit 1 ;;
esac

if [ $# -ne 2 ]; then
    echo "Error: expected input_file and output_dir" >&2
    usage
    exit 1
fi

file="$1"
DEST_DIR="$2"

if [ ! -f "$file" ]; then
    echo "Error: input file does not exist: $file" >&2
    exit 1
fi

if [ ! -d "$DEST_DIR" ]; then
    echo "Error: output directory does not exist: $DEST_DIR" >&2
    exit 1
fi

filename=$(basename "$file")

# Resolve helper functions
detect_width() {
    local val
    val=$(ffprobe -v warning -select_streams v:0 \
      -show_entries stream=width \
      -of csv=p=0 "$1")
    printf '%s' "${val%%$'\n'*}"
}

detect_height() {
    local val
    val=$(ffprobe -v warning -select_streams v:0 \
      -show_entries stream=height \
      -of csv=p=0 "$1")
    printf '%s' "${val%%$'\n'*}"
}

get_creation_time() {
    ffprobe -v warning -show_entries format_tags=creation_time -of csv=p=0 "$1"
}

get_duration_secs() {
    local raw
    raw=$(ffprobe -v warning -show_entries format=duration -of csv=p=0 "$1")
    if [[ -z "$raw" || ! "$raw" =~ ^[0-9]+\.?[0-9]*$ ]]; then
        echo "ERROR: could not parse duration from ffprobe: '$raw'" >&2
        return 1
    fi
    printf "%.0f" "$raw"
}

# Build base name from metadata: yyyymmdd-hhmmss-NNNNNs
creation_time=$(get_creation_time "$file")
if [ -z "$creation_time" ]; then
    echo "ERROR: missing creation_time metadata in $filename" >&2
    exit 1
fi
if [[ ! "$creation_time" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2} ]]; then
    echo "ERROR: unexpected creation_time format in $filename: $creation_time" >&2
    exit 1
fi
# Format: 2026-09-12T16:05:21.000000Z → 20260912-160521
dt_label=$(echo "$creation_time" | sed 's/-//g;s/T/-/;s/://g;s/\..*//')
duration_secs=$(printf "%05d" "$(get_duration_secs "$file")")
base="${dt_label}-${duration_secs}s"

# Determine pair: look for _00_ ↔ _10_ variants
pair_file=""
if [[ "$file" == *_00_* ]]; then
    pair_file="${file/_00_/_10_}"
    [ -f "$pair_file" ] || pair_file=""
elif [[ "$file" == *_10_* ]]; then
    pair_file="${file/_10_/_00_}"
    if [ -f "$pair_file" ]; then
        file="$pair_file"  # prefer the _00_ file as primary
    else
        pair_file=""
    fi
fi

# CASE 1: Dual-Lens 360 Video
if [ -n "$pair_file" ]; then
    lens_w=$(detect_width "$file")
    if [ -z "$lens_w" ]; then
        echo "ERROR: could not detect video width for $filename" >&2
        exit 1
    fi

    # For 360: stitch, convert fisheye to equirect, then scale
    vfilter="[0:v][1:v]hstack=inputs=2,v360=dfisheye:equirect:ih_fov=180:iv_fov=180,scale=${OUT_W}:${OUT_H}"

    out_file="$DEST_DIR/${base}.${TARGET}.${QUALITY}.${FOV}.360.mp4"

    if [ -f "$out_file" ]; then
        echo "Skipping existing file: $out_file"
        exit 0
    fi

    echo "[360 Pair] Stitching and compressing: $filename + $(basename "$pair_file")"

    ffmpeg_stderr=$(mktemp)
    ffmpeg -y -i "$file" -i "$pair_file" \
      -filter_complex "$vfilter" \
      -c:v libx265 -crf "$X265_CRF" -preset "$X265_PRESET" -pix_fmt yuv420p \
      -c:a aac -b:a "$AAC_BITRATE" \
      "$out_file" 2>"$ffmpeg_stderr" \
      || { echo "ERROR: ffmpeg failed for $filename" >&2; cat "$ffmpeg_stderr" >&2; rm -f "$ffmpeg_stderr"; exit 1; }
    rm -f "$ffmpeg_stderr"
    [ -f "$out_file" ] || { echo "ERROR: output file not created: $out_file" >&2; exit 1; }
    echo "OK: $out_file"

# CASE 2: Single-Lens / FreeFrame Video
else
    src_w=$(detect_width "$file")
    src_h=$(detect_height "$file")
    if [ -z "$src_w" ] || [ -z "$src_h" ]; then
        echo "ERROR: could not detect video dimensions for $filename" >&2
        exit 1
    fi

    # Detect Insta360 metadata records (0x01 metadata, 0x03 gyro, 0x04 exposure)
    SCRIPT_DIR_DETECT="$(cd "$(dirname "$0")" && pwd)"
    detect_result=$(python3 "$SCRIPT_DIR_DETECT/detect_insta360.py" "$file" 2>/dev/null || true)
    can_process=$(echo "$detect_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('can_process', False))" 2>/dev/null || echo "False")

    if [ "$can_process" = "False" ]; then
        # Missing required Insta360 records — skip stabilization and de-fishing
        # Use "original" for both in the filename
        EFFECTIVE_STABILIZATION="original"
        EFFECTIVE_FOV="original"
        NO_STABILIZE=1
        echo "[Single Lens] Missing Insta360 metadata (gyro/lens cal) — skipping stabilization and de-fishing: $filename"
    else
        EFFECTIVE_STABILIZATION="$STABILIZATION"
        EFFECTIVE_FOV="$FOV"
    fi

    # Build crop filter for target aspect (after de-fishing)
    crop_filter=""
    case "$ASPECT" in
        16:9)  crop_filter="crop=min(iw\\,ih*16/9):min(iw*9/16\\,ih)" ;;
        16:10) crop_filter="crop=min(iw\\,ih*16/10):min(iw*10/16\\,ih)" ;;
        4:3)   crop_filter="crop=min(iw\\,ih*4/3):min(iw*3/4\\,ih)" ;;
        1:1)   crop_filter="crop=min(iw\\,ih):min(iw\\,ih)" ;;
        4:5)   crop_filter="crop=min(iw\\,ih*4/5):min(iw*5/4\\,ih)" ;;
        9:16)  crop_filter="crop=min(iw\\,ih*9/16):min(iw*16/9\\,ih)" ;;
        *)     echo "ERROR: internal error -- unknown aspect ratio: $ASPECT" >&2; exit 1 ;;
    esac

    # Build scale filter for target resolution
    scale_filter="scale=${OUT_W}:${OUT_H}:force_original_aspect_ratio=decrease,pad=${OUT_W}:${OUT_H}:(ow-iw)/2:(oh-ih)/2:black"

    # Post-defish filter chain: crop → scale
    post_filter="${crop_filter},${scale_filter}"

    SCRIPT_DIR_DEFISH="$(cd "$(dirname "$0")" && pwd)"

    # --stabilization none: skip Gyroflow stabilization
    if [ "$NO_STABILIZE" -eq 1 ]; then
        out_file="$DEST_DIR/${base}.${TARGET}.${QUALITY}.${EFFECTIVE_FOV}.${EFFECTIVE_STABILIZATION}.mp4"
        if [ -f "$out_file" ]; then
            echo "Skipping existing file: $out_file"
            exit 0
        fi

        echo "[Single Lens] De-fishing and converting (stabilization: $EFFECTIVE_STABILIZATION, fov: $EFFECTIVE_FOV): $filename"

        if [ "$EFFECTIVE_FOV" = "original" ]; then
            # No metadata — just crop+scale, no de-fishing
            ffmpeg_stderr=$(mktemp)
            ffmpeg -y -i "$file" \
              -vf "format=yuv420p,${post_filter}" \
              -c:v libx265 -crf "$X265_CRF" -preset "$X265_PRESET" -pix_fmt yuv420p \
              -c:a aac -b:a "$AAC_BITRATE" \
              "$out_file" 2>"$ffmpeg_stderr" \
              || { echo "ERROR: ffmpeg failed for $filename" >&2; cat "$ffmpeg_stderr" >&2; rm -f "$ffmpeg_stderr"; exit 1; }
            rm -f "$ffmpeg_stderr"
        else
            # Pipe defish output to ffmpeg for crop+scale+encode
            ffmpeg_stderr=$(mktemp)
            python3 "$SCRIPT_DIR_DEFISH/defish_insta360.py" --fov "$EFFECTIVE_FOV" -i "$file" -o - | \
              ffmpeg -y -f rawvideo -video_size ${src_w}x${src_h} -pix_fmt bgr24 -i pipe:0 \
                -i "$file" \
                -filter_complex "[0:v]format=yuv420p,${post_filter}[v]" \
                -map "[v]" -map 1:a \
                -c:v libx265 -crf "$X265_CRF" -preset "$X265_PRESET" -pix_fmt yuv420p \
                -c:a aac -b:a "$AAC_BITRATE" \
                "$out_file" 2>"$ffmpeg_stderr" \
                || { echo "ERROR: de-fish+ffmpeg failed for $filename" >&2; cat "$ffmpeg_stderr" >&2; rm -f "$ffmpeg_stderr"; exit 1; }
            rm -f "$ffmpeg_stderr"
        fi
        [ -f "$out_file" ] || { echo "ERROR: output file not created: $out_file" >&2; exit 1; }
        echo "OK: $out_file"
        exit 0
    fi

    out_file="$DEST_DIR/${base}.${TARGET}.${QUALITY}.${EFFECTIVE_FOV}.${EFFECTIVE_STABILIZATION}.mp4"
    if [ -f "$out_file" ]; then
        echo "Skipping existing file: $out_file"
        exit 0
    fi

    echo "[Single Lens] Stabilizing with Gyroflow (level: $EFFECTIVE_STABILIZATION): $filename"

    gyro_out_dir="$(dirname "$file")"
    gyro_out_name="$(basename "${file%.*}")_stabilized.mp4"
    gyro_out="${gyro_out_dir}/${gyro_out_name}"

    gyro_params="{ 'codec': 'H.265/HEVC', 'bitrate': ${GYRO_BITRATE} }"
    gyro_args=(-p "$gyro_params" -t "_stabilized" -f --no-gpu-decoding)
    if [ -n "$STAB_PRESET" ]; then
        gyro_args+=(--preset "$STAB_PRESET")
    fi

    gyro_stderr=$(mktemp)
    if ! NO_OPENCL=1 gyroflow "$file" "${gyro_args[@]}" 2>"$gyro_stderr"; then
        echo "ERROR: Gyroflow failed for $filename (exit code $?)" >&2
        cat "$gyro_stderr" >&2
        rm -f "$gyro_stderr"
        exit 1
    fi
    rm -f "$gyro_stderr"
    [ -f "$gyro_out" ] || { echo "ERROR: Gyroflow produced no output: $gyro_out" >&2; exit 1; }

    # Get stabilized video dimensions
    stab_w=$(detect_width "$gyro_out")
    stab_h=$(detect_height "$gyro_out")

    echo "[Single Lens] De-fishing stabilized output (fov: $EFFECTIVE_FOV)"
    ffmpeg_stderr=$(mktemp)
    python3 "$SCRIPT_DIR_DEFISH/defish_insta360.py" --fov "$EFFECTIVE_FOV" --source "$file" -i "$gyro_out" -o - | \
      ffmpeg -y -f rawvideo -video_size ${stab_w}x${stab_h} -pix_fmt bgr24 -i pipe:0 \
        -i "$gyro_out" \
        -filter_complex "[0:v]format=yuv420p,${post_filter}[v]" \
        -map "[v]" -map 1:a \
        -c:v libx265 -crf "$X265_CRF" -preset "$X265_PRESET" -pix_fmt yuv420p \
        -c:a aac -b:a "$AAC_BITRATE" \
        "$out_file" 2>"$ffmpeg_stderr" \
        || { echo "ERROR: de-fish+ffmpeg failed for $filename" >&2; cat "$ffmpeg_stderr" >&2; rm -f "$ffmpeg_stderr" "$gyro_out"; exit 1; }
    rm -f "$ffmpeg_stderr" "$gyro_out"

    [ -f "$out_file" ] || { echo "ERROR: output file not created: $out_file" >&2; exit 1; }
    echo "OK: $out_file"
fi
