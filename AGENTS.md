# AGENTS.md

Two scripts process Insta360 camera footage (.insv/.mp4). The legacy `convert_insta360.sh` is still present but uses a different interface (no target presets, no de-fishing).

## External dependencies

`ffmpeg`, `ffprobe`, `gyroflow` must be on PATH. No package manager or install script — install them yourself. `python3` with `opencv-python` (cv2) and `numpy` are required for metadata detection and lens de-fishing.

## Scripts

### convert_one.sh — single video

```bash
convert_one.sh [OPTIONS] <input_file> <output_dir>
```

`input_file` is the full path to the video file with extension (e.g. `/path/to/PRO_VID_20260912_180521_00_012.mp4`).

### convert_all.sh — batch

```bash
convert_all.sh [OPTIONS] <input_dir> <output_dir>
```

Calls `convert_one.sh` for each unique base name in `input_dir`. Skips LRV previews, `.arb` files, and errors on `_10_` (second-lens) files.

### Shared options (forwarded by convert_all.sh to convert_one.sh)

`--target`, `--quality`, `--fov`, `--stabilization` — forwarded by convert_all.sh to convert_one.sh. Run with `-h` for details. Note: `--no-stabilize`, `--test`, and `--debug` are NOT forwarded by convert_all.sh.

**`convert_one.sh` specific options:**

| Option | Description |
|--------|-------------|
| `--test` | Limit output to first 5 seconds |
| `--debug DIR` | Extract first frame at each pipeline step into DIR |
| `--stabilization none` | Skip Gyroflow and de-fishing (equivalent to legacy `--no-stabilize`) |
| `--fov ultra\|mega\|dewarp\|linear` | Override default FOV for the target |

## Target presets

| Target | Resolution | Aspect | Default FOV | Description |
|--------|------------|--------|-------------|-------------|
| tv-4k | 3840x2160 | 16:9 | mega | 4K TV |
| tv-2k | 2560x1440 | 16:9 | mega | 2K TV |
| galaxy-s11 | 2560x1600 | 16:10 | mega | Samsung Tab S11 |
| ipad | 2732x2048 | 4:3 | dewarp | iPad |
| phone | 1080x1920 | 9:16 | ultra | Phone vertical |
| instagram | 1080x1080 | 1:1 | ultra | Instagram square |
| reel | 1080x1350 | 4:5 | ultra | TikTok/Reels portrait |
| raw | original | - | - | Raw H.265 passthrough (no crop/scale/de-fish/stabilization) |

Each target bundles resolution + aspect + default FOV. Use `--fov` to override the default FOV for any target. Valid FOV modes: `ultra`, `mega`, `dewarp`, `linear`.

## Quality levels

| Level | CRF | Preset | Gyro Bitrate | Description |
|-------|-----|--------|--------------|-------------|
| max | 18 | slow | 20M | Visually lossless |
| excellent | 20 | slow | 16M | Indistinguishable |
| good | 24 | fast | 8M | Great, smaller files (default) |
| acceptable | 28 | fast | 5M | Noticeable on close look |

## Stabilization levels

| Level | Gyroflow smoothness | Crop | Description |
|-------|---------------------|------|-------------|
| none | (skipped) | None | No stabilization, Gyroflow not run |
| standard | 0.25 | ~5-10% | Light smoothing, minimal crop |
| high | 0.5 (default) | ~15-20% | Moderate smoothing, moderate crop (recommended) |
| max | 1.0 | ~25-40% | Maximum smoothing, heavy crop |

Default is `high`. Use `--stabilization` to override. `--stabilization none` is equivalent to the legacy `--no-stabilize` flag.

## How it works

Single-lens: De-fished via `defish_insta360.py` (MEI/Unified model from metadata + `cv2.omnidir`), optionally stabilized via `gyroflow`, then crop + scale to target. Falls back to plain ffmpeg encode if metadata is missing.

**Metadata detection**: Before processing, `detect_insta360.py` checks for required Insta360 trailer records:
- Record 0x01: Metadata (camera model, lens calibration offset_v3)
- Record 0x03: Gyro (raw IMU data)
- Record 0x04: Exposure (rolling shutter timestamps)

If any are missing, both stabilization and de-fishing are skipped. The output filename uses `original` for both FOV and stabilization fields. The file is still encoded to H.265 and scaled to the target resolution (crop + scale only).

