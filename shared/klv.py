#!/usr/bin/env python3
"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Unified KLV Parser - STANAG 4609 Metadata Processor
Supports both CLI and library interfaces for drone/UAS applications

IMPLEMENTATION NOTE:
-------------------
This module uses manual KLV encoding/decoding rather than the 'klvdata' PyPI library
because klvdata v0.0.3 is parser-only (cannot encode) and contains bugs that cause
crashes on valid STANAG 4609 data.

Conformance notes (MISB ST 0601.19 / Motion Imagery Handbook):
  - Local Set keys are BER-OID encoded (ST 0601.19 s7.1, MIH s7.3.2); lengths are
    BER encoded (MIH s7.3.1). The two encodings differ above 127 and ST 0601
    defines items well past that, so they are handled separately here.
  - Item 1 (Checksum) is required on every packet. It is written on encode and
    verified on decode (ST 0601.19 s6.8).
  - Signed mapped items map -(2^(n-1)-1)..(2^(n-1)-1) onto their range - i.e. they
    scale to (2^n - 2), not (2^n - 1) - and reserve -(2^(n-1)) (0x8000,
    0x80000000, ...) as an "out of range"/error indicator, decoded here as None.
  - Items whose conversion this module does not implement are still identified by
    number and name and returned with their raw bytes and decoded=False, rather
    than being given a guessed conversion.
