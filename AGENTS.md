# AGENTS.md

Two scripts process Insta360 camera footage (.insv/.mp4). The legacy `convert_insta360.sh` is still present.

## External dependencies

`ffmpeg`, `ffprobe`, `gyroflow` must be on PATH. No package manager or install script — install them yourself. `python3` is also required for metadata detection.

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

Calls `convert_one.sh` for each unique base name in `input_dir`. Skips LRV previews and `_10_` second-lens files (handled by convert_one.sh with their `_00_` pair).

### Shared options

`--target`, `--quality`, `--fov`, `--stabilization`, `--no-stabilize` — forwarded by convert_all.sh to convert_one.sh. Run with `-h` for details.

## Target presets

| Target | Resolution | Aspect | Default FOV | Description |
|--------|------------|--------|-------------|-------------|
| tv-4k | 3840x2160 | 16:9 | ultra | 4K TV |
| tv-2k | 2560x1440 | 16:9 | ultra | 2K TV |
| galaxy-s11 | 2560x1600 | 16:10 | mega | Samsung Tab S11 |
| ipad | 2732x2048 | 4:3 | dewarp | iPad |
| phone | 1080x1920 | 9:16 | linear | Phone vertical |
| instagram | 1080x1080 | 1:1 | linear | Instagram square |
| reel | 1080x1350 | 4:5 | linear | TikTok/Reels portrait |

Each target bundles resolution + aspect + default FOV. Use `--fov` to override the default FOV for any target.

## Stabilization levels

| Level | Gyroflow smoothness | Crop | Description |
|-------|---------------------|------|-------------|
| none | (skipped) | None | No stabilization, Gyroflow not run |
| standard | 0.25 | ~5-10% | Light smoothing, minimal crop |
| high | 0.5 (default) | ~15-20% | Moderate smoothing, moderate crop (recommended) |
| max | 1.0 | ~25-40% | Maximum smoothing, heavy crop |

Default is `high`. Use `--stabilization` to override. `--no-stabilize` is a shortcut for `--stabilization none`.

## How it works

Two code paths based on file pairing:
- **Dual-lens 360**: `_00_` + `_10_` lens pairs are stitched via `ffmpeg hstack` + `v360` filter → single equirectangular H.265 file.
- **Single-lens**: Stabilized via `gyroflow`, then FOV conversion + crop + scale to target via `v360` filter. Falls back to plain ffmpeg encode if gyroflow fails.

**Metadata detection**: Before processing single-lens files, `detect_insta360.py` checks for required Insta360 trailer records:
- Record 0x01: Metadata (camera model, lens calibration offset_v3)
- Record 0x03: Gyro (raw IMU data)
- Record 0x04: Exposure (rolling shutter timestamps)

If any are missing, both stabilization and FOV conversion are skipped. The output filename uses `original` for both fields. The file is still encoded to H.265 and scaled to the target resolution (crop + scale only).

Filter chain order for single-lens: `format=yuv420p` → `v360=fisheye:flat` → `crop` → `scale`.

## Conventions that differ from defaults

- Filenames with `LRV_` are low-res previews — always skipped.
- Files matching `*_10_*` are second-lens pairs — never processed independently.
- Output naming: `{yyyymmdd}-{hhmmss}-{NNNNNs}.{target}.{quality}.{fov}.{stabilization}.mp4` where stabilization is `none`, `standard`, `high`, `max`, or `original` (when metadata missing).
- Existing output files are skipped (idempotent reruns).
- Default target is `tv-4k` (ultra FOV).
- Default quality is `good` (CRF 24, fast preset, 8M gyro bitrate).
- Duration is formatted with leading zeros: `00017s` for 17 seconds, `03600s` for 1 hour.

## Test data

`test/` contains one paired 360 set (`PRO_VID_*_00_012.mp4` + `PRO_LRV_*_01_012.lrv` — the LRV is ignored, second-lens `_10_` pair is missing so this processes as single-lens). `test/test_all_targets.sh` converts the test video to all targets (except tv-4k).
