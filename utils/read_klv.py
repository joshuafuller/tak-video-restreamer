#!/usr/bin/env python3
"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Read and display KLV metadata from video files or binary KLV files
Decodes STANAG 4609 packets and shows in human-readable format
"""

import sys
import os
import struct
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Add shared directory to path for KLV import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))
try:
    from klv import UnifiedKLVParser
    KLV_AVAILABLE = True
except ImportError:
    KLV_AVAILABLE = False
    print("Error: KLV module not available")
    sys.exit(1)


def is_video_file(file_path):
    """Check if file is a video file"""
    video_extensions = ['.mp4', '.mov', '.ts', '.mkv', '.avi', '.mxf', '.mpeg', '.mpg']
    return Path(file_path).suffix.lower() in video_extensions


def extract_klv_from_video(video_file):
    """Extract KLV data stream from video file to temporary file"""
    print(f"Extracting KLV from video file...")
    
    # Create temporary file for extracted KLV
    temp_klv = tempfile.NamedTemporaryFile(delete=False, suffix='.bin')
    temp_klv.close()
    
    cmd = [
        'ffmpeg',
        '-i', video_file,
        '-map', '0:d',  # Map data stream
        '-c', 'copy',
        '-f', 'data',
        '-y',
        temp_klv.name
    ]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode == 0 and os.path.exists(temp_klv.name) and os.path.getsize(temp_klv.name) > 0:
            print(f"✓ Extracted {os.path.getsize(temp_klv.name):,} bytes of KLV data\n")
            return temp_klv.name
        else:
            print("✗ No KLV data stream found in video")
            os.unlink(temp_klv.name)
            return None
            
    except subprocess.TimeoutExpired:
        print("✗ Extraction timeout")
        os.unlink(temp_klv.name)
        return None
    except Exception as e:
        print(f"✗ Error extracting KLV: {e}")
        if os.path.exists(temp_klv.name):
            os.unlink(temp_klv.name)
        return None


def _checksum_text(valid):
    """Human-readable form of a packet's ST 0601 item 1 verification result"""
    if valid is None:
        return 'not present (ST 0601 requires item 1)'
    return 'valid' if valid else 'INVALID - packet should be discarded'


def _format_item(item):
    """Format one decoded ST 0601 item for the console"""
    name = item.get('name', f"Tag {item.get('tag_id')}")
    value = item.get('value')
    units = item.get('units', '')

    if item.get('out_of_range'):
        return f"{name:34s}: out of range ({item.get('raw_value')})"
    if not item.get('decoded'):
        return f"{name:34s}: {value} (raw, not decoded)"
    if isinstance(value, dict):  # enumerated item
        return f"{name:34s}: {value.get('name')} ({value.get('code')})"

    if item.get('tag_id') in (2, 72) and isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
        return f"{name:34s}: {dt.isoformat()} ({value:.6f})"
    if isinstance(value, float):
        text = f"{name:34s}: {value:.6f}" if units == 'degrees' else f"{name:34s}: {value:.2f}"
        if units:
            text += '°' if units == 'degrees' else f' {units}'
        if 'absolute_value' in item:
            text += f" (absolute: {item['absolute_value']:.6f}°)"
        return text
    return f"{name:34s}: {value}{' ' + units if units else ''}"


