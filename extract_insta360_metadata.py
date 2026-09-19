#!/usr/bin/env python3
"""Extract and display Insta360 metadata from MP4/INSV files.

Parses the Insta360 trailer at the end of the file to extract and display
all metadata records in cleartext, including:
  - Camera info (serial, model, firmware, gyro configuration)
  - Lens calibration (offset / offset_v2 / offset_v3 strings, per protobuf field)
  - Gyro (IMU) data, raw u16 or float format, with timestamps
  - Exposure (per-frame shutter speed)
  - AAAData (auto-exposure / white-balance stats), Anchors, and other records

Field layouts follow gyroflow's telemetry-parser crate
(github.com/AdrianEddy/telemetry-parser, src/insta360/):

  Gyro record (0x03):  item = u64 ts(us) + 6 values. Raw format (u16,
      zero-centered at 32768) holds accel xyz then gyro xyz; float format
      holds 6x f64.
  Exposure (0x04):     item = u64 ts(us) + f64 shutter speed (seconds).
  AAAData (0x09):      item = 48 bytes: u32 ts(ms), f32 EV, f32 exp_time(ms),
      u32 data_stat, u32 luma_struct, 7x u32 temp.
  Lens calibration:    offset_v3 layout =
      num_xi_fx_fy_cx_cy_yaw_pitch_roll_tx_ty_tz_k1_k2_k3_p1_p2_width_height_lensType_flag

For large time-series records, only the first entries are printed.

Usage:
  python3 extract_insta360_metadata.py <input_file>
"""

import re
import struct
import sys

MAGIC = b"8db42d694ccc418790edff439fe026bf"
TRAILER_SIZE = 32 + 4 + 4 + 32  # padding(32) + extra_size(4) + version(4) + magic(32) = 72

MAX_PREVIEW_ENTRIES = 20  # max entries to show for time-series records

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

# Protobuf field names for record 0x01 (ExtraMetadata) — from telemetry-parser
# src/insta360/extra_info.rs
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

# Lens calibration string layouts, per number of underscore-delimited fields.
# 21 fields = offset_v3, with the layout Gyroflow/telemetry-parser consumes:
#   num_xi_fx_fy_cx_cy_yaw_pitch_roll_tx_ty_tz_k1_k2_k3_p1_p2_width_height_lensType_flag
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

# Small enum translations for the most interesting metadata fields
ENUM_HDR = {0: "NotHdr", 1: "WaitingProcess", 2: "Processed"}
ENUM_TRIGGER = {0: "Unknown", 1: "CameraButton", 2: "RemoteControl", 3: "Usb", 4: "BtRemote"}
ENUM_FOV_TYPE = {
    0: "Unknown", 1: "Wide", 2: "Linear", 3: "Ultrawide", 4: "Narrow", 5: "POV",
    6: "LinearPlus", 7: "LinearHorizon", 8: "FPV", 9: "Super", 10: "TinyPlanet",
    11: "InfinityWide", 12: "360LinearHorizon", 13: "MaxView", 14: "Dewarp",
    15: "Mega", 16: "BulletTime",
}
ENUM_GYRO_FILTER = {0: "Unknown", 1: "Brute", 2: "Akf"}
ENUM_GYRO_TYPE = {0: "InsdevImuType20948", 1: "InsdevImuType40609"}
ENUM_BATTERY = {0: "Thick", 1: "Thin", 2: "Vertical"}
ENUM_AUDIO = {0: "Unknown", 1: "Focus", 2: "Stereo", 3: "360", 4: "RsStereo"}
ENUM_RAW_CAPTURE = {0: "Off", 1: "Dng", 2: "Raw", 3: "Pureshot"}


# ─── Trailer / record reading ─────────────────────────────────────────

def read_trailer(f, file_size):
    """Read the Insta360 trailer header from end of file."""
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
    0 (varint), 1 (fixed64), 2 (length-delimited), 5 (fixed32). Returns a list
    of dicts with keys: field, wire, and value(bytes) as appropriate.
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
        else:  # groups (3/4) or reserved — this format doesn't use them
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