**De-fishing**: `defish_insta360.py` reads the offset_v3 lens calibration from the Insta360 metadata (MEI/Unified model: xi, fx, fy, cx, cy, k1, k2, p1, p2) and uses `cv2.omnidir.initUndistortRectifyMap` to generate pixel-correct undistortion. It outputs raw BGR24 frames to a temp file for crop+scale+encode. The `--fov` flag controls the output rectilinear field of view via a slider multiplier (ultra=1.0, mega=0.92, dewarp=0.85, linear=0.75).

## Pipeline steps (in order)

`convert_one.sh` executes one of four paths depending on `--target`, metadata availability, and `--stabilization`. When `--debug DIR` is set, an x265 video of each intermediate step is saved to DIR (the input is copied as-is; the raw de-fished BGR24 data is encoded to x265 at CRF 18).

### Path A: `raw` target (passthrough, no processing)

| Step | Operation | Debug output |
|------|-----------|-------------|
| 0 | Copy input | `01_input.mp4` |
| 1 | ffmpeg re-encode to H.265 (no crop/scale/de-fish/stabilize) | — |
| 2 | Copy output | `02_output.mp4` |

### Path B: No stabilization + no metadata (EFFECTIVE_FOV="original") — crop then scale

| Step | Operation | Debug output |
|------|-----------|-------------|
| 0 | Copy input | `01_input.mp4` |
| 1 | ffmpeg crop filter → temp file | `02_crop.mp4` |
| 2 | ffmpeg scale+pad filter from temp → final output | `03_output.mp4` |

### Path C: No stabilization + metadata present — de-fish then crop then scale

| Step | Operation | Debug output |
|------|-----------|-------------|
| 0 | Copy input | `01_input.mp4` |
| 1 | `defish_insta360.py` undistorts frames to raw BGR24 temp | — |
| 2 | Encode de-fished output to x265 (CRF 18) | `02_after_defish.mp4` |
| 3 | ffmpeg crop filter from defished temp → temp file | `03_crop.mp4` |
| 4 | ffmpeg scale+pad filter from temp → final output | `04_output.mp4` |

### Path D: Stabilization + metadata present — gyroflow, de-fish, crop, scale

| Step | Operation | Debug output |
|------|-----------|-------------|
| 0 | Copy input | `01_input.mp4` |
| 1 | `gyroflow` stabilization → `_stabilized.mp4` | — |
| 2 | Copy stabilized output | `02_after_stabilization.mp4` |
| 3 | `defish_insta360.py` undistorts stabilized frames to raw BGR24 temp | — |
| 4 | Encode de-fished output to x265 (CRF 18) | `03_after_defish.mp4` |
| 5 | ffmpeg crop filter from defished temp → temp file | `04_crop.mp4` |
| 6 | ffmpeg scale+pad filter from crop temp + audio → final output | `05_output.mp4` |

The `raw` target executes Path A (input → re-encode → output: `01_input.mp4`, `02_output.mp4`). The FOV is **not** a separate pipeline step — it is the `--fov` argument passed to `defish_insta360.py` during the de-fishing step (step 1 in Path C, step 3 in Path D).

## Conventions that differ from defaults

- Filenames with `LRV_` are low-res previews — always skipped. `.arb` and `_10_` files are also skipped by `convert_all.sh`.
- Output naming: `{yyyymmdd}-{hhmmss}-{NNNNNs}.{target}.{quality}.{fov}.{stabilization}.mp4` where fov and stabilization are `original` when metadata is missing, otherwise `none`, `standard`, `high`, `max`.
- Existing output files are skipped (idempotent reruns).
- Default target is `tv-2k` (ultra FOV).
- Default quality is `good` (CRF 24, fast preset, 8M gyro bitrate).
- Duration is formatted with leading zeros: `00017s` for 17 seconds, `03600s` for 1 hour.

## Test data

- `test/test.mp4` — single-lens Insta360 GO3S video (3072x2304, 50fps, H.264). Used by `test/test_one_target.sh` and `test/test_all_targets.sh`.
- `test/lrv_test.mp4` — LRV (low-res preview) test file.
- `test/test_all_targets.sh` — converts `test/test.mp4` to tv-2k, galaxy-s11, ipad, phone, instagram, reel (not all targets except tv-4k; excludes tv-4k and raw).
- `test/test_one_target.sh` — converts `test/test.mp4` to the instagram target (default argument).

## Testing notes

- H.265 encoding is CPU-intensive. When running tests, set timeout to **10 minutes** (600000ms) to avoid false failures. The full stabilize + de-fish + encode pipeline can take up to 15 minutes on a Raspberry Pi.
