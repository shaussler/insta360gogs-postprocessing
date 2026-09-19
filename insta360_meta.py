#!/usr/bin/env python3
"""Shared Insta360 metadata parsing library.

Provides functions to read the Insta360 trailer, parse protobuf records,
and extract lens calibration parameters (offset_v3 MEI model) from
MP4/INSV files.

Used by:
  - extract_insta360_metadata.py (full metadata display)
  - defish_insta360.py (lens de-fishing)

Field layouts follow gyroflow's telemetry-parser crate:
  github.com/AdrianEddy/telemetry-parser, src/insta360/
"""

import re
import struct

MAGIC = b"8db42d694ccc418790edff439fe026bf"
TRAILER_SIZE = 32 + 4 + 4 + 32  # padding(32) + extra_size(4) + version(4) + magic(32)

RECORD_NAMES = {
    0x00: "Offsets",
    0x01: "Metadata",
    0x02: "Thumbnail",
    0x03: "Gyro",
    0x04: "Exposure",
    0x05: "ThumbnailExt",
    0x06: "TimelapseTimestamp",
    0x07: "Gps",
    0x08: "StarNum",
    0x09: "AAAData",
    0x0A: "Anchors",
    0x0B: "AAASimulation",
    0x0C: "ExposureSecondary",
    0x0D: "Magnetic",
    0x0E: "Euler",
    0x0F: "SecGyro",
    0x10: "Speed",
    0x11: "TBox",
    0x12: "Quaternions",
    0x80: "TimeMap",
}

METADATA_FIELDS = {
    1:  "serial_number", 2: "camera_type", 3: "fw_version", 4: "file_type",
    5:  "offset", 6: "ip", 7: "creation_time", 8: "export_time",
    9:  "file_size", 10: "total_time", 11: "gps", 12: "orientation",
    13: "user_options", 14: "gyro", 15: "hdr_state", 16: "hdr_identifier",
    17: "original_offset", 18: "trigger_source", 19: "dimension",
    20: "frame_rate", 21: "image_translate", 22: "gamma_mode",
    23: "thumbnail_gyro_index", 24: "first_frame_timestamp",
    25: "rolling_shutter_time", 26: "file_group_info", 27: "window_crop_info",
    28: "gyro_timestamp", 29: "is_has_gyro_timestamp", 30: "timelapse_interval",
    31: "gyro_calib", 32: "evo_status_mode", 33: "evo_status_id",
    34: "original_offset_3d", 35: "gps_sources", 36: "first_gps_timestamp",
    37: "orientation_calib", 38: "is_collected", 39: "recycle_time",
    40: "total_frames", 41: "is_selfie", 42: "is_flowstate_online",
    43: "is_dewarp", 44: "resolution_size", 45: "battery_type",
    46: "cam_posture", 47: "fov_type", 48: "distance", 49: "fov",
    50: "gyro_filter_type", 51: "gyro_type", 52: "media_data_rotate_angel",
    53: "offset_v2", 54: "offset_v3", 55: "original_offset_v2",
    56: "original_offset_v3", 57: "focus_sensor", 58: "expect_output_type",
    59: "timelapse_interval_in_millisecond", 60: "photo_rot",
    61: "audio_mode", 62: "is_raw_gyro", 63: "raw_capture_type",
    64: "pts_type", 65: "gyro_cfg_info",
}

CAL_LAYOUTS = {
    10: ["lens_index", "focal_length_x", "focal_length_y", "principal_point_x",
         "distortion_k1", "distortion_k2", "fov_degrees", "sensor_width",
         "sensor_height", "id"],
    18: ["lens_index", "focal_length_x", "focal_length_y", "principal_point_x",
         "fov_degrees", "distortion_k1", "distortion_k2",
         "distortion_k3", "distortion_k4", "distortion_k5",
         "param_a", "param_b", "param_c", "param_d",
         "sensor_width", "sensor_height", "flag", "id"],
    21: ["num", "xi", "focal_length_x", "focal_length_y",
         "principal_point_x", "principal_point_y",
         "yaw_deg", "pitch_deg", "roll_deg",
         "tx", "ty", "tz",
         "distortion_k1", "distortion_k2", "distortion_k3",
         "distortion_p1", "distortion_p2",
         "sensor_width", "sensor_height", "lens_type", "flag"],
}


# ─── Trailer / record reading ─────────────────────────────────────────

def read_trailer(f, file_size):
    """Read the Insta360 trailer header from end of file.

    Returns dict with 'extra_size' and 'version', or None if not an Insta360 file.
    """
    f.seek(file_size - 32)
    magic = f.read(32)
    if magic != MAGIC:
        return None

    f.seek(file_size - 40)
    extra_size = struct.unpack("<I", f.read(4))[0]

    f.seek(file_size - 44)
    version = struct.unpack("<I", f.read(4))[0]

    return {"magic": magic, "extra_size": extra_size, "version": version}


