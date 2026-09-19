#!/usr/bin/env python3
"""Extract and display Insta360 metadata from MP4/INSV files.

Parses the Insta360 trailer at the end of the file to extract:
  - Camera info (serial, model, firmware)
  - Lens calibration parameters (focal length, distortion, FOV, etc.)
  - Gyro/exposure record presence and sizes
  - Source file path

Usage:
  python3 extract_insta360_metadata.py <input_file>
"""

import json
import re
import struct
import sys

MAGIC = b"8db42d694ccc418790edff439fe026bf"
TRAILER_SIZE = 32 + 4 + 4 + 32  # padding(32) + extra_size(4) + version(4) + magic(32) = 72

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
    BEFORE its 6-byte header (format + id + size). So:

        ... [data N] [header 6B] [data M] [header 6B] ... [trailer 72B]

    We scan backwards from the trailer, reading each header then its data.
    """
    records = []

    # First record header is at file_size - (TRAILER_SIZE + 6)
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

        # Skip empty offset table entries (id=0, size=0)
        if rec_id == 0 and rec_size == 0:
            header_pos -= 6
            continue

        # Data is BEFORE the header
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

        # Move to the next header (before this record's data)
        header_pos = data_pos - 6

    # Records were read backwards; reverse to get chronological order
    records.reverse()
    return records


def find_metadata_record(records):
    """Find the Record 0x01 (Metadata) in the list."""
    for rec in records:
        if rec["id"] == 0x01:
            return rec
    return None


def parse_protobuf_fields(data):
    """Parse protobuf-like TLV fields from metadata record data.

    Each field has: tag(1 byte) + varint length + value bytes.
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

        # Decode varint for length
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


def is_likely_string(data):
    """Check if bytes look like printable ASCII text."""
    if len(data) < 2:
        return False
    try:
        s = data.decode("ascii")
        printable = sum(1 for c in s if 32 <= ord(c) < 127)
        return printable / len(s) > 0.8
    except (UnicodeDecodeError, ValueError):
        return False


def parse_lens_string(s):
    """Parse an underscore-delimited lens calibration string."""
    parts = s.split("_")
    result = {"raw": s, "parts": parts}

    # Common pattern: lens_id, fx/fy, cx, cy, k1, k2, fov, [k3, k4, k5, ...], width, height, ...
    if len(parts) >= 6:
        try:
            result["lens_index"] = int(parts[0])
        except ValueError:
            pass

    # Try to interpret as known calibration parameters
    floats = []
    for p in parts:
        try:
            floats.append(float(p))
        except ValueError:
            floats.append(None)
    result["numeric"] = floats

    return result


def find_strings_in_data(data):
    """Find all underscore-delimited numeric strings in binary data."""
    text = data.decode("latin-1")
    return re.findall(r'[\d]+_[\d\.eE\-_]{15,}', text)


