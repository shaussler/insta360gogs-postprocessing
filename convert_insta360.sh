#!/bin/bash
set -euo pipefail

usage() {
    cat <<EOF
Usage: $0 [OPTIONS] <input_dir> <output_dir>

Convert Insta360 footage (.insv/.mp4) to readable files:
  - Dual-lens 360 videos are stitched and scaled
  - Single-lens videos are stabilized (Gyroflow) and scaled

Default output keeps the original resolution.
Resolution is always included in the filename.

Arguments:
  input_dir    Directory containing source videos
  output_dir   Directory for converted files (must already exist)

Options:
  -h, --help             Show this help message
  --test                 Process only the first 2 files found of each type
                         (2 dual-lens 360 videos and 2 single-lens videos)
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
  --quality LEVEL        Encoding quality (default: excellent)
                           max        CRF 18, slow, 20M gyro  — visually lossless
                           excellent  CRF 20, slow, 16M gyro  — indistinguishable
                           good       CRF 24, fast,  8M gyro  — great, smaller files
                           acceptable CRF 28, fast,  5M gyro  — noticeable on close look

Examples:
  convert_insta360.sh /mnt/insta360 /mnt/converted
  convert_insta360.sh --resolution 4k /mnt/insta360 /mnt/converted
  convert_insta360.sh --resolution 1080p --aspect keep /mnt/insta360 /mnt/converted
  convert_insta360.sh --resolution 2k --aspect 4:5 /mnt/insta360 /mnt/converted
  convert_insta360.sh --resolution 720p --aspect 9:16 --quality good /mnt/insta360 /mnt/converted
EOF
}

TEST_MODE=0
FORCE_RES="keep"  # keep, 2k, 2.5k, 4k
ASPECT="16:9"  # default crop
NO_STABILIZE=0
QUALITY="excellent"

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

if [ $# -ne 2 ]; then
    echo "Error: expected input and output directories" >&2
    usage
    exit 1
fi

# Define source and destination directories
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

# Resolution detection helper
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

# mkdir -p "$DEST_DIR"

#shopt -s nullglob ignorecase

dual_count=0
single_count=0

for file in "$SRC_DIR"/*; do
    # Ignore non-regular files (e.g. subdirectories)
    if [ ! -f "$file" ]; then
        continue
    fi

    # Ignore anything that is not a video file
    case "$file" in
        *.insv|*.mp4) ;;
        *) echo "Ignoring non-video file (not .insv/.mp4): $file"; continue ;;
    esac

    # Skip low-res preview files
    if [[ "$file" == *"LRV_"* ]]; then
        echo "Ignoring low-res preview: $file"
        continue
    fi

    # Skip second-lens 360 files (_10_) as they get handled alongside _00_
    if [[ "$file" == *"_10_"* ]]; then
        echo "Ignoring second lens (_10_, handled with its _00_ pair): $file"
        continue
    fi

    filename=$(basename "$file")
    base="${filename%.*}"
    pair_file="${file/_00_/_10_}"

    # CASE 1: Dual-Lens 360 Video (Stitch via FFmpeg - Gyroflow is for single-lens stabilization)
    if [ -f "$pair_file" ]; then
        if [ $TEST_MODE -eq 1 ] && [ $dual_count -ge 2 ]; then
            continue
        fi
        dual_count=$((dual_count + 1))

        echo "[360 Pair] Stitching and compressing: $filename"

        # Detect input resolution from one lens
        lens_w=$(detect_width "$file")
        hstack_w=$((lens_w * 2))
        RES_LABEL=$(res_label_from_width "$hstack_w")

        # Build filter: stitch lenses, convert to equirect, optionally scale
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
            continue
        fi

        ffmpeg -y -i "$file" -i "$pair_file" \
          -filter_complex "$vfilter" \
          -c:v libx265 -crf $X265_CRF -preset $X265_PRESET \
          -c:a aac -b:a $AAC_BITRATE \
          "$out_file" \
          || { echo "ERROR: ffmpeg failed for $filename" >&2; exit 1; }
        [ -f "$out_file" ] || { echo "ERROR: output file not created: $out_file" >&2; exit 1; }
        echo "OK: $out_file"

    # CASE 2: Single-Lens / FreeFrame Video (Stabilize via Gyroflow CLI)
    else
        if [ $TEST_MODE -eq 1 ] && [ $single_count -ge 2 ]; then
            continue
        fi
        single_count=$((single_count + 1))

        # Detect input resolution
        src_w=$(detect_width "$file")
        RES_LABEL=$(res_label_from_width "$src_w")

        # Build crop filter based on aspect ratio
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

        # Aspect label for filename
        if [ "$ASPECT" = "keep" ]; then
            ASPECT_LABEL=""
        else
            ASPECT_LABEL="_$(echo $ASPECT | tr ':' 'x')"
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

        # Prepend crop filter if needed
        if [ -n "$crop_filter" ]; then
            if [ -n "$vfilter" ]; then
                vfilter="${crop_filter},${vfilter}"
            else
                vfilter="$crop_filter"
            fi
        fi

        # --no-stabilize: skip Gyroflow stabilization, just convert to H265
        if [ $NO_STABILIZE -eq 1 ]; then
            out_file="$DEST_DIR/${base}_${RES_LABEL}${ASPECT_LABEL}_${QUALITY}_unstabilized.mp4"
            if [ -f "$out_file" ]; then
                echo "Skipping existing file: $out_file"
                continue
            fi
            echo "[Single Lens] Converting to H265 (no stabilization): $filename"
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
            continue
        fi

        out_file="$DEST_DIR/${base}_${RES_LABEL}${ASPECT_LABEL}_${QUALITY}_stabilized.mp4"
        if [ -f "$out_file" ]; then
            echo "Skipping existing file: $out_file"
            continue
        fi

        echo "[Single Lens] Stabilizing with Gyroflow CLI: $filename"
        
        # Run Gyroflow CLI with auto-sync
        gyro_args=(--output "$out_file" --codec libx265 --bitrate $GYRO_BITRATE --auto-sync)
        if [ -n "$gyro_h" ]; then
            gyro_args+=(--preset-export-width "$gyro_w" --preset-export-height "$gyro_h")
        fi

        set +e
        gyroflow "$file" "${gyro_args[@]}"
        gyroflow_rc=$?
        set -e

        if [ $gyroflow_rc -ne 0 ] || [ ! -f "$out_file" ]; then
            echo "WARNING: Gyroflow failed (exit $gyroflow_rc) or no output. Falling back to simple FFmpeg encode..."
            rm -f "$out_file"
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
        fi

        [ -f "$out_file" ] || { echo "ERROR: output file not created: $out_file" >&2; exit 1; }
        echo "OK: $out_file"
    fi
done

echo "Processing finished!"