def find_strings_in_data(data):
    """Find all underscore-delimited numeric strings in binary data."""
    text = data.decode("latin-1")
    return re.findall(r'[\d]+_[\d\.eE\-_]{15,}', text)


def decode_field_value(field):
    """Decode a protobuf field value into a human-readable representation."""
    wire = field.get("wire", 0)

    if wire == 0:
        return str(field["value"])

    data = field.get("bytes", b"")
    if not data:
        return None

    if wire == 1 and len(data) >= 8:
        try:
            return struct.unpack("<d", data[:8])[0]
        except struct.error:
            pass

    if wire == 5 and len(data) >= 4:
        try:
            return struct.unpack("<f", data[:4])[0]
        except struct.error:
            pass

    s = try_decode_string(data)
    if s:
        return f'"{s}"'

    if len(data) == 8:
        try:
            return struct.unpack("<d", data)[0]
        except struct.error:
            pass

    if len(data) == 4:
        try:
            return struct.unpack("<f", data)[0]
        except struct.error:
            pass

    if len(data) >= 4 and len(data) % 4 == 0:
        try:
            vals = struct.unpack(f"<{len(data) // 4}f", data)
            return list(vals)
        except struct.error:
            pass

    return None


def format_bytes_field(data):
    """Best-effort human display for a raw bytes field."""
    s = try_decode_string(data)
    if s:
        return f'"{s}"'
    embedded = re.search(rb'(/[\x20-\x7e]+\.mp4)', data)
    if embedded:
        return f'path: {embedded.group(1).decode("ascii")}'
    if len(data) >= 4 and len(data) % 4 == 0:
        try:
            vals = struct.unpack(f"<{len(data) // 4}f", data)
            if all(abs(v) < 1e30 for v in vals):
                if len(vals) <= 8:
                    return "floats: " + ", ".join(f"{v:.6g}" for v in vals)
                return f"floats x{len(vals)}"
        except struct.error:
            pass
    return f"<{len(data)} bytes: {data[:32].hex()}{'...' if len(data) > 32 else ''}>"


# ─── Misc display helpers ─────────────────────────────────────────────

def hex_dump(data, offset=0, max_bytes=128):
    """Return a formatted hex dump string."""
    lines = []
    for i in range(0, min(len(data), max_bytes), 16):
        chunk = data[i:i + 16]
        hex_str = ' '.join(f'{b:02x}' for b in chunk)
        ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f"  {offset + i:04d}: {hex_str:<48} {ascii_str}")
    if len(data) > max_bytes:
        lines.append(f"  ... ({len(data) - max_bytes} more bytes)")
    return '\n'.join(lines)


def describe_calibration(s, label):
    """Print a lens calibration string with field labels."""
    parts = s.split("_")
    labels = CAL_LAYOUTS.get(len(parts), [f"field_{j}" for j in range(len(parts))])

    print(f"  {label}: ({len(parts)} fields) \"{s}\"")
    for lab, val in zip(labels, parts):
        print(f"    {lab:20s} = {val}")
    return labels


# ─── Record display functions ─────────────────────────────────────────

