#!/usr/bin/env python3
"""De-fish Insta360 GO3S single-lens video using MEI/Unified camera model.

Reads the Insta360 metadata (offset_v3) to get the exact lens parameters
(xi, fx, fy, cx, cy, k1, k2) and uses cv2.omnidir to generate a
pixel-correct undistortion remap. Outputs raw BGR24 frames to stdout
for piping to ffmpeg, or writes directly to a file.

The FOV modes are multipliers of the auto-detected maximum usable FOV
(the widest FOV with no black pixels from the fisheye circle boundary).
This matches Gyroflow's approach: fully undistort first, then zoom to frame.

Note: cv2.omnidir only accepts 4 distortion coefficients (k1, k2, p1, p2).
The k3 value from Insta360 metadata is read but cannot be used with this API.

Usage:
  python3 defish_insta360.py --fov ultra -i input.mp4 -o output.mp4
  python3 defish_insta360.py --fov ultra -i input.mp4 -o - | ffmpeg ...
"""

import argparse
import math
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from insta360_meta import (
    read_trailer, read_all_records, get_offset_v3_scaled,
)


# FOV modes as multipliers of the auto-detected maximum usable FOV.
# UltraWide: FOV slider ~1.0 (max without black edges)
# Dewarp: FOV slider 0.85-0.90 (Gyroflow recommends this range)
# Linear: FOV slider 0.70-0.80 (Gyroflow recommends this range)
# MegaView: between ultra and dewarp
FOV_SLIDERS = {
    "ultra":  1.00,
    "mega":   0.92,
    "dewarp": 0.85,
    "linear": 0.75,
}


def get_video_info(filepath):
    cap = cv2.VideoCapture(filepath)
    if not cap.isOpened():
        print(f"Error: cannot open {filepath}", file=sys.stderr)
        sys.exit(1)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return w, h, n


def load_lens_params(filepath, video_width, video_height):
    with open(filepath, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()
        trailer = read_trailer(f, file_size)
        if trailer is None:
            print(f"Error: {filepath} is not an Insta360 file", file=sys.stderr)
            sys.exit(1)
        records = read_all_records(f, file_size, trailer["extra_size"])
    params = get_offset_v3_scaled(records, video_width, video_height)
    if params is None:
        print(f"Error: no offset_v3 found in {filepath}", file=sys.stderr)
        sys.exit(1)
    return params


def build_remap(params, fov_h_deg, out_w, out_h):
    xi = np.array([params["xi"]], dtype=np.float64)
    K = np.array([
        [params["fx"], 0, params["cx"]],
        [0, params["fy"], params["cy"]],
        [0, 0, 1],
    ], dtype=np.float64)

    # cv2.omnidir accepts exactly 4 coefficients: (k1, k2, p1, p2)
    # k3 from Insta360 metadata cannot be used with this API.
    D = np.array([params["k1"], params["k2"], params["p1"], params["p2"]],
                 dtype=np.float64)

    fov_h_rad = math.radians(fov_h_deg)
    aspect = out_w / out_h
    fov_v_rad = 2 * math.atan(math.tan(fov_h_rad / 2) / aspect)

    fx_out = (out_w / 2.0) / math.tan(fov_h_rad / 2.0)
    fy_out = (out_h / 2.0) / math.tan(fov_v_rad / 2.0)

    P = np.array([
        [fx_out, 0, out_w / 2.0, 0],
        [0, fy_out, out_h / 2.0, 0],
        [0, 0, 1, 0],
    ], dtype=np.float64)

    R = np.eye(3, dtype=np.float64)

    map1, map2 = cv2.omnidir.initUndistortRectifyMap(
        K, D, xi, R, P, (out_w, out_h), cv2.CV_32FC1,
        cv2.omnidir.RECTIFY_PERSPECTIVE,
    )
    return map1, map2


def find_max_fov(params, src_w, src_h):
    """Binary search for the maximum H-FOV where no output pixels are black.

    Black pixels occur when the remap tries to read from outside the fisheye
    image circle. This function finds the widest FOV that still covers the
    entire output frame.
    """
    lo, hi = 60.0, 150.0

    for _ in range(20):
        mid = (lo + hi) / 2.0
        map1, map2 = build_remap(params, mid, src_w, src_h)

        # Check if any output pixel maps outside the source bounds
        invalid = (map1 < 0) | (map1 >= src_w) | (map2 < 0) | (map2 >= src_h)
        if np.any(invalid):
            hi = mid
        else:
            lo = mid

    return lo


def process_frames(input_path, output_path, fov_slider, source_path=None, debug_frame=None):
    src_w, src_h, total = get_video_info(input_path)
    print(f"Input: {src_w}x{src_h}, {total} frames", file=sys.stderr)

    metadata_path = source_path if source_path else input_path
    params = load_lens_params(metadata_path, src_w, src_h)
    print(f"MEI: xi={params['xi']:.3f} fx={params['fx']:.1f} fy={params['fy']:.1f}",
          file=sys.stderr)

    # Auto-detect maximum usable FOV (no black pixels)
    max_fov = find_max_fov(params, src_w, src_h)
    fov_h_deg = max_fov * fov_slider
    print(f"Max FOV: {max_fov:.1f} deg, slider: {fov_slider:.2f} -> effective: {fov_h_deg:.1f} deg",
          file=sys.stderr)

    map1, map2 = build_remap(params, fov_h_deg, src_w, src_h)

    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        print(f"Error: cannot open {input_path}", file=sys.stderr)
        sys.exit(1)

    out_raw = None
    if output_path != "-":
        out_raw = open(output_path, "wb")

    frame_num = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        defished = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_CONSTANT)

        if out_raw is not None:
            out_raw.write(defished.tobytes())
        else:
            sys.stdout.buffer.write(defished.tobytes())

        if debug_frame and frame_num == 0:
            cv2.imwrite(debug_frame, defished)

        frame_num += 1
        if frame_num % 100 == 0:
            print(f"  {frame_num}/{total}", file=sys.stderr)

    cap.release()
    if out_raw is not None:
        out_raw.close()

    print(f"Done: {frame_num} frames", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="De-fish Insta360 video using MEI/Unified lens model")
    parser.add_argument("--fov", required=True,
                        choices=list(FOV_SLIDERS.keys()),
                        help="FOV mode (multiplier of auto-detected max FOV)")
    parser.add_argument("-i", required=True, help="Input video file")
    parser.add_argument("--source", default=None,
                        help="File to read Insta360 metadata from (default: same as -i)")
    parser.add_argument("-o", required=True,
                        help="Output file path, or - for stdout (raw BGR24)")
    parser.add_argument("--debug-frame", default=None,
                        help="Save first defished frame as PNG to this path")
    args = parser.parse_args()

    fov_slider = FOV_SLIDERS[args.fov]
    process_frames(args.i, args.o, fov_slider, source_path=args.source,
                   debug_frame=args.debug_frame)


if __name__ == "__main__":
    main()
