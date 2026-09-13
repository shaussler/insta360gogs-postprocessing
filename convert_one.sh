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
  --test                 Dry-run: show what would be processed without encoding
  --resolution RES       Output resolution (default: keep original)
                           keep   Keep original resolution
                           720p   1280 wide (360: 1280x640, single: 1280x720)
                           1080p  1920 wide (360: 1920x960, single: 1920x1080)
                           2k     2560 wide (360: 2560x1280, single: 2560x1440)
                           4k     3840 wide (360: 4096x2048, single: 3840x2160)
  --aspect RATIO         Crop to aspect ratio (default: 16:9)
                           16:9    Widescreen
                           4:3     Tablet / iPad
                           1:1     Instagram square
                           4:5     Instagram portrait
                           9:16    TikTok / Reels vertical
                           keep    Keep original aspect ratio
  --no-stabilize         Skip Gyroflow stabilization; just convert to H265 and scale
  --fov MODE             Field of view conversion (default: none, keeps native fisheye)
                           ultra   Ultra-wide (~170°), some edge distortion
                           mega    MegaView (~150°), reduced vertical distortion
                           dewarp  Dewarp (~130°), minimal distortion
                           linear  Linear (~110°), natural perspective
  --quality LEVEL        Encoding quality (default: excellent)
                           max        CRF 18, slow, 20M gyro  — visually lossless
                           excellent  CRF 20, slow, 16M gyro  — indistinguishable
                           good       CRF 24, fast,  8M gyro  — great, smaller files
                           acceptable CRF 28, fast,  5M gyro  — noticeable on close look
EOF
}

TEST_MODE=0
FORCE_RES="keep"
ASPECT="16:9"
NO_STABILIZE=0
QUALITY="excellent"
FOV=""

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --test)
            TEST_MODE=1
            shift
            ;;
        --resolution)
            shift
            FORCE_RES="${1:-}"
            case "$FORCE_RES" in
                keep|720p|1080p|2k|4k) ;;
                *) echo "Error: --resolution must be keep, 720p, 1080p, 2k, or 4k" >&2; exit 1 ;;
            esac
            shift
            ;;
        --aspect)
            shift
            ASPECT="${1:-}"
            case "$ASPECT" in
                16:9|4:3|1:1|4:5|9:16|keep) ;;
                *) echo "Error: --aspect must be 16:9, 4:3, 1:1, 4:5, 9:16, or keep" >&2; exit 1 ;;
            esac
            shift
            ;;
        --no-stabilize)
            NO_STABILIZE=1
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

# FOV filter
FOV_FILTER=""
FOV_LABEL=""
case "$FOV" in
    ultra)  FOV_FILTER="v360=fisheye:flat:ih_fov=170:iv_fov=170"; FOV_LABEL="_${FOV}" ;;
    mega)   FOV_FILTER="v360=fisheye:flat:ih_fov=150:iv_fov=150"; FOV_LABEL="_${FOV}" ;;
    dewarp) FOV_FILTER="v360=fisheye:flat:ih_fov=130:iv_fov=130"; FOV_LABEL="_${FOV}" ;;
    linear) FOV_FILTER="v360=fisheye:flat:ih_fov=110:iv_fov=110"; FOV_LABEL="_${FOV}" ;;
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
base="${filename%.*}"

# Resolve helper functions
detect_width() {
    ffprobe -v error -select_streams v:0 \
      -show_entries stream=width \
      -of csv=p=0 "$1" | head -1
}

res_label_from_width() {
    local w="$1"
    if   [ "$w" -ge 3840 ]; then echo "4K"
    elif [ "$w" -ge 3000 ]; then echo "3K"
    elif [ "$w" -ge 2560 ]; then echo "QHD"
    elif [ "$w" -ge 1920 ]; then echo "FHD"
    elif [ "$w" -ge 1280 ]; then echo "HD"
    else echo "${w}w"
    fi
}

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
    RES_LABEL=$(res_label_from_width "$hstack_w")

    if [ "$FORCE_RES" != "keep" ]; then
        case "$FORCE_RES" in
            4k)   scale_w=4096; scale_h=2048 ;;
            2k)   scale_w=2560; scale_h=1280 ;;
            1080p) scale_w=1920; scale_h=960 ;;
            720p) scale_w=1280; scale_h=640 ;;
        esac
        RES_LABEL=$(echo "$FORCE_RES" | tr '[:lower:]' '[:upper:]')
        vfilter="[0:v][1:v]hstack=inputs=2,v360=dfisheye:equirect:ih_fov=180:iv_fov=180,scale=${scale_w}:${scale_h}"
    else
        vfilter="[0:v][1:v]hstack=inputs=2,v360=dfisheye:equirect:ih_fov=180:iv_fov=180"
    fi

    out_file="$DEST_DIR/${base}_${RES_LABEL}_${QUALITY}_360.mp4"

    if [ -f "$out_file" ]; then
        echo "Skipping existing file: $out_file"
        exit 0
    fi

    echo "[360 Pair] Stitching and compressing: $filename + $(basename "$pair_file")"

    if [ "$TEST_MODE" -eq 1 ]; then
        echo "  Would run: ffmpeg -y -i $file -i $pair_file ..."
        echo "  Output: $out_file"
        exit 0
    fi

    ffmpeg -y -i "$file" -i "$pair_file" \
      -filter_complex "$vfilter" \
      -c:v libx265 -crf $X265_CRF -preset $X265_PRESET \
      -c:a aac -b:a $AAC_BITRATE \
      "$out_file" \
      || { echo "ERROR: ffmpeg failed for $filename" >&2; exit 1; }
    [ -f "$out_file" ] || { echo "ERROR: output file not created: $out_file" >&2; exit 1; }
    echo "OK: $out_file"

