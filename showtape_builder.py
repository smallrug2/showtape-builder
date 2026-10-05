"""showtape_builder.py - Build and rechannel Spark showtapes (.rshw + .wav).

Purpose: two subcommands in one file.
    build      synth dialogue lines (Windows SAPI TTS; silent fallback
               elsewhere), lay a 120 BPM beat under song lines, derive
               mouth/bounce choreography frames, and write a hand-rolled
               NRBF .rshw file plus the mixed .wav.
    rechannel  remap movement-bit channels in an existing .rshw via a
               chart file (OLD=NEW per line), for RR-Engine variants.

Usage:
    python showtape_builder.py build --lines show.tsv --name tape1 --outdir ./tapes
    python showtape_builder.py build --demo --name demo --outdir ./tapes
    python showtape_builder.py rechannel --tape ./tapes/tape1.rshw --chart chart.txt

    Lines file format (tab or '|' separated): CHAR KIND TEXT
      KIND is S (skit) or G (song). Example:
        BILLY\\tS\\tHowdy, folks!
        MITZI\\tG\\tMake a wish and blow it out!

Platform: Windows + Linux (stdlib only). TTS synth needs Windows
    PowerShell + SAPI voices; on other OSes (or TTS failure) each line
    becomes silence estimated from text length so builds still work.
"""
import argparse
import hashlib
import math
import os
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

FPS = 60
SR = 22050
MOUTH = {"BILLY": 0, "LOONEY": 1, "FATZ": 2, "DOOK": 3, "MITZI": 4}
MIRROR = 150
BOUNCE = list(range(8, 16))

DEMO_LINES = [
    ("BILLY", "S", "Howdy, folks! Welcome to the Pizza Time Playhouse!"),
    ("MITZI", "S", "I choreographed a whole cheer routine!"),
    ("BILLY", "G", "We run on greasepaint and gasoline!"),
    ("MITZI", "G", "Make a wish and blow it out!"),
]


# ---- audio helpers ----

