#!/usr/bin/env python3
"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Preflight validator for KLV video sources.

Answers the questions:
  - Is there a KLV track at all, or was it dropped on the way in?
  - Do the KLV timestamps run backwards? (breaks once the stream passes through RTSP)
  - Is the video constant frame rate, and will TAK clients decode it?

Usage:
    python utils/validate_stream.py path/to/file.ts
    python utils/validate_stream.py rtsp://localhost:8554/stream1
    python utils/validate_stream.py file.ts --json
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Repo root on path so we can reuse the app's probe helper and the KLV parser.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / 'shared'))

try:
    from app.utils.codec_detection import detect_stream_codec
except ImportError:  # pragma: no cover - only when run outside the repo
    detect_stream_codec = None

try:
    from klv import UnifiedKLVParser
    KLV_PARSER_AVAILABLE = True
except ImportError:
    KLV_PARSER_AVAILABLE = False


# STANAG 4609 / MISB ST 0601 UAS Datalink Local Set universal key
STANAG_UL = bytes([0x06, 0x0E, 0x2B, 0x34, 0x02, 0x0B, 0x01, 0x01,
                   0x0E, 0x01, 0x03, 0x01, 0x01, 0x00, 0x00, 0x00])

PASS, WARN, FAIL, SKIP = 'pass', 'warn', 'fail', 'skip'

# TAK's PGSCMedia player is stricter than ffmpeg/VLC; these are the profiles it
# reliably decodes. High profile and B-frames have been observed to hard-fail.
TAK_SAFE_PROFILES = {'baseline', 'constrained baseline', 'main'}


def _is_url(target):
    return '://' in target


def _run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _ffprobe_json(target, extra_args, timeout, packet_window=None):
    """Run ffprobe and return parsed JSON, or None on failure."""
    cmd = ['ffprobe', '-v', 'error', '-print_format', 'json']
    if _is_url(target) and target.lower().startswith(('rtsp://', 'rtsps://')):
        cmd += ['-rtsp_transport', 'tcp']
    cmd += extra_args
    if packet_window:
        cmd += ['-read_intervals', f'%+{packet_window}']
    cmd.append(target)

    try:
        result = _run(cmd, timeout)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _check(check_id, name, status, summary, hint=None, detail=None):
    return {
        'id': check_id,
        'name': name,
        'status': status,
        'summary': summary,
        'hint': hint,
        'detail': detail or {},
    }


def _monotonic_stats(values):
    """Count backward steps in a timestamp sequence."""
    backward = sum(1 for i in range(1, len(values)) if values[i] < values[i - 1])
    return backward, max(0, len(values) - 1)


# --------------------------------------------------------------------------
# Individual checks
# --------------------------------------------------------------------------

def check_klv_track(streams):
    """Is there a data track carrying KLV?"""
    data_streams = [s for s in streams if s.get('codec_type') == 'data']

    if not data_streams:
        return _check(
            'klv_track', 'KLV track present', FAIL,
            'No data track — the stream carries no KLV at all.',
            'If you published over RTSP, that is the cause: ffmpeg\'s RTP muxer has no '
            'SMPTE336M payloader and silently drops data streams. Publish over SRT instead, '
            'and pass -map 0 (default stream selection never picks data tracks).',
        )

    klv = [s for s in data_streams
           if s.get('codec_name') == 'klv' or s.get('codec_tag_string') == 'KLVA']
    if not klv:
        names = ', '.join(s.get('codec_name') or 'unknown' for s in data_streams)
        return _check(
            'klv_track', 'KLV track present', WARN,
            f'Data track present but not tagged as KLV (codec: {names}).',
            'Expect codec_name=klv / codec_tag=KLVA. A track carried over RTSP may report '
            '"unknown" because ffmpeg does not map SMPTE336M back to its KLV codec ID — '
            'that is cosmetic. Check the source if this is a file.',
            {'data_codecs': names},
        )

    return _check(
        'klv_track', 'KLV track present', PASS,
        f'KLV data track found (stream index {klv[0].get("index")}).',
        detail={'index': klv[0].get('index'), 'codec_tag': klv[0].get('codec_tag_string')},
    )