def display_record_01(rec, ctx=None):
    """Display Record 0x01 (Metadata): camera info + lens calibration."""
    data = rec["data"]
    fields = parse_protobuf_fields(data)

    print(f"=== Record 0x01: Metadata ({rec['size']:,} bytes) ===")
    print(f"Parsed {len(fields)} protobuf fields\n")

    by_field = {}
    for f in fields:
        by_field.setdefault(f["field"], []).append(f)

    def first_varint(fn):
        for f in by_field.get(fn, []):
            if f["wire"] == 0:
                return f["value"]
        return None

    def first_string(fn):
        for f in by_field.get(fn, []):
            if f["wire"] == 2:
                s = try_decode_string(f.get("bytes", b""))
                if s:
                    return s
        return None

    def first_double(fn):
        for f in by_field.get(fn, []):
            if f["wire"] == 1 and len(f.get("bytes", b"")) >= 8:
                return struct.unpack("<d", f["bytes"][:8])[0]
        return None

    # ── Camera info ──
    serial = first_string(1)
    model = first_string(2)
    fw = first_string(3)
    creation = first_varint(7)
    total_time = first_varint(10)
    file_size = first_varint(9)
    frame_rate = first_varint(20)

    # Gyro config (fields 62 is_raw_gyro, 65 gyro_cfg_info{acc_range,gyro_range})
    is_raw = None
    for f in by_field.get(62, []):
        if f["wire"] == 0:
            is_raw = bool(f["value"])
    acc_range = None
    gyro_range = None
    for f in by_field.get(65, []):
        if f["wire"] == 2:
            cfg = parse_protobuf_fields(f["bytes"])
            for g in cfg:
                if g["wire"] == 0:
                    if g["field"] == 1:
                        acc_range = g["value"]
                    elif g["field"] == 2:
                        gyro_range = g["value"]

    def gyro_format_label():
        if is_raw is True:
            return "raw (u16, zero-centered @32768 — scale = range/32768)"
        if is_raw is False:
            return "float (6x f64)"
        return "unknown"

    print("--- Camera Info ---")
    if serial:
        print(f"  Serial number:    {serial}")
    if model:
        print(f"  Camera model:     {model}")
    if fw:
        print(f"  Firmware version: {fw.split('*')[0].split('?')[0]}")
    if creation:
        ts = str(creation)
        if len(ts) == 14:
            print(f"  Timestamp:        {ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:{ts[12:14]}")
        else:
            print(f"  Timestamp:        {ts}")
    if total_time:
        print(f"  Recording time:   {total_time}s ({total_time // 3600}h{(total_time % 3600) // 60}m{total_time % 60}s)")
    if file_size:
        print(f"  Recorded size:    {file_size:,} bytes ({file_size / 1024 / 1024:.1f} MiB)")
    if frame_rate:
        print(f"  Frame rate:       {frame_rate} fps")
    if gyro_range and acc_range:
        print(f"  Gyro config:      {gyro_format_label()} — acc_range={acc_range} g, gyro_range={gyro_range} dps")
    elif is_raw is not None:
        print(f"  Gyro config:      {gyro_format_label()}")
    rs = first_double(25)
    if rs:
        print(f"  Rolling shutter:  {rs:.4f} ms")
    gyro_ts = first_double(28)
    if gyro_ts:
        print(f"  Gyro timestamp:   {gyro_ts / 1000.0:.3f} s")
    print()

    # ── Lens calibration (offset strings in their dedicated protobuf fields) ──
    print("--- Lens Calibration (per protobuf field) ---")
    cal_fields = {}
    for f in fields:
        if f["wire"] != 2:
            continue
        s = try_decode_string(f["bytes"])
        if s and "_" in s:
            parts = s.split("_")
            if len(parts) >= 10 and all(re.fullmatch(r"-?\d+(\.\d+)?(e-?\d+)?", p) for p in parts):
                cal_fields[f["field"]] = s

    if cal_fields:
        for fn, s in sorted(cal_fields.items()):
            describe_calibration(s, f"field {fn:2d} ({METADATA_FIELDS.get(fn, f'field_{fn}')})")
        print()

        # Best one to use: Gyroflow only uses offset_v3 (field 54, >=20 values)
        def part_count(s):
            return len(s.split("_"))

        preferred = None
        for fn in (54, 56, 53, 55, 5, 17):
            if fn in cal_fields:
                preferred = fn
                break
        best = max(cal_fields, key=lambda k: part_count(cal_fields[k]))
        preferred = preferred if part_count(cal_fields[preferred]) >= 20 else best

        best_s = cal_fields[preferred]
        parts = best_s.split("_")
        labels = CAL_LAYOUTS.get(len(parts), [f"field_{j}" for j in range(len(parts))])
        print("--- Lens Summary (recommended: what Gyroflow consumes) ---")
        print(f"  Source field {preferred}: {METADATA_FIELDS.get(preferred, '')}  ({len(parts)} values)")
        if len(parts) >= 21:
            m = {l: v for l, v in zip(labels, parts)}
            print(f"  xi (fisheye):        {m.get('xi')}")
            print(f"  Focal length:        fx={m.get('focal_length_x')} px, fy={m.get('focal_length_y')} px")
            print(f"  Principal point:     cx={m.get('principal_point_x')} px, cy={m.get('principal_point_y')} px")
            print(f"  Distortion:          k1={m.get('distortion_k1')}, k2={m.get('distortion_k2')}, "
                  f"k3={m.get('distortion_k3')}, p1={m.get('distortion_p1')}, p2={m.get('distortion_p2')}")
            print(f"  IMU mount rotation:  yaw={m.get('yaw_deg')} deg, pitch={m.get('pitch_deg')} deg, "
                  f"roll={m.get('roll_deg')} deg (≈90 deg = 90° sensor mount)")
            print(f"  Sensor size:         {m.get('sensor_width')}x{m.get('sensor_height')} px")
            print(f"  lens_type:           {m.get('lens_type')}, flag={m.get('flag')}")
        else:
            m = {l: v for l, v in zip(labels, parts)}
            print(f"  Focal length:        fx={m.get('focal_length_x')} px, fy={m.get('focal_length_y')} px")
            print(f"  Principal point x:   {m.get('principal_point_x')} px")
            print(f"  Distortion:          k1={m.get('distortion_k1')}, k2={m.get('distortion_k2')}")
            print(f"  FOV:                 {m.get('fov_degrees')} deg")
            print(f"  Sensor size:         {m.get('sensor_width')}x{m.get('sensor_height')} px")
        print()
    else:
        # Fallback: regex the raw record for underscore-delimited numeric strings
        meta_text = data.decode("latin-1")
        cal_strings = re.findall(r'[\d]+_[\d\.eE\-_]{15,}', meta_text)
        for s in cal_strings[:10]:
            describe_calibration(s.rstrip("_").rstrip("."), "raw scan")
        print()

    # ── All protobuf fields ──
    print("--- All Protobuf Fields ---")
    bytes_only_fields = {11, 12, 14, 31, 37}
    enum_maps = {
        15: ENUM_HDR, 18: ENUM_TRIGGER, 47: ENUM_FOV_TYPE, 50: ENUM_GYRO_FILTER,
        51: ENUM_GYRO_TYPE, 45: ENUM_BATTERY, 61: ENUM_AUDIO, 63: ENUM_RAW_CAPTURE,
    }
    bool_fields = {29, 38, 41, 42, 43, 59, 62, 65}

    for f in fields:
        fn = f["field"]
        name = METADATA_FIELDS.get(fn, f"field_{fn}")
        wire = f["wire"]

        if wire == 0:
            val = f["value"]
            if fn in bool_fields:
                print(f"  Field {fn:3d} {name:34s} (varint)   = {bool(val)}")
            else:
                enum = enum_maps.get(fn)
                suffix = f" ({enum.get(val)})" if enum and val in enum else ""
                print(f"  Field {fn:3d} {name:34s} (varint)   = {val}{suffix}")
        elif wire == 1:  # fixed64
            val = struct.unpack("<d", f["bytes"])[0]
            print(f"  Field {fn:3d} {name:34s} (double)   = {val:.9g}")
        elif wire == 5:  # fixed32
            val = struct.unpack("<f", f["bytes"])[0]
            print(f"  Field {fn:3d} {name:34s} (float)    = {val:.9g}")
        else:  # length-delimited
            raw = f.get("bytes", b"")
            if fn in bytes_only_fields:
                print(f"  Field {fn:3d} {name:34s} (bytes)    = {format_bytes_field(raw)}")
            else:
                val = decode_field_value(f)
                if val is None:
                    print(f"  Field {fn:3d} {name:34s} (bytes)    = {format_bytes_field(raw)}")
                elif isinstance(val, str):
                    print(f"  Field {fn:3d} {name:34s} (str)      = {val}")
                elif isinstance(val, float):
                    print(f"  Field {fn:3d} {name:34s} (double)   = {val:.9g}")
                else:
                    print(f"  Field {fn:3d} {name:34s} (floats)   = {val}")