"""

import json
import sys
import struct
import time
import argparse
import threading
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List
import logging

# Optional dependencies - graceful degradation
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False

# KLV encoding library (optional - currently not used due to bugs in v0.0.3)
# See module docstring for rationale
try:
    from klvdata import StreamParser
    from klvdata.common import datetime_to_bytes
    from klvdata.misb0601 import (
        UASLocalMetadataSet,
        PrecisionTimeStamp,
        MissionID,
        PlatformDesignation,
        PlatformHeadingAngle,
        SensorLatitude,
        SensorLongitude,
        SensorTrueAltitude,
        FrameCenterLatitude,
        FrameCenterLongitude,
        FrameCenterElevation,
    )
    KLVDATA_AVAILABLE = True
except ImportError:
    KLVDATA_AVAILABLE = False
    # Note: This is intentionally silent - klvdata is optional
    # and not currently used in production code


# 16-byte Universal Label for the UAS Datalink Local Set (MISB ST 0601)
UAS_LOCAL_SET_KEY = b'\x06\x0e\x2b\x34\x02\x0b\x01\x01\x0e\x01\x03\x01\x01\x00\x00\x00'

# Value written into item 65 (UAS Datalink LS Version Number)
ST0601_VERSION = 19

# Enumerations (ST 0601.19 sections 8.34, 8.63, 8.77)
ICING_DETECTED = {
    0: 'Detector off',
    1: 'No icing detected',
    2: 'Icing detected',
}

SENSOR_FOV_NAME = {
    0: 'Ultranarrow',
    1: 'Narrow',
    2: 'Medium',
    3: 'Wide',
    4: 'Ultrawide',
    5: 'Narrow Medium',
    6: '2x Ultranarrow',
    7: '4x Ultranarrow',
}

OPERATIONAL_MODE = {
    0: 'Other',
    1: 'Operational',
    2: 'Training',
    3: 'Exercise',
    4: 'Maintenance',
    5: 'Test',
}


def _spec(name, kind, **kwargs):
    """Build an ST 0601 item specification."""
    spec = {'name': name, 'kind': kind}
    spec.update(kwargs)
    return spec


# Item kinds:
#   uint / int      - plain integer, optional 'scale' multiplier
#   uint_mapped     - map 0..(2^n - 1) onto [lo, hi]
#   int_mapped      - map -(2^(n-1)-1)..(2^(n-1)-1) onto [-hi, hi];
#                     -(2^(n-1)) is the "out of range"/error indicator
#   string          - UTF-8 text
#   enum            - integer with a lookup table
#   flags           - bit field, reported as an integer
#   timestamp       - uint64 microseconds since the UNIX epoch
#   set             - nested local set (not expanded here)
#   pack            - defined pack/array structure (not expanded here)
#   opaque          - item is defined by ST 0601 but this module does not
#                     implement its conversion; raw bytes are returned
ST0601_ITEMS = {
    1: _spec('Checksum', 'uint', length=2),
    2: _spec('Precision Time Stamp', 'timestamp', length=8, units='microseconds'),
    3: _spec('Mission ID', 'string'),
    4: _spec('Platform Tail Number', 'string'),
    5: _spec('Platform Heading Angle', 'uint_mapped', length=2, lo=0.0, hi=360.0, units='degrees'),
    6: _spec('Platform Pitch Angle', 'int_mapped', length=2, hi=20.0, units='degrees', full_range_tag=90),
    7: _spec('Platform Roll Angle', 'int_mapped', length=2, hi=50.0, units='degrees', full_range_tag=91),
    8: _spec('Platform True Airspeed', 'uint', length=1, units='m/s'),
    9: _spec('Platform Indicated Airspeed', 'uint', length=1, units='m/s'),
    10: _spec('Platform Designation', 'string'),
    11: _spec('Image Source Sensor', 'string'),
    12: _spec('Image Coordinate System', 'string'),
    13: _spec('Sensor Latitude', 'int_mapped', length=4, hi=90.0, units='degrees'),
    14: _spec('Sensor Longitude', 'int_mapped', length=4, hi=180.0, units='degrees'),
    15: _spec('Sensor True Altitude', 'uint_mapped', length=2, lo=-900.0, hi=19000.0, units='meters'),
    16: _spec('Sensor Horizontal Field of View', 'uint_mapped', length=2, lo=0.0, hi=180.0, units='degrees'),
    17: _spec('Sensor Vertical Field of View', 'uint_mapped', length=2, lo=0.0, hi=180.0, units='degrees'),
    18: _spec('Sensor Relative Azimuth Angle', 'uint_mapped', length=4, lo=0.0, hi=360.0, units='degrees'),
    19: _spec('Sensor Relative Elevation Angle', 'int_mapped', length=4, hi=180.0, units='degrees'),
    20: _spec('Sensor Relative Roll Angle', 'uint_mapped', length=4, lo=0.0, hi=360.0, units='degrees'),
    21: _spec('Slant Range', 'uint_mapped', length=4, lo=0.0, hi=5000000.0, units='meters'),
    22: _spec('Target Width', 'uint_mapped', length=2, lo=0.0, hi=10000.0, units='meters'),
    23: _spec('Frame Center Latitude', 'int_mapped', length=4, hi=90.0, units='degrees'),
    24: _spec('Frame Center Longitude', 'int_mapped', length=4, hi=180.0, units='degrees'),
    25: _spec('Frame Center Elevation', 'uint_mapped', length=2, lo=-900.0, hi=19000.0, units='meters'),
    # Offset corners are 2-byte offsets of +/-0.075 degrees applied to the frame
    # centre (items 23/24) - not absolute coordinates. ST 0601.19 s8.26-8.33.
    26: _spec('Offset Corner Latitude Point 1', 'int_mapped', length=2, hi=0.075, units='degrees', offset_from=23),
    27: _spec('Offset Corner Longitude Point 1', 'int_mapped', length=2, hi=0.075, units='degrees', offset_from=24),
    28: _spec('Offset Corner Latitude Point 2', 'int_mapped', length=2, hi=0.075, units='degrees', offset_from=23),
    29: _spec('Offset Corner Longitude Point 2', 'int_mapped', length=2, hi=0.075, units='degrees', offset_from=24),
    30: _spec('Offset Corner Latitude Point 3', 'int_mapped', length=2, hi=0.075, units='degrees', offset_from=23),
    31: _spec('Offset Corner Longitude Point 3', 'int_mapped', length=2, hi=0.075, units='degrees', offset_from=24),
    32: _spec('Offset Corner Latitude Point 4', 'int_mapped', length=2, hi=0.075, units='degrees', offset_from=23),
    33: _spec('Offset Corner Longitude Point 4', 'int_mapped', length=2, hi=0.075, units='degrees', offset_from=24),
    34: _spec('Icing Detected', 'enum', length=1, values=ICING_DETECTED),
    35: _spec('Wind Direction', 'uint_mapped', length=2, lo=0.0, hi=360.0, units='degrees'),
    36: _spec('Wind Speed', 'uint_mapped', length=1, lo=0.0, hi=100.0, units='m/s'),
    37: _spec('Static Pressure', 'uint_mapped', length=2, lo=0.0, hi=5000.0, units='millibar'),
    38: _spec('Density Altitude', 'uint_mapped', length=2, lo=-900.0, hi=19000.0, units='meters'),
    39: _spec('Outside Air Temperature', 'int', length=1, units='celsius'),
    40: _spec('Target Location Latitude', 'int_mapped', length=4, hi=90.0, units='degrees'),
    41: _spec('Target Location Longitude', 'int_mapped', length=4, hi=180.0, units='degrees'),
    42: _spec('Target Location Elevation', 'uint_mapped', length=2, lo=-900.0, hi=19000.0, units='meters'),
    43: _spec('Target Track Gate Width', 'uint', length=1, scale=2, units='pixels'),
    44: _spec('Target Track Gate Height', 'uint', length=1, scale=2, units='pixels'),
    45: _spec('Target Error Estimate - CE90', 'uint_mapped', length=2, lo=0.0, hi=4095.0, units='meters'),
    46: _spec('Target Error Estimate - LE90', 'uint_mapped', length=2, lo=0.0, hi=4095.0, units='meters'),
    47: _spec('Generic Flag Data 01', 'flags', length=1),
    48: _spec('Security Local Set', 'set', standard='MISB ST 0102'),
    49: _spec('Differential Pressure', 'uint_mapped', length=2, lo=0.0, hi=5000.0, units='millibar'),
    50: _spec('Platform Angle of Attack', 'int_mapped', length=2, hi=20.0, units='degrees', full_range_tag=92),
    51: _spec('Platform Vertical Speed', 'int_mapped', length=2, hi=180.0, units='m/s'),
    52: _spec('Platform Sideslip Angle', 'int_mapped', length=2, hi=20.0, units='degrees', full_range_tag=93),
    53: _spec('Airfield Barometric Pressure', 'uint_mapped', length=2, lo=0.0, hi=5000.0, units='millibar'),
    54: _spec('Airfield Elevation', 'uint_mapped', length=2, lo=-900.0, hi=19000.0, units='meters'),
    55: _spec('Relative Humidity', 'uint_mapped', length=1, lo=0.0, hi=100.0, units='percent'),
    56: _spec('Platform Ground Speed', 'uint', length=1, units='m/s'),
    57: _spec('Ground Range', 'uint_mapped', length=4, lo=0.0, hi=5000000.0, units='meters'),
    58: _spec('Platform Fuel Remaining', 'uint_mapped', length=2, lo=0.0, hi=10000.0, units='kilograms'),
    59: _spec('Platform Call Sign', 'string'),
    60: _spec('Weapon Load', 'uint', length=2),
    61: _spec('Weapon Fired', 'uint', length=1),
    62: _spec('Laser PRF Code', 'uint', length=2),
    63: _spec('Sensor Field of View Name', 'enum', length=1, values=SENSOR_FOV_NAME),
    64: _spec('Platform Magnetic Heading', 'uint_mapped', length=2, lo=0.0, hi=360.0, units='degrees'),
    65: _spec('UAS Datalink LS Version Number', 'uint', length=1),
    66: _spec('Target Location Covariance Matrix', 'opaque', deprecated=True),
    67: _spec('Alternate Platform Latitude', 'int_mapped', length=4, hi=90.0, units='degrees'),
    68: _spec('Alternate Platform Longitude', 'int_mapped', length=4, hi=180.0, units='degrees'),
    69: _spec('Alternate Platform Altitude', 'uint_mapped', length=2, lo=-900.0, hi=19000.0, units='meters'),
    70: _spec('Alternate Platform Name', 'string'),
    71: _spec('Alternate Platform Heading', 'uint_mapped', length=2, lo=0.0, hi=360.0, units='degrees'),
    72: _spec('Event Start Time - UTC', 'timestamp', length=8, units='microseconds'),
    73: _spec('RVT Local Set', 'set', standard='MISB ST 0806'),
    74: _spec('VMTI Local Set', 'set', standard='MISB ST 0903'),
    75: _spec('Sensor Ellipsoid Height', 'uint_mapped', length=2, lo=-900.0, hi=19000.0, units='meters'),
    76: _spec('Alternate Platform Ellipsoid Height', 'uint_mapped', length=2, lo=-900.0, hi=19000.0, units='meters'),
    77: _spec('Operational Mode', 'enum', length=1, values=OPERATIONAL_MODE),
    78: _spec('Frame Center Height Above Ellipsoid', 'uint_mapped', length=2, lo=-900.0, hi=19000.0, units='meters'),
    79: _spec('Sensor North Velocity', 'int_mapped', length=2, hi=327.0, units='m/s'),
    80: _spec('Sensor East Velocity', 'int_mapped', length=2, hi=327.0, units='m/s'),
    81: _spec('Image Horizon Pixel Pack', 'pack'),
    82: _spec('Corner Latitude Point 1 (Full)', 'int_mapped', length=4, hi=90.0, units='degrees'),
    83: _spec('Corner Longitude Point 1 (Full)', 'int_mapped', length=4, hi=180.0, units='degrees'),
    84: _spec('Corner Latitude Point 2 (Full)', 'int_mapped', length=4, hi=90.0, units='degrees'),
    85: _spec('Corner Longitude Point 2 (Full)', 'int_mapped', length=4, hi=180.0, units='degrees'),
    86: _spec('Corner Latitude Point 3 (Full)', 'int_mapped', length=4, hi=90.0, units='degrees'),
    87: _spec('Corner Longitude Point 3 (Full)', 'int_mapped', length=4, hi=180.0, units='degrees'),
    88: _spec('Corner Latitude Point 4 (Full)', 'int_mapped', length=4, hi=90.0, units='degrees'),
    89: _spec('Corner Longitude Point 4 (Full)', 'int_mapped', length=4, hi=180.0, units='degrees'),
    90: _spec('Platform Pitch Angle (Full)', 'int_mapped', length=4, hi=90.0, units='degrees'),
    91: _spec('Platform Roll Angle (Full)', 'int_mapped', length=4, hi=90.0, units='degrees'),
    92: _spec('Platform Angle of Attack (Full)', 'int_mapped', length=4, hi=90.0, units='degrees'),
    93: _spec('Platform Sideslip Angle (Full)', 'int_mapped', length=4, hi=180.0, units='degrees'),
    94: _spec('MIIS Core Identifier', 'opaque', standard='MISB ST 1204'),
    95: _spec('SAR Motion Imagery Local Set', 'set', standard='MISB ST 1206'),
    96: _spec('Target Width Extended', 'opaque', units='meters'),
    97: _spec('Range Image Local Set', 'set', standard='MISB ST 1002'),
    98: _spec('Geo-Registration Local Set', 'set', standard='MISB ST 1601'),
    99: _spec('Composite Imaging Local Set', 'set', standard='MISB ST 1602'),
    100: _spec('Segment Local Set', 'set', standard='MISB ST 1607'),
    101: _spec('Amend Local Set', 'set', standard='MISB ST 1607'),
    102: _spec('SDCC-FLP', 'pack', standard='MISB ST 1010', repeatable=True),
    103: _spec('Density Altitude Extended', 'opaque', units='meters'),
    104: _spec('Sensor Ellipsoid Height Extended', 'opaque', units='meters'),
    105: _spec('Alternate Platform Ellipsoid Height Extended', 'opaque', units='meters'),
    106: _spec('Stream Designator', 'string'),
    107: _spec('Operational Base', 'string'),
    108: _spec('Broadcast Source', 'string'),
    109: _spec('Range to Recovery Location', 'opaque', units='kilometers'),
    110: _spec('Time Airborne', 'uint', units='seconds'),
    111: _spec('Propulsion Unit Speed', 'uint', units='rpm'),
    112: _spec('Platform Course Angle', 'opaque', units='degrees'),
    113: _spec('Altitude AGL', 'opaque', units='meters'),
    114: _spec('Radar Altimeter', 'opaque', units='meters'),
    115: _spec('Control Command', 'pack'),
    116: _spec('Control Command Verification List', 'pack'),
    117: _spec('Sensor Azimuth Rate', 'opaque', units='degrees/second'),
    118: _spec('Sensor Elevation Rate', 'opaque', units='degrees/second'),
    119: _spec('Sensor Roll Rate', 'opaque', units='degrees/second'),
    120: _spec('On-board MI Storage Percent Full', 'opaque', units='percent'),
    121: _spec('Active Wavelength List', 'pack'),
    122: _spec('Country Codes', 'pack'),
    123: _spec('Number of NAVSATs in View', 'uint'),
    124: _spec('Positioning Method Source', 'flags'),
    125: _spec('Platform Status', 'uint'),
    126: _spec('Sensor Control Mode', 'uint'),
    127: _spec('Sensor Frame Rate Pack', 'pack'),
    128: _spec('Wavelengths List', 'pack', repeatable=True),
    129: _spec('Target ID', 'string'),
    130: _spec('Airbase Locations', 'pack'),
    131: _spec('Takeoff Time', 'uint', units='microseconds'),
    132: _spec('Transmission Frequency', 'opaque', units='megahertz'),
    133: _spec('On-board MI Storage Capacity', 'uint', units='gigabytes'),
    134: _spec('Zoom Percentage', 'opaque', units='percent'),
    135: _spec('Communications Method', 'string'),
    136: _spec('Leap Seconds', 'int', units='seconds'),
    137: _spec('Correction Offset', 'int', units='microseconds'),
    138: _spec('Payload List', 'pack'),
    139: _spec('Active Payloads', 'pack'),
    140: _spec('Weapons Stores', 'pack', repeatable=True),
    141: _spec('Waypoint List', 'pack', repeatable=True),
    142: _spec('View Domain', 'pack'),
}

# Backwards-compatible name lookup (this used to be the whole tag table)
STANAG_4609_TAGS = {tag: spec['name'] for tag, spec in ST0601_ITEMS.items()}

# Items ST 0601.19 Table 1 permits more than once in a single Local Set. Packet
# order is significant for these, so decoded packets keep an ordered item list
# alongside the name-keyed dictionary.
REPEATABLE_TAGS = {tag for tag, spec in ST0601_ITEMS.items() if spec.get('repeatable')}

# Items that must not be generated in new metadata
DEPRECATED_TAGS = {tag for tag, spec in ST0601_ITEMS.items() if spec.get('deprecated')}


# --------------------------------------------------------------------------
# BER / BER-OID encoding helpers (Motion Imagery Handbook s7.3.1, s7.3.2)
# --------------------------------------------------------------------------

def encode_ber_length(length: int) -> bytes:
    """Encode a BER length (short form under 128, else long form)."""
    if length < 0:
        raise ValueError('length must not be negative')
    if length < 128:
        return bytes([length])
    payload = length.to_bytes((length.bit_length() + 7) // 8, 'big')
    return bytes([0x80 | len(payload)]) + payload


def decode_ber_length(data: bytes, offset: int = 0) -> Tuple[Optional[int], int]:
    """Decode a BER length. Returns (length, new_offset); length is None if truncated."""
    if offset >= len(data):
        return None, offset
    first = data[offset]
    offset += 1
    if first & 0x80 == 0:
        return first, offset
    count = first & 0x7F
    if count == 0 or offset + count > len(data):
        return None, offset
    value = int.from_bytes(data[offset:offset + count], 'big')
    return value, offset + count


def encode_ber_oid(value: int) -> bytes:
    """Encode a BER-OID key (7 bits per byte, MSB set on all but the last)."""
    if value < 0:
        raise ValueError('BER-OID values must not be negative')
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, 0x80 | (value & 0x7F))
        value >>= 7
    return bytes(out)


def decode_ber_oid(data: bytes, offset: int = 0) -> Tuple[Optional[int], int]:
    """Decode a BER-OID key. Returns (value, new_offset); value is None if truncated."""
    value = 0
    start = offset
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, offset
        if offset - start > 8:  # runaway continuation bits
            return None, offset
    return None, offset


def compute_checksum(packet: bytes) -> int:
    """
    16-bit checksum over a UAS Local Set packet (ST 0601.19 s6.8).

    `packet` runs from the first byte of the 16-byte Universal Label through the
    1-byte length of the checksum item itself (i.e. everything except the two
    checksum value bytes).
    """
    bcc = 0
    for i, byte in enumerate(packet):
        bcc = (bcc + (byte << (8 * ((i + 1) % 2)))) & 0xFFFF
    return bcc


# --------------------------------------------------------------------------
# Value conversion helpers
# --------------------------------------------------------------------------

def _be_uint(data: bytes) -> int:
    return int.from_bytes(data, 'big', signed=False)


def _be_int(data: bytes) -> int:
    return int.from_bytes(data, 'big', signed=True)


def decode_uint_mapped(data: bytes, lo: float, hi: float) -> float:
    """Map 0..(2^n - 1) onto [lo, hi]."""
    span = (1 << (8 * len(data))) - 1
    return lo + _be_uint(data) * (hi - lo) / float(span)


def encode_uint_mapped(value: float, lo: float, hi: float, length: int) -> bytes:
    """Inverse of decode_uint_mapped, clamped to [lo, hi]."""
    span = (1 << (8 * length)) - 1
    clamped = max(lo, min(hi, float(value)))
    raw = int(round((clamped - lo) * span / (hi - lo)))
    return max(0, min(span, raw)).to_bytes(length, 'big')


def decode_int_mapped(data: bytes, hi: float) -> Optional[float]:
    """
    Map -(2^(n-1) - 1)..(2^(n-1) - 1) onto [-hi, hi].

    The scale factor is (2^n - 2), not (2^n - 1), and -(2^(n-1)) is reserved as
    the "out of range"/error indicator - returned as None.
    """
    bits = 8 * len(data)
    raw = _be_int(data)
    if raw == -(1 << (bits - 1)):
        return None
    return raw * (2.0 * hi) / float((1 << bits) - 2)


def encode_int_mapped(value: Optional[float], hi: float, length: int) -> bytes:
    """Inverse of decode_int_mapped. None encodes the out-of-range indicator."""
    bits = 8 * length
    if value is None:
        return (-(1 << (bits - 1))).to_bytes(length, 'big', signed=True)
    limit = (1 << (bits - 1)) - 1
    clamped = max(-hi, min(hi, float(value)))
    raw = int(round(clamped * ((1 << bits) - 2) / (2.0 * hi)))
    return max(-limit, min(limit, raw)).to_bytes(length, 'big', signed=True)


def _decode_string(data: bytes) -> str:
    return data.decode('utf-8', errors='replace').rstrip('\x00')


class UnifiedKLVParser:
    """
    Unified KLV Parser supporting both CLI and library interfaces
    Implements MISB ST 0601 UAS Datalink Local Set parsing
    """

    def __init__(self, stream_name: str = None, api_url: str = "http://localhost:3000"):
        self.stream_name = stream_name
        self.api_url = api_url
        self.raw_format = 'hex'  # Default format for raw values: 'hex' or 'decimal'
        self.logger = self._setup_logging()

    def _setup_logging(self) -> logging.Logger:
        """Setup logging configuration"""
        return logging.getLogger('UnifiedKLVParser')

    def parse_ber_length(self, data: bytes, offset: int) -> Tuple[int, int]:
        """Parse a BER length field. Returns (length, new_offset)."""
        length, new_offset = decode_ber_length(data, offset)
        return (0 if length is None else length), new_offset

    def parse_ber_oid(self, data: bytes, offset: int) -> Tuple[Optional[int], int]:
        """Parse a BER-OID item key. Returns (tag, new_offset)."""
        return decode_ber_oid(data, offset)

    def parse_klv_packet(self, data: bytes) -> Dict[str, Any]:
        """
        Parse a complete UAS Datalink Local Set packet.

        Returns a dict with:
          items          - every item in packet order, including repeats
          tags           - name-keyed view of items (repeats get a ' #n' suffix)
          checksum_valid - True/False, or None when the packet carries no checksum
        """
        result = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'stream_name': self.stream_name,
            'items': [],
            'tags': {},
            'raw_size': len(data),
            'checksum_valid': None,
        }

        if len(data) < 17:  # 16-byte key + at least one length byte
            result['error'] = 'Packet too small'
            return result

        if data[:16] != UAS_LOCAL_SET_KEY:
            result['error'] = 'Not a UAS Datalink Local Set packet'
            return result

        try:
            length, offset = decode_ber_length(data, 16)
            if length is None:
                result['error'] = 'Invalid length field'
                return result

            end_offset = offset + length
            if end_offset > len(data):
                result['error'] = 'Invalid length field'
                return result

            result['raw_size'] = end_offset

            while offset < end_offset:
                # Item keys are BER-OID encoded, item lengths are BER encoded
                tag, offset = decode_ber_oid(data, offset)
                if tag is None or offset >= end_offset:
                    break

                item_length, offset = decode_ber_length(data, offset)
                if item_length is None or offset + item_length > end_offset:
                    result['error'] = f'Truncated value for item {tag}'
                    break

                item_data = data[offset:offset + item_length]

                if tag == 1 and item_length == 2:
                    # Checksum covers everything up to and including its own length byte
                    expected = compute_checksum(data[:offset])
                    result['checksum_valid'] = (expected == _be_uint(item_data))
                    result['checksum_expected'] = expected

                offset += item_length
                result['items'].append(self._decode_item(tag, item_data))

            self._resolve_offset_corners(result['items'])
            result['tags'] = self._index_items(result['items'])

        except Exception as e:
            result['error'] = f'Parse error: {str(e)}'

        return result

    def _format_raw(self, data: bytes):
        """Raw value in the configured representation"""
        if self.raw_format == 'decimal':
            return int.from_bytes(data, byteorder='big', signed=False)
        return data.hex()

    def _decode_item(self, tag: int, data: bytes) -> Dict[str, Any]:
        """Decode one Local Set item into a descriptive dict."""
        spec = ST0601_ITEMS.get(tag)
        raw_value = self._format_raw(data)
        item = {
            'tag_id': tag,
            'name': spec['name'] if spec else f'Unknown Tag {tag}',
            'value': raw_value,
            'raw_value': raw_value,
            'raw_length': len(data),
            'decoded': False,
        }
        if spec is None:
            return item
        if spec.get('units'):
            item['units'] = spec['units']
        if spec.get('deprecated'):
            item['deprecated'] = True

        value, decoded = self._decode_value(spec, data)
        item['decoded'] = decoded
        if decoded and value is None:
            # Signed mapped items use -(2^(n-1)) to say "out of range"
            item['value'] = None
            item['out_of_range'] = True
        elif value is not None:
            item['value'] = value
        if spec.get('offset_from'):
            item['offset_from_tag'] = spec['offset_from']
        return item

    def _decode_value(self, spec: Dict[str, Any], data: bytes) -> Tuple[Any, bool]:
        """
        Convert an item's bytes to a value.

        Returns (value, decoded). decoded is False when this module has no
        conversion for the item or the length does not match the specification.
        """
        kind = spec['kind']
        if not data:
            return None, False

        expected_length = spec.get('length')
        if expected_length is not None and len(data) != expected_length:
            return None, False

        try:
            if kind == 'string':
                return _decode_string(data), True
            if kind == 'timestamp':
                return _be_uint(data) / 1000000.0, True
            if kind == 'uint':
                return _be_uint(data) * spec.get('scale', 1), True
            if kind == 'int':
                return _be_int(data) * spec.get('scale', 1), True
            if kind == 'flags':
                return _be_uint(data), True
            if kind == 'enum':
                code = _be_uint(data)
                return {'code': code, 'name': spec['values'].get(code, 'Reserved')}, True
            if kind == 'uint_mapped':
                return decode_uint_mapped(data, spec['lo'], spec['hi']), True
            if kind == 'int_mapped':
                return decode_int_mapped(data, spec['hi']), True
            if kind in ('set', 'pack'):
                standard = spec.get('standard')
                label = f"{spec['name']} ({len(data)} bytes"
                label += f", {standard})" if standard else ')'
                return label, False
        except Exception:
            return None, False

        return None, False

    def _resolve_offset_corners(self, items: List[Dict[str, Any]]):
        """
        Add absolute coordinates for the offset corner items (26-33).

        Those items carry a +/-0.075 degree offset from the frame centre, so the
        absolute corner is only meaningful alongside items 23/24.
        """
        centres = {}
        for item in items:
            if item['tag_id'] in (23, 24) and item.get('decoded') and isinstance(item.get('value'), float):
                centres[item['tag_id']] = item['value']
        for item in items:
            base = item.get('offset_from_tag')
            if base in centres and isinstance(item.get('value'), float):
                item['absolute_value'] = centres[base] + item['value']

    @staticmethod
    def _index_items(items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Name-keyed view of the item list.

        Some ST 0601 items may legitimately appear more than once in a packet
        (ST 0601.19 Table 1); repeats are suffixed rather than overwritten so no
        data is lost through the dictionary view.
        """
        indexed: Dict[str, Any] = {}
        counts: Dict[str, int] = {}
        for position, item in enumerate(items):
            name = item['name']
            counts[name] = counts.get(name, 0) + 1
            key = name if counts[name] == 1 else f"{name} #{counts[name]}"
            entry = dict(item)
            entry['position'] = position
            indexed[key] = entry
        return indexed

    def _parse_tag_value(self, tag: int, data: bytes) -> Tuple[Any, Any]:
        """
        Parse an individual item value (MISB ST 0601.19).
        Returns: (decoded_value, raw_value_in_specified_format)
        """
        if len(data) == 0:
            return None, None
        item = self._decode_item(tag, data)
        return item['value'], item['raw_value']

    def send_to_api(self, klv_data: Dict[str, Any]) -> bool:
        """Send parsed KLV data to the API"""
        if not REQUESTS_AVAILABLE:
            self.logger.warning("Requests library not available, cannot send to API")
            return False

        try:
            url = f"{self.api_url}/api/klv/{self.stream_name}"
            response = requests.post(url, json=klv_data, timeout=5)
            response.raise_for_status()
            return True
        except Exception as e:
            self.logger.error(f"Failed to send KLV data to API: {e}")
            return False

    def generate_test_klv(self) -> bytes:
        """Generate a test KLV packet for validation"""
        return encode_uas_metadata({
            'timestamp': int(time.time() * 1000000),
            'mission_id': 'TEST_MISSION_001',
            'platform_designation': 'Test Platform',
            'image_source_sensor': 'EO Nose',
            'platform_heading': 90.0,
            'platform_pitch': -0.4315251,
            'platform_roll': 3.405814,
            'sensor_latitude': 40.7128,
            'sensor_longitude': -74.0060,
            'sensor_altitude': 1000.0,
            'sensor_hfov': 20.0,
            'sensor_vfov': 12.0,
            'frame_center_latitude': 40.7128,
            'frame_center_longitude': -74.0060,
            'frame_center_elevation': 100.0,
        })

    def run_test_mode(self):
        """Run parser in test mode"""
        print("=== Unified KLV Parser Test Mode ===")

        # Generate test packet
        test_packet = self.generate_test_klv()
        print(f"Generated test packet: {len(test_packet)} bytes")

        # Parse the packet
        parsed = self.parse_klv_packet(test_packet)

        # Display results
        print(f"\nChecksum valid: {parsed.get('checksum_valid')}")
        print("\nParsed KLV Data:")
        print(json.dumps(parsed, indent=2))

        # Test API submission if available
        if self.stream_name and REQUESTS_AVAILABLE:
            print(f"\nTesting API submission to stream: {self.stream_name}")
            success = self.send_to_api(parsed)
            print(f"API submission: {'SUCCESS' if success else 'FAILED'}")

    def run_stream_mode(self):
        """Run parser in stream processing mode"""
        print(f"=== Streaming KLV Parser for {self.stream_name} ===")

        # In a real implementation, this would:
        # 1. Connect to the stream source
        # 2. Extract KLV data from the stream
        # 3. Parse and forward to API
        # 4. Handle errors gracefully

        # For now, simulate with periodic test data
        try:
            while True:
                test_packet = self.generate_test_klv()
                parsed = self.parse_klv_packet(test_packet)

                print(f"[{datetime.now().strftime('%H:%M:%S')}] Processed KLV packet")

                if self.send_to_api(parsed):
                    print(f"  -> Sent to API for stream: {self.stream_name}")
                else:
                    print(f"  -> Failed to send to API")

                time.sleep(2)  # 2Hz update rate

        except KeyboardInterrupt:
            print("\nShutting down KLV parser...")


