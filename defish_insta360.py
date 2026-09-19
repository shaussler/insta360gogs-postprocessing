#!/usr/bin/env python3
"""De-fish Insta360 GO3S single-lens video using MEI/Unified camera model.

Reads the Insta360 metadata (offset_v3) to get the exact lens parameters
(xi, fx, fy, cx, cy, k1, k2) and uses cv2.omnidir to generate a
pixel-correct undistortion remap. Outputs raw BGR24 frames to stdout
for piping to ffmpeg, or writes directly to a file.

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


# Output rectilinear H-FOV per mode (degrees).
FOV_MODES = {
    "ultra":  120,
    "mega":   100,
    "dewarp":  90,
    "linear":  75,
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


def process_frames(input_path, output_path, fov_h_deg, source_path=None):
    src_w, src_h, total = get_video_info(input_path)
    print(f"Input: {src_w}x{src_h}, {total} frames", file=sys.stderr)

    metadata_path = source_path if source_path else input_path
    params = load_lens_params(metadata_path, src_w, src_h)
    print(f"MEI: xi={params['xi']:.3f} fx={params['fx']:.1f} fy={params['fy']:.1f} ", file=sys.stderr)
    print(f"De-fish FOV: {fov_h_deg} deg", file=sys.stderr)

    map1, map2 = build_remap(params, fov_h_deg, src_w, src_h)

    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        print(f"Error: cannot open {input_path}", file=sys.stderr)
        sys.exit(1)

    out_file = None
    if output_path != "-":
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        out_file = cv2.VideoWriter(output_path, fourcc, 30, (src_w, src_h))
        if not out_file.isOpened():
            print(f"Error: cannot create {output_path}", file=sys.stderr)
            sys.exit(1)

    frame_num = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        defished = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_CONSTANT)

        if out_file is not None:
            out_file.write(defished)
        else:
            sys.stdout.buffer.write(defished.tobytes())

        frame_num += 1
        if frame_num % 100 == 0:
            print(f"  {frame_num}/{total}", file=sys.stderr)

    cap.release()
    if out_file is not None:
        out_file.release()

    print(f"Done: {frame_num} frames", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="De-fish Insta360 video using MEI/Unified lens model")
    parser.add_argument("--fov", required=True,
                        choices=list(FOV_MODES.keys()),
                        help="Output rectilinear FOV mode")
    parser.add_argument("-i", required=True, help="Input video file")
    parser.add_argument("--source", default=None,
                        help="File to read Insta360 metadata from (default: same as -i)")
    parser.add_argument("-o", required=True,
                        help="Output file path, or - for stdout (raw BGR24)")
    args = parser.parse_args()

    fov_h_deg = FOV_MODES[args.fov]
    process_frames(args.i, args.o, fov_h_deg, source_path=args.source)


if __name__ == "__main__":
    main()