def read_all_records(f, file_size, extra_size):
    """Read all Insta360 records from the extra data area.

    Records are stored backwards from the trailer: the first record header
    is at file_size - 78 (TRAILER_SIZE + 6), and each record's DATA sits
    BEFORE its 6-byte header (format + id + size).
    """
    records = []
    header_pos = file_size - (TRAILER_SIZE + 6)
    extra_start = file_size - TRAILER_SIZE - extra_size

    while header_pos >= extra_start:
        f.seek(header_pos)
        raw = f.read(6)
        if len(raw) < 6:
            break

        rec_format = raw[0]
        rec_id = raw[1]
        rec_size = struct.unpack("<I", raw[2:6])[0]

        if rec_id == 0 and rec_size == 0:
            header_pos -= 6
            continue

        data_pos = header_pos - rec_size
        if data_pos < extra_start:
            break

        f.seek(data_pos)
        data = f.read(rec_size)

        records.append({
            "id": rec_id,
            "name": RECORD_NAMES.get(rec_id, f"Unknown(0x{rec_id:02X})"),
            "format": rec_format,
            "size": rec_size,
            "data": data,
        })

        header_pos = data_pos - 6

    records.reverse()
    return records


# ─── Protobuf helpers ─────────────────────────────────────────────────

def read_varint(data, pos):
    """Read a protobuf varint at pos; returns (value, new_pos)."""
    result = 0
    shift = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 70:
            return result, pos


def parse_protobuf_fields(data):
    """Parse top-level protobuf fields from record 0x01 (ExtraMetadata).

    tag = (field_num << 3) | wire_type, itself a varint. Handles wire types
    0 (varint), 1 (fixed64), 2 (length-delimited), 5 (fixed32).
    """
    fields = []
    pos = 0
    n = len(data)

    while pos < n:
        try:
            tag, pos = read_varint(data, pos)
            if tag == 0:
                break
            field_num = tag >> 3
            wire = tag & 0x07
        except IndexError:
            break

        if wire == 0:  # varint
            try:
                value, pos = read_varint(data, pos)
            except IndexError:
                break
            fields.append({"field": field_num, "wire": 0, "value": value})
        elif wire == 1:  # fixed64
            if pos + 8 > n:
                break
            fields.append({"field": field_num, "wire": 1, "bytes": data[pos:pos + 8]})
            pos += 8
        elif wire == 2:  # length-delimited
            try:
                length, pos = read_varint(data, pos)
            except IndexError:
                break
            end = pos + length
            if end > n:
                break
            fields.append({"field": field_num, "wire": 2, "length": length,
                           "bytes": data[pos:end]})
            pos = end
        elif wire == 5:  # fixed32
            if pos + 4 > n:
                break
            fields.append({"field": field_num, "wire": 5, "bytes": data[pos:pos + 4]})
            pos += 4
        else:
            break

    return fields


def try_decode_string(value_bytes):
    """Try to decode bytes as an ASCII string."""
    try:
        s = value_bytes.decode("ascii")
        if all(32 <= ord(c) < 127 or c in "\t\n\r" for c in s):
            return s
    except (UnicodeDecodeError, ValueError):
        pass
    return None


# ─── Lens parameter extraction ────────────────────────────────────────

def get_offset_v3(records):
    """Extract MEI/Unified lens model parameters from offset_v3 (field 54).

    Returns dict with keys:
      xi, fx, fy, cx, cy, k1, k2, k3, p1, p2, sensor_w, sensor_h
    All values are at the SENSOR resolution (4056x3040 for GO3S).
    Returns None if offset_v3 is not found or has < 20 fields.
    """
    for rec in records:
        if rec["id"] != 0x01:
            continue
        fields = parse_protobuf_fields(rec["data"])
        for f in fields:
            if f["field"] != 54 or f["wire"] != 2:
                continue
            s = try_decode_string(f.get("bytes", b""))
            if not s:
                continue
            parts = s.split("_")
            if len(parts) < 20:
                continue
            try:
                vals = [float(p) for p in parts]
            except ValueError:
                continue
            return {
                "xi":       vals[1],
                "fx":       vals[2],
                "fy":       vals[3],
                "cx":       vals[4],
                "cy":       vals[5],
                "yaw":      vals[6],
                "pitch":    vals[7],
                "roll":     vals[8],
                "tx":       vals[9],
                "ty":       vals[10],
                "tz":       vals[11],
                "k1":       vals[12],
                "k2":       vals[13],
                "k3":       vals[14],
                "p1":       vals[15],
                "p2":       vals[16],
                "sensor_w": int(vals[17]),
                "sensor_h": int(vals[18]) if len(parts) > 18 else 3040,
            }
    return None


def get_offset_v3_scaled(records, video_width, video_height):
    """Get MEI parameters scaled from sensor resolution to video resolution.

    Returns dict with same keys as get_offset_v3(), but fx/fy/cx/cy scaled
    to match the actual video dimensions. Returns None if unavailable.
    """
    params = get_offset_v3(records)
    if params is None:
        return None

    scale = video_width / params["sensor_w"]
    return {
        "xi":       params["xi"],
        "fx":       params["fx"] * scale,
        "fy":       params["fy"] * scale,
        "cx":       params["cx"] * scale,
        "cy":       params["cy"] * scale,
        "k1":       params["k1"],
        "k2":       params["k2"],
        "k3":       params["k3"],
        "p1":       params["p1"],
        "p2":       params["p2"],
        "sensor_w": params["sensor_w"],
        "sensor_h": params["sensor_h"],
        "video_w":  video_width,
        "video_h":  video_height,
        "scale":    scale,
    }
