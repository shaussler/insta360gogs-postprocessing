#!/usr/bin/env python3
"""Extract and display Insta360 metadata from MP4/INSV files.

Parses the Insta360 trailer at the end of the file to extract and display
all metadata records in cleartext, including:
  - Camera info (serial, model, firmware)
  - Lens calibration parameters (focal length, distortion, FOV, etc.)
  - Gyro (IMU) data
  - Exposure / rolling shutter data
  - AAAData, Anchors, and other record types

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


# ─── Record-specific parsers ──────────────────────────────────────────

def parse_protobuf_fields(data):
    """Parse protobuf-like TLV fields from binary data.

    Each field has: varint tag + varint length (for length-delimited) + value.
    tag = (field_num << 3) | wire_type.
    """
    fields = []
    pos = 0

    while pos < len(data):
        if pos >= len(data):
            break

        tag = data[pos]
        pos += 1
        field_num = tag >> 3
        wire_type = tag & 0x07

        # Decode varint for length/value
        length = 0
        shift = 0
        while pos < len(data):
            b = data[pos]
            pos += 1
            length |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break

        if wire_type == 2:  # length-delimited
            value_bytes = data[pos:pos + length]
            pos += length
            fields.append({
                "field": field_num,
                "wire": wire_type,
                "length": length,
                "bytes": value_bytes,
            })
        elif wire_type == 0:  # varint
            fields.append({
                "field": field_num,
                "wire": wire_type,
                "value": length,
            })
        else:
            value_bytes = data[pos:pos + length]
            pos += length
            fields.append({
                "field": field_num,
                "wire": wire_type,
                "length": length,
                "bytes": value_bytes,
            })

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


def decode_field_value(field):
    """Decode a protobuf field value into a human-readable representation."""
    wire = field.get("wire", 0)

    if wire == 0:
        return str(field["value"])

    data = field.get("bytes", b"")
    if not data:
        return None

    s = try_decode_string(data)
    if s:
        return f'"{s}"'

    if len(data) == 8:
        try:
            val = struct.unpack("<d", data)[0]
            return val
        except struct.error:
            pass

    if len(data) == 4:
        try:
            val = struct.unpack("<f", data)[0]
            return val
        except struct.error:
            pass

    if len(data) >= 4 and len(data) % 4 == 0:
        try:
            vals = struct.unpack(f"<{len(data) // 4}f", data)
            return list(vals)
        except struct.error:
            pass

    return None


# ─── Record display functions ─────────────────────────────────────────

def display_record_01(rec):
    """Display Record 0x01 (Metadata): camera info + lens calibration."""
    data = rec["data"]
    fields = parse_protobuf_fields(data)

    print(f"=== Record 0x01: Metadata ({rec['size']:,} bytes) ===")
    print(f"Parsed {len(fields)} protobuf fields\n")

    # Camera info
    print("--- Camera Info ---")
    for field in fields:
        val = decode_field_value(field)
        if val is None:
            continue
        fn = field["field"]
        if isinstance(val, str) and val.startswith('"'):
            s = val.strip('"')
            if fn == 1:
                print(f"  Serial number:    {s}")
            elif fn == 2:
                print(f"  Camera model:     {s}")
            elif fn == 3:
                fw = s.split("*")[0].split("?")[0]
                print(f"  Firmware version: {fw}")
            elif fn == 17:
                print(f"  Field 17:         {s}")
        elif isinstance(val, int):
            if fn == 7:
                ts = str(val)
                if len(ts) == 14:
                    print(f"  Timestamp:        {ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:{ts[12:14]}")
                else:
                    print(f"  Field {fn:2d} (varint): {val}")
            elif fn == 10:
                print(f"  Duration:         {val}s ({val // 3600}h{(val % 3600) // 60}m{val % 60}s)")
            else:
                print(f"  Field {fn:2d} (varint): {val}")
    print()

    # Lens calibration strings
    meta_text = data.decode("latin-1")
    all_cal = re.findall(r'[\d]+_[\d\.eE\-_]{15,}', meta_text)
    seen = set()
    cal_strings = []
    for s in all_cal:
        s_clean = s.rstrip("_").rstrip(".")
        if s_clean not in seen and len(s_clean) > 20:
            seen.add(s_clean)
            cal_strings.append(s_clean)

    if cal_strings:
        print("--- Lens Calibration Strings ---")
        for i, s in enumerate(cal_strings):
            parts = s.split("_")
            print(f"\n  Calibration {i + 1} ({len(parts)} fields): \"{s}\"")

            if len(parts) == 10:
                labels = ["lens_index", "focal_length_x", "focal_length_y",
                          "principal_point_x", "distortion_k1", "distortion_k2",
                          "fov_degrees", "sensor_width", "sensor_height", "id"]
            elif len(parts) == 18:
                labels = ["lens_index", "focal_length_x", "focal_length_y",
                          "principal_point_x", "distortion_k1", "distortion_k2",
                          "fov_degrees", "distortion_k3", "distortion_k4", "distortion_k5",
                          "param_a", "param_b", "param_c", "param_d",
                          "sensor_width", "sensor_height", "flag", "id"]
            elif len(parts) == 21:
                labels = ["lens_index", "pixel_aspect", "focal_length_x", "focal_length_y",
                          "principal_point_x", "principal_point_y", "distortion_k1",
                          "distortion_k2", "fov_degrees", "distortion_k3", "distortion_k4",
                          "distortion_k5", "param_a", "param_b", "param_c",
                          "param_d", "param_e", "sensor_width", "sensor_height",
                          "flag", "id"]
            else:
                labels = [f"field_{j}" for j in range(len(parts))]

            for label, val in zip(labels, parts):
                print(f"    {label:25s} = {val}")

        # Summary
        best = max(cal_strings, key=lambda s: len(s.split("_")))
        parts = best.split("_")
        print("\n--- Lens Summary (most complete calibration) ---")
        if len(parts) >= 21:
            print(f"  Lens index:          {parts[0]}")
            print(f"  Pixel aspect ratio:  {parts[1]}")
            print(f"  Focal length X:      {parts[2]} px")
            print(f"  Focal length Y:      {parts[3]} px")
            print(f"  Principal point X:   {parts[4]} px")
            print(f"  Principal point Y:   {parts[5]} px")
            print(f"  Distortion k1:       {parts[6]}")
            print(f"  Distortion k2:       {parts[7]}")
            print(f"  FOV:                 {parts[8]} deg")
            print(f"  Distortion k3:       {parts[9]}")
            print(f"  Distortion k4:       {parts[10]}")
            print(f"  Distortion k5:       {parts[11]}")
            print(f"  Sensor size:         {parts[17]}x{parts[18]}")
        elif len(parts) >= 10:
            print(f"  Lens index:          {parts[0]}")
            print(f"  Focal length X:      {parts[1]} px")
            print(f"  Focal length Y:      {parts[2]} px")
            print(f"  Principal point X:   {parts[3]} px")
            print(f"  Distortion k1:       {parts[4]}")
            print(f"  Distortion k2:       {parts[5]}")
            print(f"  FOV:                 {parts[6]} deg")
            print(f"  Sensor size:         {parts[7]}x{parts[8]}")

    # All protobuf fields
    print("\n--- All Protobuf Fields ---")
    for field in fields:
        val = decode_field_value(field)
        fn = field["field"]
        if val is None:
            raw = field.get("bytes", b"")
            # Try to decode as file path
            try:
                s = raw.decode("utf-8")
                if "/" in s:
                    path = s[:s.index(".mp4") + 4] if ".mp4" in s else s
                    print(f"  Field {fn:2d} (path):       {path}")
                    continue
            except (UnicodeDecodeError, ValueError):
                pass
            embedded = re.search(rb'(/[\x20-\x7e]+\.mp4)', raw)
            if embedded:
                print(f"  Field {fn:2d} (path):       {embedded.group(1).decode('ascii')}")
                continue
            nested = find_strings_in_data(raw)
            if not nested and len(raw) > 0:
                print(f"  Field {fn:2d} (bytes, {len(raw)}B): {raw[:40].hex()}...")
        elif isinstance(val, str) and val.startswith('"'):
            s = val.strip('"')
            if "/" in s and ".mp4" in s:
                print(f"  Field {fn:2d} (path):       {s}")
            elif "_" in s and any(c.isdigit() for c in s):
                pass
            else:
                print(f"  Field {fn:2d} (str):        {s}")
        elif isinstance(val, int):
            print(f"  Field {fn:2d} (varint):     {val}")
        elif isinstance(val, float):
            print(f"  Field {fn:2d} (float):      {val:.6g}")
        elif isinstance(val, list):
            if len(val) <= 8:
                print(f"  Field {fn:2d} (floats x{len(val)}): {[f'{v:.6g}' if v is not None else '?' for v in val]}")
            else:
                print(f"  Field {fn:2d} (floats x{len(val)}): [{val[0]:.6g}, {val[1]:.6g}, ... {val[-1]:.6g}]")


def display_record_02(rec):
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
    else:
        print(f"  Format: Unknown (first bytes: {data[:16].hex()})")
        print(hex_dump(data, max_bytes=64))


def display_record_03(rec):
    """Display Record 0x03 (Gyro): IMU data as 20-byte entries."""
    data = rec["data"]
    entry_size = 20
    n = len(data) // entry_size
    print(f"=== Record 0x03: Gyro ({rec['size']:,} bytes, ~{n} samples) ===")
    print(f"  Entry size: {entry_size} bytes")
    print(f"  Data format: 2-byte counter + 4-byte zeros + 12 bytes (3x int16?) + 2 bytes")
    print()

    count = min(n, MAX_PREVIEW_ENTRIES)
    print(f"  First {count} entries:")
    print(f"  {'Entry':>6s}  {'Counter':>8s}  {'Raw bytes (6-17)':40s}  {'As 3xint16':>24s}  {'Tail':>6s}")
    for i in range(count):
        entry = data[i * entry_size:(i + 1) * entry_size]
        counter = struct.unpack("<H", entry[0:2])[0]
        b2_7 = entry[2:6]
        raw_6_17 = entry[6:18]
        tail = struct.unpack("<H", entry[18:20])[0]

        # Try as 3 signed int16 (most likely gyro/accel data)
        i1 = struct.unpack("<h", raw_6_17[0:2])[0]
        i2 = struct.unpack("<h", raw_6_17[2:4])[0]
        i3 = struct.unpack("<h", raw_6_17[4:6])[0]
        i4 = struct.unpack("<h", raw_6_17[6:8])[0]
        i5 = struct.unpack("<h", raw_6_17[8:10])[0]
        i6 = struct.unpack("<h", raw_6_17[10:12])[0]

        raw_hex = ' '.join(f'{b:02x}' for b in raw_6_17)
        print(f"  {i:6d}  {counter:04x}({counter:5d})  {raw_hex}  "
              f"{i1:6d},{i2:6d},{i3:6d},{i4:6d},{i5:6d},{i6:6d}  {tail:04x}")

    if n > MAX_PREVIEW_ENTRIES:
        print(f"  ... ({n - MAX_PREVIEW_ENTRIES} more entries)")
    print(f"\n  Total: {n} samples")


def display_record_04(rec):
    """Display Record 0x04 (Exposure): rolling shutter timestamps as 16-byte entries."""
    data = rec["data"]
    entry_size = 16
    n = len(data) // entry_size
    print(f"=== Record 0x04: Exposure ({rec['size']:,} bytes, {n} entries) ===")
    print(f"  Entry size: {entry_size} bytes")
    print(f"  Layout: [4B timestamp_us] [4B zero] [4B shutter_state] [4B exposure_time]")
    print()

    count = min(n, MAX_PREVIEW_ENTRIES)
    print(f"  First {count} entries:")
    print(f"  {'Entry':>6s}  {'Timestamp (us)':>14s}  {'Time (s)':>10s}  {'Shutter':>10s}  {'Exposure (s)':>12s}")
    for i in range(count):
        entry = data[i * entry_size:(i + 1) * entry_size]
        ts_us = struct.unpack("<I", entry[0:4])[0]
        zero = struct.unpack("<I", entry[4:8])[0]
        shutter = struct.unpack("<I", entry[8:12])[0]
        exposure = struct.unpack("<f", entry[12:16])[0]
        print(f"  {i:6d}  {ts_us:>14d}  {ts_us / 1e6:>10.6f}  0x{shutter:08x}  {exposure:>12.6f}")

    if n > MAX_PREVIEW_ENTRIES:
        # Show a few from the end too
        print(f"  ... ({n - 2 * MAX_PREVIEW_ENTRIES} more entries)")
        print(f"  Last {min(5, n - MAX_PREVIEW_ENTRIES)} entries:")
        for i in range(max(count, n - 5), n):
            entry = data[i * entry_size:(i + 1) * entry_size]
            ts_us = struct.unpack("<I", entry[0:4])[0]
            shutter = struct.unpack("<I", entry[8:12])[0]
            exposure = struct.unpack("<f", entry[12:16])[0]
            print(f"  {i:6d}  {ts_us:>14d}  {ts_us / 1e6:>10.6f}  0x{shutter:08x}  {exposure:>12.6f}")

    # Summary
    if n > 0:
        first_entry = data[:entry_size]
        last_entry = data[(n - 1) * entry_size:n * entry_size]
        first_ts = struct.unpack("<I", first_entry[0:4])[0]
        last_ts = struct.unpack("<I", last_entry[0:4])[0]
        duration_ms = (last_ts - first_ts) / 1000
        print(f"\n  Duration: ~{duration_ms:.1f} ms ({duration_ms / 1000:.3f} s)")
        if n > 1:
            interval_us = (last_ts - first_ts) / (n - 1)
            print(f"  Average interval: ~{interval_us:.0f} us ({1e6 / interval_us:.0f} Hz)")


def display_record_09(rec):
    """Display Record 0x09 (AAAData)."""
    data = rec["data"]
    print(f"=== Record 0x09: AAAData ({rec['size']:,} bytes) ===")
    print("  Auto Exposure / Auto White Balance data")
    print()

    # Try protobuf parse of first portion
    print("  First portion as protobuf fields:")
    pos = 0
    count = 0
    while pos < min(len(data), 200) and count < 30:
        b = data[pos]
        if b == 0:
            pos += 1
            continue
        fn = b >> 3
        wt = b & 0x07
        pos += 1

        if wt == 0:  # varint
            val = 0
            shift = 0
            while pos < len(data):
                b2 = data[pos]
                pos += 1
                val |= (b2 & 0x7F) << shift
                shift += 7
                if not (b2 & 0x80):
                    break
            print(f"    field={fn:2d} varint={val}")
        elif wt == 2:  # length-delimited
            length = 0
            shift = 0
            while pos < len(data):
                b2 = data[pos]
                pos += 1
                length |= (b2 & 0x7F) << shift
                shift += 7
                if not (b2 & 0x80):
                    break
            chunk = data[pos:pos + length]
            pos += length
            s = try_decode_string(chunk)
            if s:
                print(f"    field={fn:2d} string({length})=\"{s}\"")
            elif length <= 20:
                print(f"    field={fn:2d} bytes({length})={chunk.hex()}")
            else:
                print(f"    field={fn:2d} bytes({length})={chunk[:20].hex()}...")
        elif wt == 5:  # 32-bit
            val = struct.unpack("<f", data[pos:pos + 4])[0]
            pos += 4
            print(f"    field={fn:2d} float32={val:.6g}")
        elif wt == 1:  # 64-bit
            val = struct.unpack("<d", data[pos:pos + 8])[0]
            pos += 8
            print(f"    field={fn:2d} float64={val:.6g}")
        else:
            break
        count += 1

    # Also try to find strings in the data
    print("\n  Strings found in data:")
    for m in re.finditer(rb'[\x20-\x7e]{8,}', data):
        print(f"    offset {m.start():5d}: \"{m.group().decode('ascii')}\"")

    print(f"\n  First 128 bytes:")
    print(hex_dump(data, max_bytes=128))


def display_record_0a(rec):
    """Display Record 0x0A (Anchors)."""
    data = rec["data"]
    print(f"=== Record 0x0A: Anchors ({rec['size']:,} bytes) ===")

    # Parse as sequence of int32 values
    n = len(data) // 4
    if n > 0:
        vals = struct.unpack(f"<{n}i", data[:n * 4])
        print(f"  As int32 ({n} values): {vals}")
    if len(data) % 4 != 0:
        print(f"  Remaining bytes: {data[n * 4:].hex()}")


def display_record_00(rec):
    """Display Record 0x00 (Offsets)."""
    data = rec["data"]
    print(f"=== Record 0x00: Offsets ({rec['size']:,} bytes) ===")
    if len(data) >= 4:
        n = len(data) // 4
        vals = struct.unpack(f"<{n}I", data[:n * 4])
        print(f"  As uint32 ({n} values): {vals}")
    print(hex_dump(data, max_bytes=64))


def display_generic_record(rec):
    """Display any other record type with hex dump."""
    data = rec["data"]
    print(f"=== Record 0x{rec['id']:02X}: {rec['name']} ({rec['size']:,} bytes, format={rec['format']}) ===")

    # Check for embedded strings
    strings = []
    for m in re.finditer(rb'[\x20-\x7e]{8,}', data):
        strings.append((m.start(), m.group().decode('ascii')))
    if strings:
        print(f"  Embedded strings ({len(strings)}):")
        for offset, s in strings[:20]:
            print(f"    offset {offset:5d}: \"{s}\"")

    # Check for underscore-delimited numbers (lens cal)
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

            print(f"Records found: {len(records)}")
            for rec in records:
                print(f"  0x{rec['id']:02X} ({rec['name']:20s}): format={rec['format']}, size={rec['size']:,} bytes")
            print()

            # Display each record
            for rec in records:
                displayer = RECORD_DISPLAYERS.get(rec["id"], display_generic_record)
                displayer(rec)
                print()

    except FileNotFoundError:
        print(f"Error: file not found: {filepath}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