def read_klv_file(klv_file_path, is_temp=False):
    """Read and parse KLV packets from binary file"""
    print(f"\n{'='*70}")
    print(f"KLV Metadata Reader".center(70))
    print(f"{'='*70}\n")
    
    if not is_temp:
        print(f"File: {os.path.basename(klv_file_path)}")
    
    if not os.path.exists(klv_file_path):
        print(f"Error: File not found: {klv_file_path}")
        return None
    
    file_size = os.path.getsize(klv_file_path)
    print(f"Size: {file_size:,} bytes\n")
    
    parser = UnifiedKLVParser()
    packets = []
    
    try:
        with open(klv_file_path, 'rb') as f:
            klv_data = f.read()
        
        # STANAG 4609 universal key
        stanag_key = bytes([0x06, 0x0E, 0x2B, 0x34, 0x02, 0x0B, 0x01, 0x01,
                           0x0E, 0x01, 0x03, 0x01, 0x01, 0x00, 0x00, 0x00])
        
        offset = 0
        packet_num = 0
        
        while offset < len(klv_data):
            # Look for STANAG key
            if klv_data[offset:offset+16] != stanag_key:
                # Try to find next key
                next_key = klv_data.find(stanag_key, offset + 1)
                if next_key == -1:
                    break
                offset = next_key
                continue
            
            try:
                # Parse BER length
                if offset + 16 >= len(klv_data):
                    break
                    
                length_byte = klv_data[offset + 16]
                if length_byte < 128:
                    length_size = 1
                    data_len = length_byte
                else:
                    length_bytes = length_byte & 0x7F
                    length_size = 1 + length_bytes
                    data_len = 0
                    for i in range(length_bytes):
                        if offset + 17 + i >= len(klv_data):
                            break
                        data_len = (data_len << 8) | klv_data[offset + 17 + i]
                
                packet_size = 16 + length_size + data_len
                
                if offset + packet_size > len(klv_data):
                    break
                
                # Parse packet
                packet_data = klv_data[offset:offset + packet_size]
                parsed = parser.parse_klv_packet(packet_data)
                packets.append(parsed)
                packet_num += 1
                
                offset += packet_size
                
            except Exception as e:
                offset += 1
                continue
    
    except Exception as e:
        print(f"Error reading file: {e}")
        return None
    
    print(f"Total Packets: {len(packets)}")

    if not packets:
        print("No packets found in file")
        return

    valid = sum(1 for p in packets if p.get('checksum_valid') is True)
    absent = sum(1 for p in packets if p.get('checksum_valid') is None)
    print(f"Checksums:     {valid} valid, {len(packets) - valid - absent} invalid, {absent} absent\n")
    
    # Display summary
    print(f"{'='*70}")
    print("Packet Summary")
    print(f"{'='*70}\n")
    
    # Show first packet in detail
    print("First Packet (detailed):")
    print("-" * 70)
    first = packets[0]
    print(f"Timestamp: {first.get('timestamp', 'N/A')}")
    print(f"Raw Size: {first.get('raw_size', 0)} bytes")
    print(f"Items Found: {len(first.get('items', first.get('tags', {})))}")
    print(f"Checksum: {_checksum_text(first.get('checksum_valid'))}\n")

    # Items are printed in packet order; ST 0601 permits some items to repeat
    # and their order carries meaning, so do not sort them.
    for item in first.get('items', []):
        print(f"  {_format_item(item)}")
    
    # Show timing information
    if len(packets) > 1:
        print(f"\n{'-'*70}")
        print("Last Packet (summary):")
        print("-" * 70)
        last = packets[-1]
        print(f"Timestamp: {last.get('timestamp', 'N/A')}")
        print(f"Raw Size: {last.get('raw_size', 0)} bytes")
        
        # Calculate timing
        if 'tags' in first and 'tags' in last:
            first_time = first['tags'].get('Precision Time Stamp', {}).get('value')
            last_time = last['tags'].get('Precision Time Stamp', {}).get('value')

            if first_time and last_time:
                duration = last_time - first_time
                rate = (len(packets) - 1) / duration if duration > 0 else 0
                print(f"\nTiming Information:")
                print(f"  Duration: {duration:.2f} seconds")
                print(f"  Update Rate: {rate:.2f} Hz")
                print(f"  Avg Interval: {1000/rate:.1f} ms" if rate > 0 else "  Avg Interval: N/A")
    
    print(f"\n{'='*70}\n")
    
    # Option to export as JSON
    return packets


def export_json(packets, output_file):
    """Export packets to JSON file"""
    with open(output_file, 'w') as f:
        json.dump(packets, f, indent=2, default=str)
    print(f"Exported {len(packets)} packets to: {output_file}")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Read and display KLV metadata from video files or binary KLV files'
    )
    parser.add_argument(
        'input_file',
        help='Video file (.mp4, .ts, .mov, etc.) or KLV binary file (.bin)'
    )
    parser.add_argument(
        '--json',
        help='Export to JSON file'
    )
    parser.add_argument(
        '--packet',
        type=int,
        help='Show detailed info for specific packet number'
    )
    
    args = parser.parse_args()
    
    if not os.path.exists(args.input_file):
        print(f"Error: File not found: {args.input_file}")
        return 1
    
    # Check if input is a video file
    temp_klv_file = None
    if is_video_file(args.input_file):
        # Extract KLV from video
        temp_klv_file = extract_klv_from_video(args.input_file)
        if not temp_klv_file:
            print("\nNo KLV metadata found in video file")
            return 1
        klv_file = temp_klv_file
        is_temp = True
    else:
        # Direct KLV binary file
        klv_file = args.input_file
        is_temp = False
    
    try:
        # Read and parse the file
        packets = read_klv_file(klv_file, is_temp=is_temp)
        
        if not packets:
            print("No packets found or failed to parse")
            return 1
        
        if args.json:
            export_json(packets, args.json)
        
        if args.packet is not None:
            if 0 <= args.packet < len(packets):
                print(f"\n{'='*70}")
                print(f"Packet {args.packet} (detailed):")
                print(f"{'='*70}\n")
                print(json.dumps(packets[args.packet], indent=2, default=str))
            else:
                print(f"\nError: Packet {args.packet} out of range (0-{len(packets)-1})")
        
        return 0
        
    finally:
        # Clean up temporary file if created
        if temp_klv_file and os.path.exists(temp_klv_file):
            os.unlink(temp_klv_file)


if __name__ == "__main__":
    main()