def display_record_02(rec, ctx=None):
    """Display Record 0x02 (Thumbnail)."""
    data = rec["data"]
    print(f"=== Record 0x02: Thumbnail ({rec['size']:,} bytes) ===")
    if data[:2] == b'\xff\xd8':
        print("  Format: JPEG image")
        # Try to extract dimensions from JPEG SOF
        pos = 2
        while pos < len(data) - 1:
            if data[pos] != 0xFF:
                break
            marker = data[pos + 1]
            if marker == 0xD8 or marker == 0xD9:
                pos += 2
                continue
            if marker == 0x00:
                pos += 1
                continue
            # SOS or other markers without length
            if marker == 0xDA:
                break
            if pos + 3 < len(data):
                seg_len = struct.unpack(">H", data[pos + 2:pos + 4])[0]
                # SOF0-SOF3 markers contain image dimensions
                if 0xC0 <= marker <= 0xC3:
                    if pos + 9 < len(data):
                        height = struct.unpack(">H", data[pos + 5:pos + 7])[0]
                        width = struct.unpack(">H", data[pos + 7:pos + 9])[0]
                        print(f"  Dimensions: {width}x{height}")
                        break
                pos += 2 + seg_len
            else:
                break
    elif data[:4] == b'\x00\x00\x00\x01' or data[:3] == b'\x00\x00\x01':
        print(f"  Format: H.264 Annex B stream (first NAL type: 0x{data[4]:02x} "
              f"= {data[4] & 0x1f})")
    else:
        print(f"  Format: Unknown (first bytes: {data[:16].hex()})")
        print(hex_dump(data, max_bytes=64))