def check_klv_timestamps(target, streams, window, timeout):
    """Do the KLV timestamps run backwards? This is the defect that breaks at the RTP hop."""
    klv = [s for s in streams
           if s.get('codec_type') == 'data'
           and (s.get('codec_name') == 'klv' or s.get('codec_tag_string') == 'KLVA')]
    if not klv:
        return _check('klv_timestamps', 'KLV timestamps monotonic', SKIP,
                      'No KLV track to check.')

    data = _ffprobe_json(
        target,
        ['-select_streams', f'd:{0}', '-show_entries', 'packet=pts,dts'],
        timeout, packet_window=window,
    )
    if not data or not data.get('packets'):
        return _check('klv_timestamps', 'KLV timestamps monotonic', SKIP,
                      'Could not read KLV packets to check timing.')

    packets = data['packets']
    pts = [int(p['pts']) for p in packets if p.get('pts') not in (None, 'N/A')]
    if len(pts) < 2:
        return _check('klv_timestamps', 'KLV timestamps monotonic', SKIP,
                      f'Only {len(pts)} KLV packet(s) sampled — not enough to judge.')

    backward, total = _monotonic_stats(pts)
    pct = (100.0 * backward / total) if total else 0.0
    detail = {'packets_sampled': len(pts), 'backward_steps': backward,
              'backward_pct': round(pct, 1)}

    if backward:
        return _check(
            'klv_timestamps', 'KLV timestamps monotonic', FAIL,
            f'KLV PTS runs backwards on {backward}/{total} steps ({pct:.1f}%).',
            'The KLV track was stamped with the video\'s B-frame timestamps. MPEG-TS tolerates '
            'this, but RTP carries only one timestamp, so clients downstream of RTSP drop the '
            'packets. Repair with: -c:d copy -bsf:d "setts=pts=DTS:dts=DTS" '
            '(or enable repair_klv_timestamps in server settings for pull streams).',
            detail,
        )

    return _check('klv_timestamps', 'KLV timestamps monotonic', PASS,
                  f'{len(pts)} KLV packets sampled, all strictly increasing.', detail=detail)


def check_frame_rate(target, streams, window, timeout):
    """Constant frame rate? VFR is a known WinTAK failure trigger."""
    video = [s for s in streams if s.get('codec_type') == 'video']
    if not video:
        return _check('frame_rate', 'Constant frame rate', SKIP, 'No video track.')

    data = _ffprobe_json(
        target,
        ['-select_streams', 'v:0', '-show_entries', 'packet=dts'],
        timeout, packet_window=window,
    )
    if not data or not data.get('packets'):
        return _check('frame_rate', 'Constant frame rate', SKIP,
                      'Could not read video packets to check timing.')

    dts = [int(p['dts']) for p in data['packets'] if p.get('dts') not in (None, 'N/A')]
    if len(dts) < 3:
        return _check('frame_rate', 'Constant frame rate', SKIP,
                      'Not enough video packets sampled.')

    deltas = [dts[i] - dts[i - 1] for i in range(1, len(dts))]
    distinct = sorted(set(deltas))
    detail = {'frames_sampled': len(dts), 'distinct_intervals': len(distinct)}

    # Tolerate 1-tick rounding jitter (90 kHz clocks rarely divide evenly).
    spread = max(distinct) - min(distinct) if distinct else 0
    if spread <= 2:
        return _check('frame_rate', 'Constant frame rate', PASS,
                      f'Constant frame interval across {len(dts)} frames.', detail=detail)

    detail['min_interval_ms'] = round(min(distinct) / 90.0, 2)
    detail['max_interval_ms'] = round(max(distinct) / 90.0, 2)
    return _check(
        'frame_rate', 'Constant frame rate', FAIL,
        f'Variable frame rate — intervals range {detail["min_interval_ms"]}–'
        f'{detail["max_interval_ms"]} ms across {len(dts)} frames.',
        'WinTAK (PGSCMedia) fails on VFR with an opaque "Media Fatal Error". '
        'Re-encode constant: -r 30 -fps_mode:v cfr',
        detail,
    )