# CASE 2: Single-Lens / FreeFrame Video
else
    src_w=$(detect_width "$file")
    RES_LABEL=$(res_label_from_width "$src_w")

    # Crop filter
    crop_filter=""
    if [ "$ASPECT" != "keep" ]; then
        case "$ASPECT" in
            16:9) crop_filter="crop=iw:iw*9/16" ;;
            4:3)  crop_filter="crop=iw:iw*3/4" ;;
            1:1)  crop_filter="crop=min(iw\\,ih):min(iw\\,ih)" ;;
            4:5)  crop_filter="crop=ih*4/5:ih" ;;
            9:16) crop_filter="crop=ih*9/16:ih" ;;
        esac
    fi

    ASPECT_LABEL=""
    if [ "$ASPECT" != "keep" ]; then
        ASPECT_LABEL="_$(echo "$ASPECT" | tr ':' 'x')"
    fi

    if [ "$FORCE_RES" != "keep" ]; then
        case "$FORCE_RES" in
            4k)    scale_w=3840; scale_h=2160 ;;
            2k)    scale_w=2560; scale_h=1440 ;;
            1080p) scale_w=1920; scale_h=1080 ;;
            720p)  scale_w=1280; scale_h=720 ;;
        esac
        RES_LABEL=$(echo "$FORCE_RES" | tr '[:lower:]' '[:upper:]')
        vfilter="scale=${scale_w}:${scale_h}:force_original_aspect_ratio=decrease"
        gyro_w=$scale_w
        gyro_h=$scale_h
    else
        vfilter=""
        gyro_w=$src_w
        gyro_h=""
    fi

    if [ -n "$crop_filter" ]; then
        if [ -n "$vfilter" ]; then
            vfilter="${crop_filter},${vfilter}"
        else
            vfilter="$crop_filter"
        fi
    fi

    # Prepend FOV filter if specified
    if [ -n "$FOV_FILTER" ]; then
        if [ -n "$vfilter" ]; then
            vfilter="${FOV_FILTER},${vfilter}"
        else
            vfilter="$FOV_FILTER"
        fi
    fi

    # --no-stabilize: skip Gyroflow stabilization
    if [ "$NO_STABILIZE" -eq 1 ]; then
        out_file="$DEST_DIR/${base}_${RES_LABEL}${ASPECT_LABEL}_${QUALITY}${FOV_LABEL}_unstabilized.mp4"
        if [ -f "$out_file" ]; then
            echo "Skipping existing file: $out_file"
            exit 0
        fi

        echo "[Single Lens] Converting to H265 (no stabilization): $filename"

        if [ "$TEST_MODE" -eq 1 ]; then
            echo "  Would run: ffmpeg -y -i $file ..."
            echo "  Output: $out_file"
            exit 0
        fi

        if [ -n "$vfilter" ]; then
            ffmpeg -y -i "$file" \
              -vf "$vfilter" \
              -c:v libx265 -crf $X265_CRF -preset $X265_PRESET \
              -c:a aac -b:a $AAC_BITRATE \
              "$out_file" \
              || { echo "ERROR: ffmpeg failed for $filename" >&2; exit 1; }
        else
            ffmpeg -y -i "$file" \
              -c:v libx265 -crf $X265_CRF -preset $X265_PRESET \
              -c:a aac -b:a $AAC_BITRATE \
              "$out_file" \
              || { echo "ERROR: ffmpeg failed for $filename" >&2; exit 1; }
        fi
        [ -f "$out_file" ] || { echo "ERROR: output file not created: $out_file" >&2; exit 1; }
        echo "OK: $out_file"
        exit 0
    fi

    out_file="$DEST_DIR/${base}_${RES_LABEL}${ASPECT_LABEL}_${QUALITY}${FOV_LABEL}_stabilized.mp4"
    if [ -f "$out_file" ]; then
        echo "Skipping existing file: $out_file"
        exit 0
    fi

    echo "[Single Lens] Stabilizing with Gyroflow CLI: $filename"

    if [ "$TEST_MODE" -eq 1 ]; then
        echo "  Would run: gyroflow $file --output $out_file ..."
        echo "  Output: $out_file"
        exit 0
    fi

    # When FOV is specified, gyroflow outputs to temp file, then ffmpeg applies FOV
    if [ -n "$FOV_FILTER" ]; then
        gyro_out="${out_file%.mp4}_gyroflow_tmp.mp4"
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
              -c:v libx265 -crf $X265_CRF -preset $X265_PRESET \
              -c:a aac -b:a $AAC_BITRATE \
              "$out_file" \
              || { echo "ERROR: ffmpeg fallback also failed for $filename" >&2; exit 1; }
        else
            ffmpeg -y -i "$file" \
              -c:v libx265 -crf $X265_CRF -preset $X265_PRESET \
              -c:a aac -b:a $AAC_BITRATE \
              "$out_file" \
              || { echo "ERROR: ffmpeg fallback also failed for $filename" >&2; exit 1; }
        fi
    elif [ -n "$FOV_FILTER" ]; then
        # Apply FOV filter to gyroflow output
        echo "[Single Lens] Applying FOV: $FOV"
        ffmpeg -y -i "$gyro_out" \
          -vf "$FOV_FILTER" \
          -c:v libx265 -crf $X265_CRF -preset $X265_PRESET \
          -c:a aac -b:a $AAC_BITRATE \
          "$out_file" \
          || { echo "ERROR: ffmpeg FOV filter failed for $filename" >&2; rm -f "$gyro_out"; exit 1; }
        rm -f "$gyro_out"
    fi

    [ -f "$out_file" ] || { echo "ERROR: output file not created: $out_file" >&2; exit 1; }
    echo "OK: $out_file"
fi