def display_record_03(rec, ctx):
    """Display Record 0x03 (Gyro): IMU data.

    Valid formats (from telemetry-parser RecordType::Gyro):
      raw   (20 bytes):  u64 ts(us) + 6x u16 zerocentered @32768
                         -> accel x,y,z then gyro x,y,z
      float (56 bytes):  u64 ts(us) + 6x f64 (accel then gyro)
    """
    data = rec["data"]
    is_raw = ctx.get("is_raw_gyro")
    if is_raw is None:
        item_size = 20 if len(data) % 20 == 0 and len(data) > 0 else 56
    else:
        item_size = 20 if is_raw else 56

    n = len(data) // item_size
    fmt_label = "raw u16@-32768" if item_size == 20 else "float f64"
    print(f"=== Record 0x03: Gyro ({rec['size']:,} bytes, {n:,} samples) ===")
    print(f"  Item size: {item_size} bytes ({fmt_label})")
    print("  Layout: u64 timestamp_us + accel(3) + gyro(3)")
    print()

    acc_range = ctx.get("acc_range")
    gyro_range = ctx.get("gyro_range")
    if item_size == 20:
        acc_scale = (acc_range / 32768.0) if acc_range else None        # g / LSB
        gyro_scale = (gyro_range / 32768.0) if gyro_range else None     # dps / LSB
        acc_unit = f"g (LSB * {acc_scale:.4g})" if acc_scale else "g (unknown scale)"
        gyro_unit = f"dps (LSB * {gyro_scale:.4g})" if gyro_scale else "dps (unknown scale)"
    else:
        acc_scale = gyro_scale = None
        acc_unit = "g"
        gyro_unit = "rad/s"

    print(f"  Accel unit: {acc_unit}")
    print(f"  Gyro  unit: {gyro_unit}")

    def parse_entry(i):
        e = data[i * item_size:(i + 1) * item_size]
        ts = struct.unpack("<Q", e[0:8])[0] / 1e6  # us -> s
        if item_size == 20:
            u = struct.unpack("<6H", e[8:20])
            acc = [x - 32768.0 for x in u[0:3]]
            gyro = [x - 32768.0 for x in u[3:6]]
        else:
            acc = list(struct.unpack("<3d", e[8:32]))
            gyro = list(struct.unpack("<3d", e[32:56]))
        return ts, acc, gyro

    if n == 0:
        print("  (no complete samples)")
        return

    count = min(n, MAX_PREVIEW_ENTRIES)
    header = (f"  {'Entry':>6s}  {'Time (s)':>11s}  "
              f"{'Accel X':>9s} {'Accel Y':>9s} {'Accel Z':>9s}  "
              f"{'Gyro X':>9s} {'Gyro Y':>9s} {'Gyro Z':>9s}")
    print(f"  First {count} entries:")
    print(header)
    for i in range(count):
        ts, acc, gyro = parse_entry(i)
        vals = []
        for v, sc in zip(acc + gyro, [acc_scale] * 3 + [gyro_scale] * 3):
            if sc:
                vals.append(f"{v:8.1f}({v * sc:5.1f})")
            else:
                vals.append(f"{v:9.1f}")
        print(f"  {i:6d}  {ts:11.4f}  {vals[0]} {vals[1]} {vals[2]}  "
              f"{vals[3]} {vals[4]} {vals[5]}")

    if n > MAX_PREVIEW_ENTRIES:
        print(f"  ... ({n - MAX_PREVIEW_ENTRIES:,} more samples)")

    # Show last few
    last_start = max(count, n - 5)
    if last_start < n:
        print(f"  Last {n - last_start} samples:")
        print(header)
        for i in range(last_start, n):
            ts, acc, gyro = parse_entry(i)
            vals = []
            for v, sc in zip(acc + gyro, [acc_scale] * 3 + [gyro_scale] * 3):
                if sc:
                    vals.append(f"{v:8.1f}({v * sc:5.1f})")
                else:
                    vals.append(f"{v:9.1f}")
            print(f"  {i:6d}  {ts:11.4f}  {vals[0]} {vals[1]} {vals[2]}  "
                  f"{vals[3]} {vals[4]} {vals[5]}")

    if n > 1:
        ts0, _, _ = parse_entry(0)
        tsN, _, _ = parse_entry(n - 1)
        span_us = (tsN - ts0) * 1e6
        interval_us = span_us / (n - 1)
        print(f"\n  Total: {n:,} samples over ~{tsN - ts0:.3f} s "
              f"(avg interval {interval_us:.2f} us = {1e6 / interval_us:.1f} Hz)")