def decode_field_value(field):
    """Decode a protobuf field value into a human-readable representation."""
    wire = field.get("wire", 0)

    if wire == 0:  # varint
        return str(field["value"])

    data = field.get("bytes", b"")
    if not data:
        return None

    # Try string
    s = try_decode_string(data)
    if s:
        return f'"{s}"'

    # Try float array
    if len(data) >= 4 and len(data) % 4 == 0:
        try:
            vals = struct.unpack(f"<{len(data) // 4}f", data)
            return list(vals)
        except struct.error:
            pass

    # Try double
    if len(data) == 8:
        try:
            val = struct.unpack("<d", data)[0]
            return val
        except struct.error:
            pass

    # Try single float
    if len(data) == 4:
        try:
            val = struct.unpack("<f", data)[0]
            return val
        except struct.error:
            pass

    # Raw hex
    return f"hex:{data.hex()}"


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <input_file>", file=sys.stderr)
        sys.exit(2)

    filepath = sys.argv[1]

    try:
        with open(filepath, "rb") as f:
            f.seek(0, 2)
            file_size = f.tell()

            # Read trailer
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

            # Read all records
            records = read_all_records(f, file_size, trailer["extra_size"])

            print(f"Records found: {len(records)}")
            for rec in records:
                print(f"  0x{rec['id']:02X} ({rec['name']:20s}): format={rec['format']}, size={rec['size']:,} bytes")
            print()

            # Parse metadata record
            meta = find_metadata_record(records)
            if meta is None:
                print("No Record 0x01 (Metadata) found.")
                sys.exit(0)

            fields = parse_protobuf_fields(meta["data"])

            print(f"=== Record 0x01 (Metadata) ===")
            print(f"Total size: {meta['size']:,} bytes")
            print(f"Parsed {len(fields)} protobuf fields")
            print()

            # Camera info
            print("--- Camera Info ---")
            lens_strings = []
            for field in fields:
                val = decode_field_value(field)
                if val is None:
                    continue
                fn = field["field"]
                wire = field["wire"]

                if isinstance(val, str) and val.startswith('"'):
                    s = val.strip('"')
                    if fn == 1:
                        print(f"  Serial number:    {s}")
                    elif fn == 2:
                        print(f"  Camera model:     {s}")
                    elif fn == 3:
                        # Firmware version - trim any trailing junk
                        fw = s.split("*")[0].split("?")[0]
                        print(f"  Firmware version: {fw}")
                    elif fn == 17:
                        print(f"  Unknown string:   {s}")
                    elif "/" in s or ".mp4" in s:
                        print(f"  Source path:      {s}")
                    elif "_" in s and any(c.isdigit() for c in s):
                        lens_strings.append((fn, s))
                elif isinstance(val, int):
                    if fn == 7:
                        print(f"  Timestamp:        {val}")
                    elif fn == 9:
                        print(f"  Extra data offset:{val}")
                    elif fn == 10:
                        print(f"  Duration (s):     {val}")
                    elif fn == 16:
                        print(f"  Unknown int16:    {val}")
                    elif fn == 18:
                        print(f"  Unknown int18:    {val}")
                    elif fn == 20:
                        print(f"  Unknown int20:    {val}")
                    elif fn == 26:
                        print(f"  Unknown int26:    {val}")
                    elif fn == 29:
                        print(f"  Unknown int29:    {val}")
                    else:
                        print(f"  Field {fn:2d} (varint): {val}")

            print()

            # Find all lens calibration strings directly in raw metadata bytes
            meta_text = meta["data"].decode("latin-1")
            lens_patterns = re.findall(r'1_[\d\.eE\-_]{15,}', meta_text)

            # Also find strings starting with non-1 numbers (other calibrations)
            all_cal_strings = re.findall(r'[\d]+_[\d\.eE\-_]{15,}', meta_text)

            # Merge and deduplicate
            seen = set()
            cal_strings = []
            for s in all_cal_strings:
                # Clean trailing non-alphanumeric
                s_clean = s.rstrip("_").rstrip(".")
                if s_clean not in seen and len(s_clean) > 20:
                    seen.add(s_clean)
                    cal_strings.append(s_clean)

            if cal_strings:
                print("--- Lens Calibration Strings (raw) ---")
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

                    for j, (label, val) in enumerate(zip(labels, parts)):
                        print(f"    {label:25s} = {val}")

            # Also dump all non-trivial fields
            print()
            print("--- All Fields ---")
            for field in fields:
                val = decode_field_value(field)
                if val is None:
                    continue
                fn = field["field"]
                wire = field["wire"]

                if isinstance(val, str) and val.startswith('"'):
                    s = val.strip('"')
                    if len(s) < 100:
                        if "/" in s and ".mp4" in s:
                            print(f"  Field {fn:2d} (path):       {s}")
                        elif "_" in s and any(c.isdigit() for c in s):
                            pass  # already shown in lens calibration section
                        else:
                            print(f"  Field {fn:2d} (str):        {s}")
                elif isinstance(val, str) and val.startswith("hex:"):
                    raw_bytes = field.get("bytes", b"")
                    # Try to decode as UTF-8 string (file paths, etc.)
                    try:
                        s = raw_bytes.decode("utf-8")
                        if "/" in s and ".mp4" in s:
                            path = s[:s.index(".mp4") + 4]
                            print(f"  Field {fn:2d} (path):       {path}")
                            continue
                    except (UnicodeDecodeError, ValueError):
                        pass
                    embedded = re.search(rb'(/[\x20-\x7e]+\.mp4)', raw_bytes)
                    if embedded:
                        print(f"  Field {fn:2d} (path):       {embedded.group(1).decode('ascii')}")
                        continue
                    nested = find_strings_in_data(raw_bytes)
                    if nested:
                        pass  # already shown in lens calibration section
                    else:
                        print(f"  Field {fn:2d} (bytes, {len(raw_bytes)}B): {raw_bytes[:80].hex()}...")
                elif isinstance(val, int):
                    if fn == 7:
                        ts = str(val)
                        if len(ts) == 14:
                            print(f"  Field {fn:2d} (timestamp):  {ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:{ts[12:14]}")
                        else:
                            print(f"  Field {fn:2d} (varint):     {val}")
                    elif fn == 10:
                        print(f"  Field {fn:2d} (duration):   {val}s ({val//3600}h{(val%3600)//60}m{val%60}s)")
                    else:
                        print(f"  Field {fn:2d} (varint):     {val}")
                elif isinstance(val, float):
                    print(f"  Field {fn:2d} (float):      {val:.6g}")
                elif isinstance(val, list):
                    if len(val) <= 8:
                        print(f"  Field {fn:2d} (floats x{len(val)}): {[f'{v:.6g}' if v is not None else 'None' for v in val]}")
                    else:
                        print(f"  Field {fn:2d} (floats x{len(val)}): [{val[0]:.6g}, {val[1]:.6g}, ... {val[-1]:.6g}]")
                elif isinstance(val, str) and val.startswith("hex:"):
                    # Already handled above
                    pass

            # Summary of key lens parameters
            if cal_strings:
                print()
                print("=== Summary ===")
                # Use the most complete calibration string (21 fields if available)
                best = max(cal_strings, key=lambda s: len(s.split("_")))
                parts = best.split("_")
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
                elif len(parts) >= 18:
                    print(f"  Lens index:          {parts[0]}")
                    print(f"  Focal length X:      {parts[1]} px")
                    print(f"  Focal length Y:      {parts[2]} px")
                    print(f"  Principal point X:   {parts[3]} px")
                    print(f"  Principal point Y:   {parts[15] if len(parts) > 15 else '?'} px")
                    print(f"  Distortion k1:       {parts[4]}")
                    print(f"  Distortion k2:       {parts[5]}")
                    print(f"  FOV:                 {parts[6]} deg")
                    print(f"  Distortion k3:       {parts[7]}")
                    print(f"  Distortion k4:       {parts[8]}")
                    print(f"  Distortion k5:       {parts[9]}")
                    print(f"  Sensor size:         {parts[14]}x{parts[15]}")
                elif len(parts) >= 10:
                    print(f"  Lens index:          {parts[0]}")
                    print(f"  Focal length X:      {parts[1]} px")
                    print(f"  Focal length Y:      {parts[2]} px")
                    print(f"  Principal point X:   {parts[3]} px")
                    print(f"  Distortion k1:       {parts[4]}")
                    print(f"  Distortion k2:       {parts[5]}")
                    print(f"  FOV:                 {parts[6]} deg")
                    print(f"  Sensor size:         {parts[7]}x{parts[8]}")

    except FileNotFoundError:
        print(f"Error: file not found: {filepath}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