def check_tak_safe_video(streams):
    """H.264 profile and B-frames — both observed to hard-fail WinTAK."""
    video = [s for s in streams if s.get('codec_type') == 'video']
    if not video:
        return _check('tak_video', 'TAK-safe video profile', SKIP, 'No video track.')

    v = video[0]
    profile = (v.get('profile') or '').strip()
    b_frames = v.get('has_b_frames')
    problems = []

    if profile and profile.lower() not in TAK_SAFE_PROFILES:
        problems.append(f'{profile} profile')
    if b_frames:
        problems.append(f'B-frames (has_b_frames={b_frames})')

    detail = {'profile': profile or None, 'has_b_frames': b_frames,
              'codec': v.get('codec_name')}

    if problems:
        return _check(
            'tak_video', 'TAK-safe video profile', WARN,
            'Video uses ' + ' and '.join(problems) + '.',
            'TAK clients decode Baseline/Main without B-frames most reliably. '
            'Re-encode with: -profile:v main -bf 0',
            detail,
        )

    return _check('tak_video', 'TAK-safe video profile', PASS,
                  f'{v.get("codec_name")} {profile or "unknown profile"}, no B-frames.',
                  detail=detail)


def check_klv_pes_stream_id(target):
    """
    MISB ST 1402 specifies stream_id 0xBD (private_stream_1) for synchronous KLV.
    FFmpeg always writes 0xFC (metadata_stream) and cannot be told otherwise, so this
    is advisory — it only matters for strict MISB consumers. File-only: needs raw TS bytes.
    """
    if _is_url(target):
        return _check('klv_pes_id', 'KLV PES stream_id (MISB ST 1402)', SKIP,
                      'Raw TS inspection only works on local files.')
    if Path(target).suffix.lower() not in ('.ts', '.mpg', '.mpeg', '.m2ts', '.mts'):
        return _check('klv_pes_id', 'KLV PES stream_id (MISB ST 1402)', SKIP,
                      'Not an MPEG-TS container.')

    pmt_pids = set()
    klv_pid = None
    stream_ids = set()
    sampled = 0

    try:
        with open(target, 'rb') as f:
            # PAT -> PMT PIDs -> KLV PID -> sample PES headers on that PID.
            for _ in range(400000):
                pkt = f.read(188)
                if len(pkt) < 188 or pkt[0] != 0x47:
                    break
                pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
                pusi = pkt[1] & 0x40
                afc = (pkt[3] >> 4) & 0x3
                idx = 4
                if afc in (2, 3):
                    idx += 1 + pkt[4]
                if afc == 2 or idx >= 188:
                    continue
                payload = pkt[idx:]

                if pid == 0 and pusi and len(payload) > 1:
                    sec = payload[1 + payload[0]:]
                    if len(sec) > 11 and sec[0] == 0x00:
                        seclen = ((sec[1] & 0x0F) << 8) | sec[2]
                        body = sec[8:min(3 + seclen - 4, len(sec))]
                        for i in range(0, len(body) - 3, 4):
                            prog = (body[i] << 8) | body[i + 1]
                            if prog:
                                pmt_pids.add(((body[i + 2] & 0x1F) << 8) | body[i + 3])

                elif klv_pid is None and pid in pmt_pids and pusi and len(payload) > 1:
                    sec = payload[1 + payload[0]:]
                    if len(sec) > 12 and sec[0] == 0x02:
                        seclen = ((sec[1] & 0x0F) << 8) | sec[2]
                        i = 12 + (((sec[10] & 0x0F) << 8) | sec[11])
                        end = min(3 + seclen - 4, len(sec))
                        while i + 4 < end:
                            stype = sec[i]
                            epid = ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
                            esil = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
                            if stype in (0x06, 0x15) and b'KLVA' in sec[i + 5:i + 5 + esil]:
                                klv_pid = epid
                            i += 5 + esil

                elif klv_pid is not None and pid == klv_pid and pusi:
                    if payload[:3] == b'\x00\x00\x01':
                        stream_ids.add(payload[3])
                        sampled += 1
                        if sampled >= 32:
                            break
    except OSError as e:
        return _check('klv_pes_id', 'KLV PES stream_id (MISB ST 1402)', SKIP,
                      f'Could not read file: {e}')

    if not stream_ids:
        return _check('klv_pes_id', 'KLV PES stream_id (MISB ST 1402)', SKIP,
                      'No KLV PES packets found in the sampled region.')

    ids = ', '.join(f'0x{s:02X}' for s in sorted(stream_ids))
    if stream_ids == {0xBD}:
        return _check('klv_pes_id', 'KLV PES stream_id (MISB ST 1402)', PASS,
                      f'stream_id {ids} (private_stream_1), as ST 1402 specifies.',
                      detail={'stream_ids': ids})

    return _check(
        'klv_pes_id', 'KLV PES stream_id (MISB ST 1402)', WARN,
        f'KLV PES uses stream_id {ids}; ST 1402 specifies 0xBD for synchronous KLV.',
        'Advisory only. FFmpeg always writes 0xFC (metadata_stream) and offers no way to '
        'change it, so most ffmpeg-produced streams look like this and work fine. Worth '
        'chasing only if a strict MISB consumer rejects an otherwise-valid stream.',
        {'stream_ids': ids},
    )