def display_record_04(rec, ctx):
    """Display Record 0x04 (Exposure): u64 ts(us) + f64 shutter time (s)."""
    data = rec["data"]
    entry_size = 16
    n = len(data) // entry_size
    print(f"=== Record 0x04: Exposure ({rec['size']:,} bytes, {n} entries) ===")
    print("  Item: u64 timestamp_us + f64 shutter_time (seconds)")
    print()

    def parse_entry(i):
        e = data[i * entry_size:(i + 1) * entry_size]
        ts_us = struct.unpack("<Q", e[0:8])[0]
        shutter = struct.unpack("<d", e[8:16])[0]
        return ts_us, shutter

    if n == 0:
        print("  (no complete entries)")
        return

    count = min(n, MAX_PREVIEW_ENTRIES)
    print(f"  First {count} entries:")
    print(f"  {'Entry':>6s}  {'Timestamp (us)':>14s}  {'Time (s)':>10s}  "
          f"{'Shutter (s)':>12s}  {'Shutter (ms)':>12s}")
    for i in range(count):
        ts_us, shutter = parse_entry(i)
        print(f"  {i:6d}  {ts_us:>14d}  {ts_us / 1e6:>10.6f}  "
              f"{shutter:>12.6f}  {shutter * 1000.0:>12.3f}")

    if n > MAX_PREVIEW_ENTRIES:
        print(f"  ... ({n - count} more entries)")
        last_start = max(count, n - 5)
        print(f"  Last {n - last_start} entries:")
        for i in range(last_start, n):
            ts_us, shutter = parse_entry(i)
            print(f"  {i:6d}  {ts_us:>14d}  {ts_us / 1e6:>10.6f}  "
                  f"{shutter:>12.6f}  {shutter * 1000.0:>12.3f}")

    if n > 1:
        ts0, _ = parse_entry(0)
        tsN, _ = parse_entry(n - 1)
        span_us = tsN - ts0
        interval_us = span_us / (n - 1)
        print(f"\n  Duration: ~{span_us / 1e6:.3f} s, avg interval "
              f"{interval_us:.1f} us ({1e6 / interval_us:.1f} Hz) = per-frame recording")
        shutters = [parse_entry(i)[1] for i in range(n)]
        print(f"  Shutter range: {min(shutters) * 1000:.3f} ms .. "
              f"{max(shutters) * 1000:.3f} ms")


