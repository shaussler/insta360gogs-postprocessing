#!/usr/bin/env python3
"""Detect Insta360 metadata records in MP4/INSV files.

Checks for the Insta360 trailer magic and required records:
  - Record 0x01: Metadata (camera model, lens calibration offset_v3)
  - Record 0x03: Gyro (raw IMU data)
  - Record 0x04: Exposure (rolling shutter timestamps)

Exit codes:
  0 = all required records present (can stabilize + FOV)
  1 = missing required records or not an Insta360 file
  2 = error (file not found, etc.)

Output (stdout): JSON with detection results.
"""

import json
import struct
import sys

MAGIC = b"8db42d694ccc418790edff439fe026bf"
HEADER_SIZE = 32 + 4 + 4 + 32  # padding(32) + size(4) + version(4) + magic(32)

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

REQUIRED_RECORDS = {0x01, 0x03, 0x04}


def detect_records(filepath):
    """Detect Insta360 records in a file.

    Returns dict with:
      - has_magic: bool
      - records: list of record ids found
      - missing: list of required record ids that are missing
      - can_process: bool (has_magic and no missing records)
    """
    result = {"has_magic": False, "records": [], "missing": [], "can_process": False}

    try:
        with open(filepath, "rb") as f:
            f.seek(0, 2)
            file_size = f.tell()

            if file_size < HEADER_SIZE:
                return result

            # Check magic at end of file (stored as ASCII text)
            f.seek(file_size - 32)
            magic = f.read(32)
            if magic != MAGIC:
                return result
            result["has_magic"] = True

            # Read extra_size (4 bytes before version, which is before magic)
            f.seek(file_size - 40)
            extra_size = struct.unpack("<I", f.read(4))[0]

            # Scan backwards through records
            offset = HEADER_SIZE + 4 + 1 + 1  # = 78
            max_records = 50  # safety limit

            while offset < extra_size and len(result["records"]) < max_records:
                f.seek(file_size - offset)
                _format = struct.unpack("B", f.read(1))[0]
                rec_id = struct.unpack("B", f.read(1))[0]
                rec_size = struct.unpack("<I", f.read(4))[0]

                if rec_id > 0:
                    result["records"].append(rec_id)

                offset += rec_size + 4 + 1 + 1

    except (IOError, OSError) as e:
        print(f"Error reading file: {e}", file=sys.stderr)
        return result

    result["records"].sort()
    result["missing"] = sorted(REQUIRED_RECORDS - set(result["records"]))
    result["can_process"] = result["has_magic"] and len(result["missing"]) == 0
    return result


def main():
    if len(sys.argv) != 2:
        print("Usage: detect_insta360.py <file>", file=sys.stderr)
        sys.exit(2)

    filepath = sys.argv[1]
    result = detect_records(filepath)
    print(json.dumps(result))
    sys.exit(0 if result["can_process"] else 1)


if __name__ == "__main__":
    main()