def check_st0601(target, streams, window, timeout):
    """Do the KLV packets actually decode as MISB ST 0601?"""
    if not KLV_PARSER_AVAILABLE:
        return _check('st0601', 'ST 0601 decodes', SKIP, 'shared/klv.py not importable.')

    klv = [s for s in streams
           if s.get('codec_type') == 'data'
           and (s.get('codec_name') == 'klv' or s.get('codec_tag_string') == 'KLVA')]
    if not klv:
        return _check('st0601', 'ST 0601 decodes', SKIP, 'No KLV track.')

    cmd = ['ffmpeg', '-v', 'error']
    if target.lower().startswith(('rtsp://', 'rtsps://')):
        cmd += ['-rtsp_transport', 'tcp']
    cmd += ['-t', str(window), '-i', target, '-map', 'd:0', '-c', 'copy', '-f', 'data', '-']

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _check('st0601', 'ST 0601 decodes', SKIP, 'Timed out reading KLV payload.')

    blob = result.stdout or b''
    count = blob.count(STANAG_UL)
    if not count:
        return _check(
            'st0601', 'ST 0601 decodes', FAIL,
            'KLV track carries no recognisable ST 0601 UAS Local Set packets.',
            'The universal key 06.0E.2B.34...01.01 was not found. The source encoder may be '
            'emitting a different local set, or the payload is corrupt.',
        )

    parser = UnifiedKLVParser()
    offset = blob.find(STANAG_UL)
    try:
        parsed = parser.parse_klv_packet(blob[offset:])
        tags = len(parsed.get('tags', {}))
    except Exception as e:
        return _check('st0601', 'ST 0601 decodes', WARN,
                      f'Found {count} KLV packets but the first failed to parse: {e}')

    # ST 0601.19 makes item 1 (Checksum) mandatory, so sample a few packets and
    # see whether they carry one and whether it verifies.
    checked = good = bad = missing = 0
    pos = offset
    while pos != -1 and checked < 50:
        try:
            sample = parser.parse_klv_packet(blob[pos:])
        except Exception:
            break
        valid = sample.get('checksum_valid')
        if valid is None:
            missing += 1
        elif valid:
            good += 1
        else:
            bad += 1
        checked += 1
        pos = blob.find(STANAG_UL, pos + 1)

    rate = count / window if window else 0
    summary = f'{count} ST 0601 packets in {window}s (~{rate:.1f} Hz), {tags} tags in first packet.'
    detail = {
        'packets': count, 'rate_hz': round(rate, 2), 'tags_first_packet': tags,
        'checksums_checked': checked, 'checksums_ok': good,
        'checksums_bad': bad, 'checksums_missing': missing,
    }

    if bad:
        return _check(
            'st0601', 'ST 0601 decodes', WARN,
            summary + f' {bad}/{checked} sampled packets fail their ST 0601 checksum.',
            'A packet whose checksum does not match must be discarded by a conforming reader '
            '(ST 0601.19 s6.1), so downstream clients may drop this metadata.', detail,
        )
    if missing and not good:
        return _check(
            'st0601', 'ST 0601 decodes', WARN,
            summary + ' No packet carries item 1 (Checksum).',
            'ST 0601.19 requires the checksum item on every UAS Local Set packet. Strict '
            'readers may reject metadata without it.', detail,
        )

    return _check('st0601', 'ST 0601 decodes', PASS,
                  summary + f' {good}/{checked} sampled checksums verify.', detail=detail)


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def validate(target, window=10, timeout=60):
    """Run all checks against a file path or stream URL."""
    probe = _ffprobe_json(target, ['-show_streams'], timeout)
    if not probe:
        return {
            'target': target,
            'ok': False,
            'error': 'ffprobe could not read the target (not found, unreachable, or not media).',
            'checks': [],
        }

    streams = probe.get('streams', [])
    checks = [
        check_klv_track(streams),
        check_klv_timestamps(target, streams, window, timeout),
        check_frame_rate(target, streams, window, timeout),
        check_tak_safe_video(streams),
        check_klv_pes_stream_id(target),
        check_st0601(target, streams, window, timeout),
    ]

    return {
        'target': target,
        'ok': not any(c['status'] == FAIL for c in checks),
        'has_warnings': any(c['status'] == WARN for c in checks),
        'checks': checks,
        'streams': [
            {'index': s.get('index'), 'type': s.get('codec_type'),
             'codec': s.get('codec_name'), 'tag': s.get('codec_tag_string')}
            for s in streams
        ],
    }