def display_record_09(rec, ctx):
    """Display Record 0x09 (AAAData): fixed 48-byte binary items.

    Layout (telemetry-parser RecordType::AAAData):
      u32 ts(ms), f32 ev_target, f32 exp_time(ms), u32 data_stat,
      u32 luma_struct, 7x u32 temp_data.
    """
    data = rec["data"]
    entry_size = 48
    n = len(data) // entry_size
    print(f"=== Record 0x09: AAAData ({rec['size']:,} bytes, {n} samples) ===")
    print("  Auto Exposure / Auto White Balance data (fixed 48-byte items, not protobuf)")
    print()

    def parse_entry(i):
        e = data[i * entry_size:(i + 1) * entry_size]
        ts_ms = struct.unpack("<I", e[0:4])[0]
        ev = struct.unpack("<f", e[4:8])[0]
        exp_ms = struct.unpack("<f", e[8:12])[0]
        data_stat = struct.unpack("<I", e[12:16])[0]
        luma = struct.unpack("<I", e[16:20])[0]
        temp = struct.unpack("<7I", e[20:48])

        luma_wg_grid = luma & 0x7F
        luma_wg_y = (luma & 0x3F80) >> 7
        sum_wg_y = (0x7C000 & luma) >> 14
        iso_value = (100 * ((luma & 0xFFF80000) >> 19)) >> 6
        return ts_ms, ev, exp_ms, data_stat, luma, (luma_wg_grid, luma_wg_y, sum_wg_y, iso_value), temp

    if n == 0:
        print("  (no complete samples)")
        return

    count = min(n, MAX_PREVIEW_ENTRIES)
    print(f"  First {count} samples:")
    print(f"  {'Entry':>6s}  {'Time (s)':>10s}  {'EV':>7s}  {'Exp (ms)':>8s}  "
          f"{'ISO':>5s}  {'data_stat':>10s}  {'wgrid':>5s} {'wgy':>4s} {'sum_wg_y':>7s}")
    for i in range(count):
        ts_ms, ev, exp_ms, dstat, luma, (wg, wy, swy, iso), temp = parse_entry(i)
        print(f"  {i:6d}  {ts_ms / 1000.0:>10.3f}  {ev:>7.3f}  {exp_ms:>8.3f}  "
              f"{iso:>5d}  0x{dstat:08x}  {wg:>5d} {wy:>4d} {swy:>7d}")

    if n > MAX_PREVIEW_ENTRIES:
        print(f"  ... ({n - MAX_PREVIEW_ENTRIES} more samples)")

    if n > 1:
        ts0 = parse_entry(0)[0]
        tsN = parse_entry(n - 1)[0]
        print(f"\n  Span: {ts0 / 1000.0:.3f} s .. {tsN / 1000.0:.3f} s "
              f"= {tsN - ts0} ms over {n} samples")


def display_record_0a(rec, ctx=None):
    """Display Record 0x0A (Anchors).

    Layout (telemetry-parser): u8 type + u32 count, then count timestamps;
    types 2/18 carry two u64 timestamps each, others one.
    """
    data = rec["data"]
    print(f"=== Record 0x0A: Anchors ({rec['size']:,} bytes) ===")
    pos = 0
    anchors = []
    while pos < len(data):
        if pos + 5 > len(data):
            break
        type_ = data[pos]
        count = struct.unpack("<I", data[pos + 1:pos + 5])[0]
        pos += 5
        items = []
        ok = True
        for _ in range(count):
            fields_per = 2 if type_ in (2, 18) else 1
            if pos + 8 * fields_per > len(data):
                ok = False
                break
            items.append([struct.unpack("<Q", data[pos + 8 * j:pos + 8 * j + 8])[0]
                          for j in range(fields_per)])
            pos += 8 * fields_per
        if not ok or items:
            anchors.append({"type": type_, "count": count, "items": items})
    if anchors:
        print(f"  Anchors parsed: {len(anchors)}")
        for a in anchors:
            print(f"    type={a['type']} count={a['count']}: "
                  f"{[it for it in a['items'][:10]]}"
                  f"{'...' if len(a['items']) > 10 else ''}")
        if pos < len(data):
            print(f"  Trailing {len(data) - pos} bytes: {data[pos:].hex()}")
    else:
        print(hex_dump(data, max_bytes=64))


