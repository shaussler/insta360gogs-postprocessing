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
  --target TARGET        Output preset for target device (default: tv-4k)
                           tv-4k       3840x2160 16:9   (4K TV)
                           tv-2k       2560x1440 16:9   (2K TV)
                           galaxy-s11  2560x1600 16:10  (Samsung Tab S11)
                           ipad        2732x2048 4:3    (iPad)
                           phone       1080x1920 9:16   (Phone vertical)
                           instagram   1080x1080 1:1    (Instagram square)
                           reel        1080x1350 4:5    (TikTok/Reels portrait)
  --fov MODE             Override default FOV for target (default: per target)
                           ultra   Ultra-wide (~170°), some edge distortion
                           mega    MegaView (~150°), reduced vertical distortion
                           dewarp  Dewarp (~130°), minimal distortion
                           linear  Linear (~110°), natural perspective
  --no-stabilize         Skip Gyroflow stabilization; just convert to H265 and scale
  --quality LEVEL        Encoding quality (default: excellent)
                           max        CRF 18, slow, 20M gyro  — visually lossless
                           excellent  CRF 20, slow, 16M gyro  — indistinguishable
                           good       CRF 24, fast,  8M gyro  — great, smaller files
                           acceptable CRF 28, fast,  5M gyro  — noticeable on close look
EOF
}

TARGET="tv-4k"
NO_STABILIZE=0
QUALITY="excellent"
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
        --no-stabilize)
            NO_STABILIZE=1
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

# FOV filter
FOV_FILTER=""
case "$FOV" in
    ultra)  FOV_FILTER="v360=fisheye:flat:ih_fov=170:iv_fov=170" ;;
    mega)   FOV_FILTER="v360=fisheye:flat:ih_fov=150:iv_fov=150" ;;
    dewarp) FOV_FILTER="v360=fisheye:flat:ih_fov=130:iv_fov=130" ;;
    linear) FOV_FILTER="v360=fisheye:flat:ih_fov=110:iv_fov=110" ;;
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
    ffprobe -v error -select_streams v:0 \
      -show_entries stream=width \
      -of csv=p=0 "$1" | head -1
}

detect_height() {
    ffprobe -v error -select_streams v:0 \
      -show_entries stream=height \
      -of csv=p=0 "$1" | head -1
}

get_creation_time() {
    ffprobe -v error -show_entries format_tags=creation_time -of csv=p=0 "$1"
}

get_duration_secs() {
    ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" | xargs printf "%.0f"
}

# Build base name from metadata: yyyymmdd-hhmmss-NNNNNs
creation_time=$(get_creation_time "$file")
if [ -z "$creation_time" ]; then
    echo "ERROR: missing creation_time metadata in $filename" >&2
    exit 1
fi
# Format: 2026-09-12T16:05:21.000000Z → 20260912-160521
dt_label=$(echo "$creation_time" | sed 's/-//g;s/T/-/;s/://g;s/\..*//')
duration_secs=$(printf "%05d" "$(get_duration_secs "$file")")
base="${dt_label}-${duration_secs}s"

# Determine pair: look for _00_ ↔ _10_ variants
pair_file="${file/_00_/_10_}"
if [ "$file" = "$pair_file" ]; then
    # Not a _00_ file — check if this IS a _10_ file
    pair_file="${file/_10_/_00_}"
    if [ -f "$pair_file" ]; then
        file="$pair_file"  # prefer the _00_ file as primary
    fi
fi

# CASE 1: Dual-Lens 360 Video
if [ -f "$pair_file" ]; then
    lens_w=$(detect_width "$file")
    hstack_w=$((lens_w * 2))

    # For 360: stitch, convert fisheye to equirect, then scale
    vfilter="[0:v][1:v]hstack=inputs=2,v360=dfisheye:equirect:ih_fov=180:iv_fov=180,scale=${OUT_W}:${OUT_H}"

    out_file="$DEST_DIR/${base}.${TARGET}.${QUALITY}.${FOV}.360.mp4"

    if [ -f "$out_file" ]; then
        echo "Skipping existing file: $out_file"
        exit 0
    fi

    echo "[360 Pair] Stitching and compressing: $filename + $(basename "$pair_file")"

    ffmpeg -y -i "$file" -i "$pair_file" \
      -filter_complex "$vfilter" \
      -c:v libx265 -crf $X265_CRF -preset $X265_PRESET -pix_fmt yuv420p \
      -c:a aac -b:a $AAC_BITRATE \
      "$out_file" \
      || { echo "ERROR: ffmpeg failed for $filename" >&2; exit 1; }
    [ -f "$out_file" ] || { echo "ERROR: output file not created: $out_file" >&2; exit 1; }
    echo "OK: $out_file"