_GLYPH = {PASS: '[ OK ]', WARN: '[WARN]', FAIL: '[FAIL]', SKIP: '[ -- ]'}


def print_report(report):
    print()
    print('=' * 74)
    print(f'  Stream validation: {report["target"]}')
    print('=' * 74)

    if report.get('error'):
        print(f'\n  {report["error"]}\n')
        return

    print('\n  Tracks:')
    for s in report['streams']:
        tag = f' [{s["tag"]}]' if s.get('tag') and s['tag'].strip('[]0') else ''
        print(f'    {s["index"]}: {s["type"]:<6} {s["codec"]}{tag}')

    print()
    for c in report['checks']:
        print(f'  {_GLYPH[c["status"]]} {c["name"]}')
        print(f'         {c["summary"]}')
        if c['status'] in (FAIL, WARN) and c.get('hint'):
            for line in _wrap(c['hint'], 64):
                print(f'         → {line}')
        print()

    print('-' * 74)
    if report['ok'] and not report['has_warnings']:
        print('  RESULT: all checks passed.')
    elif report['ok']:
        print('  RESULT: usable, with warnings above.')
    else:
        print('  RESULT: problems found — see FAIL entries above.')
    print('=' * 74)
    print()


def _wrap(text, width):
    words, line, out = text.split(), '', []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = f'{line} {w}'.strip()
    if line:
        out.append(line)
    return out


def main():
    parser = argparse.ArgumentParser(
        description='Validate a video file or live stream for KLV and TAK-client compatibility.'
    )
    parser.add_argument('target', help='Video file path or stream URL (rtsp://, srt://)')
    parser.add_argument('--json', action='store_true', help='Emit JSON instead of a report')
    parser.add_argument('--window', type=int, default=10,
                        help='Seconds of stream to sample (default: 10)')
    parser.add_argument('--timeout', type=int, default=60,
                        help='Per-probe timeout in seconds (default: 60)')
    args = parser.parse_args()

    if not _is_url(args.target) and not os.path.exists(args.target):
        print(f'Error: file not found: {args.target}', file=sys.stderr)
        return 2

    report = validate(args.target, window=args.window, timeout=args.timeout)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)

    if report.get('error'):
        return 2
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