def display_record_00(rec):
    """Display Record 0x00 (Offsets).

    Layout (telemetry-parser): repeated entries of u8 id + u8 format +
    u32 size + u32 offset (10 bytes each).
    """
    data = rec["data"]
    print(f"=== Record 0x00: Offsets ({rec['size']:,} bytes) ===")
    if len(data) % 10 != 0:
        print("  (data length not a multiple of 10; showing hex)")
        print(hex_dump(data, max_bytes=64))
        return
    n = len(data) // 10
    print(f"  {'ID':>3s} {'Fmt':>3s} {'Size':>8s} {'Offset':>10s}")
    for i in range(n):
        e = data[i * 10:(i + 1) * 10]
        rid = e[0]
        fmt = e[1]
        size = struct.unpack("<I", e[2:6])[0]
        offs = struct.unpack("<I", e[6:10])[0]
        print(f"  {rid:#04x} {fmt:#04x} {size:>8d} {offs:>10d}")


def display_generic_record(rec, ctx=None):
    """Display any other record type with hex dump."""
    data = rec["data"]
    print(f"=== Record 0x{rec['id']:02X}: {rec['name']} ({rec['size']:,} bytes, format={rec['format']}) ===")

    strings = []
    for m in re.finditer(rb'[\x20-\x7e]{8,}', data):
        strings.append((m.start(), m.group().decode('ascii')))
    if strings:
        print(f"  Embedded strings ({len(strings)}):")
        for offset, s in strings[:20]:
            print(f"    offset {offset:5d}: \"{s}\"")

    cal = find_strings_in_data(data)
    if cal:
        print(f"  Lens calibration strings ({len(cal)}):")
        for s in cal[:10]:
            print(f"    \"{s}\"")

    print(f"\n  First 128 bytes:")
    print(hex_dump(data, max_bytes=128))


# ─── Main ──────────────────────────────────────────────────────────────

RECORD_DISPLAYERS = {
    0x00: display_record_00,
    0x01: display_record_01,
    0x02: display_record_02,
    0x03: display_record_03,
    0x04: display_record_04,
    0x09: display_record_09,
    0x0A: display_record_0a,
}


def build_gyro_context(records):
    """Extract gyro config (is_raw_gyro, ranges) from record 0x01 if present."""
    ctx = {"is_raw_gyro": None, "acc_range": None, "gyro_range": None}
    for rec in records:
        if rec["id"] != 0x01:
            continue
        for f in parse_protobuf_fields(rec["data"]):
            if f["wire"] == 0 and f["field"] == 62:
                ctx["is_raw_gyro"] = bool(f["value"])
            elif f["wire"] == 2 and f["field"] == 65:
                for g in parse_protobuf_fields(f["bytes"]):
                    if g["wire"] == 0:
                        if g["field"] == 1:
                            ctx["acc_range"] = g["value"]
                        elif g["field"] == 2:
                            ctx["gyro_range"] = g["value"]
    return ctx


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <input_file>", file=sys.stderr)
        sys.exit(2)

    filepath = sys.argv[1]

    try:
        with open(filepath, "rb") as f:
            f.seek(0, 2)
            file_size = f.tell()

            trailer = read_trailer(f, file_size)
            if trailer is None:
                print(f"Error: {filepath} is not an Insta360 file (magic not found)", file=sys.stderr)
                sys.exit(1)

            print(f"=== Insta360 Metadata ===")
            print(f"File: {filepath}")
            print(f"File size: {file_size:,} bytes ({file_size / 1024 / 1024:.1f} MB)")
            print(f"Extra data size: {trailer['extra_size']:,} bytes")
            print(f"Trailer version: {trailer['version']}")
            print()

            records = read_all_records(f, file_size, trailer["extra_size"])
            ctx = build_gyro_context(records)

            print(f"Records found: {len(records)}")
            for rec in records:
                print(f"  0x{rec['id']:02X} ({rec['name']:20s}): format={rec['format']}, size={rec['size']:,} bytes")
            print()

            for rec in records:
                displayer = RECORD_DISPLAYERS.get(rec["id"], display_generic_record)
                displayer(rec, ctx)
                print()

    except FileNotFoundError:
        print(f"Error: file not found: {filepath}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()