def synth_line(char, text, outpath, cache_dir, ps_script):
    """TTS via Windows SAPI; fall back to estimated silence elsewhere."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5((char + text).encode()).hexdigest()
    cached = cache_dir / (key + ".wav")
    if cached.is_file():
        shutil.copyfile(cached, outpath)
        return
    if os.name == "nt" and ps_script and Path(ps_script).is_file():
        try:
            subprocess.run(["powershell", "-ExecutionPolicy", "Bypass",
                            "-File", str(ps_script), "-Text", text,
                            "-Out", str(outpath)], check=True, timeout=120)
            shutil.copyfile(outpath, cached)
            return
        except Exception as e:
            print("warning: TTS failed (%s); using silence" % e)
    # Fallback: silence proportional to text length (~12 chars/sec, min 1s).
    dur = max(1.0, len(text) / 12.0)
    write_wav(outpath, [0] * int(dur * SR))


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, \
            "need mono 16-bit WAV: %s" % path
        n = w.getnframes()
        return list(struct.unpack("<%dh" % n, w.readframes(n)))


def write_wav(path, samples):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(struct.pack("<%dh" % len(samples),
                                  *[max(-32768, min(32767, int(s)))
                                    for s in samples]))


def make_beat(seconds):
    """120 BPM groove: kick, hats, bass. Returns sample list."""
    n = int(seconds * SR)
    out = [0.0] * n
    bps = 2.0
    for i in range(n):
        t = i / SR
        beat = (t * bps) % 4.0
        s = 0.0
        for k in (0.0, 2.0):  # kick on 0 and 2
            dt = (beat - k) % 4.0
            if dt < 0.12:
                s += math.sin(2 * math.pi * 55 * dt) * math.exp(-dt * 40) * 0.9
        dt = (t * bps * 2) % 1.0  # hats every half beat
        if dt < 0.03:
            s += math.sin(2 * math.pi * 6000 * dt) * math.exp(-dt * 200) * 0.25
        bar = int(t * bps / 4) % 4
        root = [55.0, 55.0, 65.4, 82.4][bar]
        s += math.sin(2 * math.pi * root * t) * 0.22
        s += math.sin(2 * math.pi * root * 2 * t) * 0.08
        out[i] = s * 0.5
    return [int(x * 12000) for x in out]


def build_show(name, lines, outdir, cache_dir, ps_script):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    tmp = outdir / "_lines"
    tmp.mkdir(exist_ok=True)
    spans, track = [], []
    t = 1.0  # 1s leader
    for idx, (char, kind, text) in enumerate(lines):
        char = char.upper()
        if char not in MOUTH:
            print("warning: unknown char %r, using BILLY" % char)
            char = "BILLY"
        wav = tmp / ("line%02d.wav" % idx)
        synth_line(char, text, str(wav), Path(cache_dir), ps_script)
        data = read_wav(wav)
        dur = len(data) / SR
        gap = 0.35 if kind == "S" else 0.22
        track += [0] * (int(t * SR) - len(track))
        track += data
        spans.append((char, kind, t, dur))
        t += dur + gap
    song_spans = [sp for sp in spans if sp[1] == "G"]
    if song_spans:
        first, last = song_spans[0][2], song_spans[-1][2] + song_spans[-1][3]
        beat = make_beat(last - first + 1.0)
        for i, b in enumerate(beat):
            j = int(first * SR) + i
            if j < len(track):
                track[j] = max(-32768, min(32767, track[j] + b))
        total = last + 1.0
    else:
        total = t + 0.5
    track += [0] * (int(total * SR) - len(track))
    audio_path = outdir / (name + ".wav")
    write_wav(str(audio_path), track)
    print("%s audio: %.1fs" % (name, total))
    # Choreography -> signalData ints (bit+1 per set bit, 0 = frame end).
    nframes = int(total * FPS)
    frames = [set() for _ in range(nframes)]

    def frange(s, d):
        return range(max(0, int(s * FPS)), min(nframes, int((s + d) * FPS)))

    for (char, kind, s, d) in spans:
        mb = MOUTH[char]
        for f in frange(s, d):
            if (f // 3) % 2 == 0:  # 20 Hz mouth flap
                frames[f].add(mb)
                frames[f].add(mb + MIRROR)
    for (char, kind, s, d) in spans:
        if kind != "G":
            continue
        f0, f1 = int(s * FPS), int((s + d) * FPS)
        beat0, b = (f0 // 30) * 30, 0
        while beat0 + b * 30 < f1:
            bit = BOUNCE[(beat0 // 30 + b) % len(BOUNCE)]
            for f in range(max(f0, beat0 + b * 30),
                           min(f1, beat0 + b * 30 + 6)):
                frames[f].add(bit)
                frames[f].add(bit + MIRROR)
            b += 1
    signal = [0]
    for fr in frames:
        for bit in sorted(fr):
            signal.append(bit + 1)
        signal.append(0)
    print("%s frames: %d  signal ints: %d" % (name, nframes, len(signal)))
    return audio_path, signal


# ---- NRBF writer (.NET BinaryFormatter, hand-rolled) ----

def _lps(s):
    b = s.encode("utf-8")
    n, out = len(b), bytearray()
    while True:
        c = n & 0x7F
        n >>= 7
        if n:
            out.append(c | 0x80)
        else:
            out.append(c)
            break
    out += b
    return bytes(out)


def _i32(v):
    return struct.pack("<i", v)


def _u32(v):
    return struct.pack("<I", v)


def write_rshw(path, audio_bytes, signal_ints):
    lib = "Assembly-CSharp, Version=0.0.0.0, Culture=neutral, PublicKeyToken=null"
    out = bytearray()
    out += b"\x00" + _i32(1) + _u32(0xFFFFFFFF) + _i32(1) + _i32(0)
    out += b"\x0c" + _i32(2) + _lps(lib)
    out += b"\x05" + _i32(1) + _lps("rshwFormat") + _i32(3)
    out += _lps("<audioData>k__BackingField")
    out += _lps("<signalData>k__BackingField")
    out += _lps("<videoData>k__BackingField")
    out += bytes([7, 7, 7]) + bytes([2, 8, 2]) + _i32(2)
    out += b"\x09" + _i32(3) + b"\x09" + _i32(4) + b"\x0a"
    out += b"\x0f" + _i32(3) + _i32(len(audio_bytes)) + bytes([2])
    out += audio_bytes
    out += b"\x0f" + _i32(4) + _i32(len(signal_ints)) + bytes([8])
    for v in signal_ints:
        out += _i32(v)
    out += b"\x0b"
    with open(path, "wb") as fh:
        fh.write(bytes(out))
    print("wrote %s (%d bytes)" % (path, len(out)))


# ---- rechannel ----

def extract_signal(rshw_path):
    data = Path(rshw_path).read_bytes()
    marker = b"\x0f\x04\x00\x00\x00"
    i = data.find(marker)
    if i == -1:
        raise ValueError("signal array not found in %s" % rshw_path)
    pos = i + len(marker)
    n = struct.unpack("<i", data[pos:pos + 4])[0]
    pos += 4
    if data[pos] != 8:
        raise ValueError("not an int32 array")
    pos += 1
    return list(struct.unpack("<%di" % n, data[pos:pos + 4 * n]))


def load_chart(chart_path):
    chart = {}
    with open(chart_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                a, b = line.split("=", 1)
                chart[int(a.strip())] = int(b.strip())
    return chart


def load_lines(path):
    """Parse a lines file: CHAR<tab/|>KIND<tab/|>TEXT per line."""
    lines = []
    with open(path, encoding="utf-8") as fh:
        for n, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            sep = "\t" if "\t" in raw else ("|" if "|" in raw else None)
            if sep is None:
                raise ValueError("line %d: need CHAR<TAB>KIND<TAB>TEXT" % n)
            parts = raw.split(sep, 2)
            if len(parts) != 3 or parts[1].strip().upper() not in ("S", "G"):
                raise ValueError("line %d: bad KIND (want S or G)" % n)
            lines.append((parts[0].strip().upper(), parts[1].strip().upper(),
                          parts[2].strip()))
    if not lines:
        raise ValueError("no lines in %s" % path)
    return lines


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Build/rechannel showtapes")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="Build .wav + .rshw from lines")
    b.add_argument("--lines", default="",
                   help="Lines file (CHAR KIND TEXT per line)")
    b.add_argument("--demo", action="store_true",
                   help="Use built-in demo lines")
    b.add_argument("--name", default="tape1", help="Tape name")
    b.add_argument("--outdir", default=str(Path.cwd() / "tapes"),
                   help="Output directory")
    b.add_argument("--cache-dir", default=str(Path.home() / ".showtape_vox"),
                   help="TTS wav cache directory")
    b.add_argument("--tts-script", default="",
                   help="Optional tts_line.ps1 path (Windows SAPI)")
    r = sub.add_parser("rechannel", help="Remap bits in a .rshw file")
    r.add_argument("--tape", required=True, help="Input .rshw path")
    r.add_argument("--chart", required=True,
                   help="Chart file with OLD=NEW per line")
    r.add_argument("--out", default="",
                   help="Output .rshw (default: <tape>_remapped.rshw)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.cmd == "build":
        if args.demo or not args.lines:
            lines = DEMO_LINES
            if not args.demo and not args.lines:
                print("no --lines given; using built-in demo lines")
        else:
            lines = load_lines(args.lines)
        audio_path, signal = build_show(args.name, lines, args.outdir,
                                        args.cache_dir,
                                        args.tts_script or None)
        wav = Path(audio_path).read_bytes()  # full .wav; game parses RIFF
        write_rshw(str(Path(args.outdir) / (args.name + ".rshw")),
                   wav, signal)
    elif args.cmd == "rechannel":
        chart = load_chart(args.chart)
        sig = extract_signal(args.tape)
        out = [0 if v == 0 else chart.get(v - 1, v - 1) + 1 for v in sig]
        wav_path = Path(args.tape).with_suffix(".wav")
        if not wav_path.is_file():  # also try sibling without suffix change
            wav_path = Path(str(args.tape).replace(".rshw", ".wav"))
        if not wav_path.is_file():
            print("ERROR: companion .wav not found for %s" % args.tape,
                  file=sys.stderr)
            sys.exit(2)
        dst = args.out or str(args.tape).replace(".rshw", "_remapped.rshw")
        write_rshw(dst, wav_path.read_bytes(), out)
        print("remapped %d ints, %d channels changed" % (len(out), len(chart)))


if __name__ == "__main__":
    main()