def _encode_item(tag: int, value: bytes) -> bytes:
    """Encode one Local Set item: BER-OID key, BER length, value."""
    return encode_ber_oid(tag) + encode_ber_length(len(value)) + value


def _encode_string_item(tag: int, text: str, max_bytes: int = 127) -> bytes:
    return _encode_item(tag, str(text).encode('utf-8')[:max_bytes])


def _encode_angle(metadata: Dict[str, Any], key: str,
                  legacy_tag: int, full_tag: int) -> bytes:
    """
    Encode a platform angle using one representation only.

    ST 0601.19 s6.3 prefers the full-range item; the range-restricted item is
    kept for values it can actually carry so legacy readers still see them.
    """
    value = float(metadata[key])
    legacy_hi = ST0601_ITEMS[legacy_tag]['hi']
    if abs(value) <= legacy_hi:
        return _encode_item(legacy_tag, encode_int_mapped(value, legacy_hi, 2))
    return _encode_item(full_tag, encode_int_mapped(value, ST0601_ITEMS[full_tag]['hi'], 4))


def encode_uas_metadata(metadata: Dict[str, Any]) -> bytes:
    """
    Encode UAS metadata into a MISB ST 0601 UAS Datalink Local Set packet.

    Items are written in ascending order and the packet always carries the two
    items ST 0601 requires: item 2 (Precision Time Stamp) and item 1 (Checksum),
    plus item 65 (version number). The deprecated item 66 is never generated.

    Args:
        metadata: dict of UAS metadata fields
            timestamp (microseconds since epoch; defaults to now)
            mission_id, platform_tail_number, platform_designation,
            image_source_sensor, image_coordinate_system, platform_call_sign
            platform_heading, platform_pitch, platform_roll (degrees)
            platform_true_airspeed, platform_ground_speed (m/s)
            sensor_latitude, sensor_longitude (degrees), sensor_altitude (m)
            sensor_hfov, sensor_vfov (degrees)
            sensor_relative_azimuth, sensor_relative_elevation,
            sensor_relative_roll (degrees), slant_range (m)
            frame_center_latitude, frame_center_longitude (degrees),
            frame_center_elevation (m)
            target_latitude, target_longitude (degrees), target_elevation (m)
            operational_mode (0-5), core_identifier (ST 1204 bytes or hex string)
            version (ST 0601 revision written to item 65)

    Returns:
        KLV packet bytes
    """
    items: List[bytes] = []

    timestamp = int(metadata.get('timestamp', time.time() * 1000000))
    items.append(_encode_item(2, struct.pack('>Q', timestamp)))

    if 'mission_id' in metadata:
        items.append(_encode_string_item(3, metadata['mission_id']))
    if 'platform_tail_number' in metadata:
        items.append(_encode_string_item(4, metadata['platform_tail_number']))
    if 'platform_heading' in metadata:
        items.append(_encode_item(5, encode_uint_mapped(metadata['platform_heading'], 0.0, 360.0, 2)))
    if 'platform_pitch' in metadata:
        items.append(_encode_angle(metadata, 'platform_pitch', 6, 90))
    if 'platform_roll' in metadata:
        items.append(_encode_angle(metadata, 'platform_roll', 7, 91))
    if 'platform_true_airspeed' in metadata:
        items.append(_encode_item(8, bytes([max(0, min(255, int(metadata['platform_true_airspeed'])))])))
    if 'platform_designation' in metadata:
        items.append(_encode_string_item(10, metadata['platform_designation']))
    if 'image_source_sensor' in metadata:
        items.append(_encode_string_item(11, metadata['image_source_sensor']))
    if 'image_coordinate_system' in metadata:
        items.append(_encode_string_item(12, metadata['image_coordinate_system']))
    if 'sensor_latitude' in metadata:
        items.append(_encode_item(13, encode_int_mapped(metadata['sensor_latitude'], 90.0, 4)))
    if 'sensor_longitude' in metadata:
        items.append(_encode_item(14, encode_int_mapped(metadata['sensor_longitude'], 180.0, 4)))
    if 'sensor_altitude' in metadata:
        items.append(_encode_item(15, encode_uint_mapped(metadata['sensor_altitude'], -900.0, 19000.0, 2)))
    if 'sensor_hfov' in metadata:
        items.append(_encode_item(16, encode_uint_mapped(metadata['sensor_hfov'], 0.0, 180.0, 2)))
    if 'sensor_vfov' in metadata:
        items.append(_encode_item(17, encode_uint_mapped(metadata['sensor_vfov'], 0.0, 180.0, 2)))
    if 'sensor_relative_azimuth' in metadata:
        items.append(_encode_item(18, encode_uint_mapped(metadata['sensor_relative_azimuth'], 0.0, 360.0, 4)))
    if 'sensor_relative_elevation' in metadata:
        items.append(_encode_item(19, encode_int_mapped(metadata['sensor_relative_elevation'], 180.0, 4)))
    if 'sensor_relative_roll' in metadata:
        items.append(_encode_item(20, encode_uint_mapped(metadata['sensor_relative_roll'], 0.0, 360.0, 4)))
    if 'slant_range' in metadata:
        items.append(_encode_item(21, encode_uint_mapped(metadata['slant_range'], 0.0, 5000000.0, 4)))
    if 'frame_center_latitude' in metadata:
        items.append(_encode_item(23, encode_int_mapped(metadata['frame_center_latitude'], 90.0, 4)))
    if 'frame_center_longitude' in metadata:
        items.append(_encode_item(24, encode_int_mapped(metadata['frame_center_longitude'], 180.0, 4)))
    if 'frame_center_elevation' in metadata:
        items.append(_encode_item(25, encode_uint_mapped(metadata['frame_center_elevation'], -900.0, 19000.0, 2)))
    if 'target_latitude' in metadata:
        items.append(_encode_item(40, encode_int_mapped(metadata['target_latitude'], 90.0, 4)))
    if 'target_longitude' in metadata:
        items.append(_encode_item(41, encode_int_mapped(metadata['target_longitude'], 180.0, 4)))
    if 'target_elevation' in metadata:
        items.append(_encode_item(42, encode_uint_mapped(metadata['target_elevation'], -900.0, 19000.0, 2)))
    if 'platform_ground_speed' in metadata:
        items.append(_encode_item(56, bytes([max(0, min(255, int(metadata['platform_ground_speed'])))])))
    if 'platform_call_sign' in metadata:
        items.append(_encode_string_item(59, metadata['platform_call_sign']))

    # Item 65 - version of ST 0601 this metadata was generated against
    version = int(metadata.get('version', ST0601_VERSION))
    items.append(_encode_item(65, bytes([max(0, min(255, version))])))

    if 'operational_mode' in metadata:
        items.append(_encode_item(77, bytes([int(metadata['operational_mode']) & 0xFF])))

    # Item 94 - MIIS Core Identifier (ST 1204), required by ST 0902
    core_id = metadata.get('core_identifier')
    if core_id:
        if isinstance(core_id, str):
            core_id = bytes.fromhex(core_id)
        items.append(_encode_item(94, bytes(core_id)))

    payload = b''.join(items)

    # Item 1 (Checksum) is the last item and covers the whole packet including
    # the 16-byte key and its own key/length bytes (ST 0601.19 s6.8).
    checksum_header = encode_ber_oid(1) + encode_ber_length(2)
    length_field = encode_ber_length(len(payload) + len(checksum_header) + 2)
    prefix = UAS_LOCAL_SET_KEY + length_field + payload + checksum_header
    return prefix + struct.pack('>H', compute_checksum(prefix))


def main():
    """CLI entry point"""
    parser = argparse.ArgumentParser(description='Unified KLV Parser - STANAG 4609 processor')
    parser.add_argument('--mode', choices=['test', 'stream'], default='test',
                       help='Parser mode: test (validation) or stream (processing)')
    parser.add_argument('--stream', type=str,
                       help='Stream name for processing mode')
    parser.add_argument('--api-url', type=str, default='http://localhost:3000',
                       help='API base URL')

    args = parser.parse_args()

    # Create parser instance
    klv_parser = UnifiedKLVParser(
        stream_name=args.stream,
        api_url=args.api_url
    )

    # Run in specified mode
    if args.mode == 'test':
        klv_parser.run_test_mode()
    elif args.mode == 'stream':
        if not args.stream:
            print("Error: --stream required for stream mode")
            sys.exit(1)
        klv_parser.run_stream_mode()


if __name__ == '__main__':
    main()