# CASE 2: Single-Lens / FreeFrame Video
else
    src_w=$(detect_width "$file")
    src_h=$(detect_height "$file")

    # Build crop filter for target aspect (after FOV conversion)
    crop_filter=""
    case "$ASPECT" in
        16:9)  crop_filter="crop=min(iw\\,ih*16/9):min(iw*9/16\\,ih)" ;;
        16:10) crop_filter="crop=min(iw\\,ih*16/10):min(iw*10/16\\,ih)" ;;
        4:3)   crop_filter="crop=min(iw\\,ih*4/3):min(iw*3/4\\,ih)" ;;
        1:1)   crop_filter="crop=min(iw\\,ih):min(iw\\,ih)" ;;
        4:5)   crop_filter="crop=min(iw\\,ih*4/5):min(iw*5/4\\,ih)" ;;
        9:16)  crop_filter="crop=min(iw\\,ih*9/16):min(iw*16/9\\,ih)" ;;
    esac

    # Build scale filter for target resolution
    scale_filter="scale=${OUT_W}:${OUT_H}:force_original_aspect_ratio=decrease"

    # Build gyroflow export dimensions
    gyro_w=$OUT_W
    gyro_h=$OUT_H

    # Construct filter chain: FOV → crop → scale
    vfilter=""
    if [ -n "$FOV_FILTER" ]; then
        vfilter="$FOV_FILTER"
    fi
    if [ -n "$crop_filter" ]; then
        if [ -n "$vfilter" ]; then
            vfilter="${vfilter},${crop_filter}"
        else
            vfilter="$crop_filter"
        fi
    fi
    if [ -n "$scale_filter" ]; then
        if [ -n "$vfilter" ]; then
            vfilter="${vfilter},${scale_filter}"
        else
            vfilter="$scale_filter"
        fi
    fi

    # --no-stabilize: skip Gyroflow stabilization
    if [ "$NO_STABILIZE" -eq 1 ]; then
        out_file="$DEST_DIR/${base}.${TARGET}.${QUALITY}.${FOV}.unstabilized.mp4"
        if [ -f "$out_file" ]; then
            echo "Skipping existing file: $out_file"
            exit 0
        fi

        echo "[Single Lens] Converting to H265 (no stabilization): $filename"

        if [ -n "$vfilter" ]; then
            ffmpeg -y -i "$file" \
              -vf "$vfilter" \
              -c:v libx265 -crf $X265_CRF -preset $X265_PRESET -pix_fmt yuv420p \
              -c:a aac -b:a $AAC_BITRATE \
              "$out_file" \
              || { echo "ERROR: ffmpeg failed for $filename" >&2; exit 1; }
        else
            ffmpeg -y -i "$file" \
              -c:v libx265 -crf $X265_CRF -preset $X265_PRESET -pix_fmt yuv420p \
              -c:a aac -b:a $AAC_BITRATE \
              "$out_file" \
              || { echo "ERROR: ffmpeg failed for $filename" >&2; exit 1; }
        fi
        [ -f "$out_file" ] || { echo "ERROR: output file not created: $out_file" >&2; exit 1; }
        echo "OK: $out_file"
        exit 0
    fi

    out_file="$DEST_DIR/${base}.${TARGET}.${QUALITY}.${FOV}.stabilized.mp4"
    if [ -f "$out_file" ]; then
        echo "Skipping existing file: $out_file"
        exit 0
    fi

    echo "[Single Lens] Stabilizing with Gyroflow CLI: $filename"

    # When FOV/crop/scale is needed, gyroflow outputs to temp file, then ffmpeg applies filters
    if [ -n "$vfilter" ]; then
        gyro_out="${out_file%.mp4}.gyroflow_tmp.mp4"
    else
        gyro_out="$out_file"
    fi

    gyro_args=(--output "$gyro_out" --codec libx265 --bitrate $GYRO_BITRATE --auto-sync)
    if [ -n "$gyro_h" ]; then
        gyro_args+=(--preset-export-width "$gyro_w" --preset-export-height "$gyro_h")
    fi

    set +e
    gyroflow "$file" "${gyro_args[@]}"
    gyroflow_rc=$?
    set -e

    if [ $gyroflow_rc -ne 0 ] || [ ! -f "$gyro_out" ]; then
        echo "WARNING: Gyroflow failed (exit $gyroflow_rc) or no output. Falling back to simple FFmpeg encode..."
        rm -f "$gyro_out"
        if [ -n "$vfilter" ]; then
            ffmpeg -y -i "$file" \
              -vf "$vfilter" \
              -c:v libx265 -crf $X265_CRF -preset $X265_PRESET -pix_fmt yuv420p \
              -c:a aac -b:a $AAC_BITRATE \
              "$out_file" \
              || { echo "ERROR: ffmpeg fallback also failed for $filename" >&2; exit 1; }
        else
            ffmpeg -y -i "$file" \
              -c:v libx265 -crf $X265_CRF -preset $X265_PRESET -pix_fmt yuv420p \
              -c:a aac -b:a $AAC_BITRATE \
              "$out_file" \
              || { echo "ERROR: ffmpeg fallback also failed for $filename" >&2; exit 1; }
        fi
    elif [ -n "$vfilter" ]; then
        # Apply FOV/crop/scale filters to gyroflow output
        echo "[Single Lens] Applying filters: FOV=$FOV → crop → scale"
        ffmpeg -y -i "$gyro_out" \
          -vf "$vfilter" \
          -c:v libx265 -crf $X265_CRF -preset $X265_PRESET -pix_fmt yuv420p \
          -c:a aac -b:a $AAC_BITRATE \
          "$out_file" \
          || { echo "ERROR: ffmpeg filter failed for $filename" >&2; rm -f "$gyro_out"; exit 1; }
        rm -f "$gyro_out"
    fi

    [ -f "$out_file" ] || { echo "ERROR: output file not created: $out_file" >&2; exit 1; }
    echo "OK: $out_file"
fi
