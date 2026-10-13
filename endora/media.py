"""Media probing and the project model: layers (cameras / recorder inputs) holding clips."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

VIDEO_EXT = {".mp4", ".mov", ".mxf", ".mts", ".m2ts", ".avi", ".mkv", ".crm", ".braw", ".insv", ".lrv", ".3gp", ".m4v", ".wmv"}
AUDIO_EXT = {".wav", ".bwf", ".mp3", ".m4a", ".aac", ".flac", ".aif", ".aiff", ".ogg", ".wma"}

MAX_VIDEO_LAYERS = 20
MAX_AUDIO_LAYERS = 30

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _bin_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "bin")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def tool(name: str) -> str:
    exe = name + (".exe" if sys.platform == "win32" else "")
    local = os.path.join(_bin_dir(), exe)
    if os.path.exists(local):
        return local
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"{name} was not found. Reinstall Endora MultiSync or put {exe} on PATH.")
    return found


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW, **kw)


def popen(cmd: list[str], **kw) -> subprocess.Popen:
    return subprocess.Popen(cmd, creationflags=_NO_WINDOW, **kw)


def parse_rate(s: str | None) -> Optional[float]:
    if not s or s in ("0/0", "0"):
        return None
    try:
        return float(Fraction(s))
    except (ValueError, ZeroDivisionError):
        return None


def tc_to_seconds(tc: str, fps: float) -> Optional[float]:
    m = re.match(r"^(\d{1,2})[:;.](\d{2})[:;.](\d{2})[:;.,](\d{2,3})$", tc.strip())
    if not m:
        return None
    h, mi, s, f = (int(x) for x in m.groups())
    drop = ";" in tc or "," in tc
    nominal = round(fps) if fps else 25
    if drop and nominal in (30, 60):
        # SMPTE drop-frame: count real frames, then divide by the true rate
        d = 2 if nominal == 30 else 4
        total_min = 60 * h + mi
        frames = (nominal * 3600 * h + nominal * 60 * mi + nominal * s + f) - d * (total_min - total_min // 10)
        return frames / (fps or nominal * 1000 / 1001)
    return h * 3600 + mi * 60 + s + f / nominal


@dataclass
class Clip:
    path: str
    kind: str                     # "video" | "audio"
    duration: float = 0.0
    fps: Optional[float] = None
    width: int = 0
    height: int = 0
    sample_rate: int = 48000
    channels: int = 0             # audio channels in the file (0 = no audio)
    tc_start: Optional[float] = None  # seconds since midnight from embedded timecode
    tc_string: Optional[str] = None
    created: Optional[float] = None  # file creation time (epoch seconds) from metadata or disk
    take: Optional[str] = None    # recorder take id; clips of one take start together
    channel: int = 0              # which channel this layer uses (0-based)
    original_path: Optional[str] = None  # set when this clip uses a drift-corrected copy
    drift_ms: float = 0.0
    original_duration: float = 0.0
    # results
    start: Optional[float] = None
    method: str = ""
    status: str = "wait"          # wait | ok | warn | bad
    confidence: float = 0.0
    note: str = ""

    @property
    def name(self) -> str:
        return re.split(r"[\\/]", self.path)[-1]


@dataclass
class Layer:
    name: str
    kind: str                     # "video" | "audio"
    clips: list[Clip] = field(default_factory=list)
    source: str = ""              # "camera" | "recorder" | "camera-audio"


def probe(path: str) -> dict:
    r = run([tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path])
    if r.returncode != 0:
        raise RuntimeError(f"Can't read {os.path.basename(path)}: {r.stderr.decode(errors='ignore').strip()[:200]}")
    return json.loads(r.stdout.decode("utf-8", errors="ignore"))


def make_clip(path: str) -> Clip:
    info = probe(path)
    fmt = info.get("format", {})
    streams = info.get("streams", [])
    vs = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    aus = [s for s in streams if s.get("codec_type") == "audio"]
    ext = os.path.splitext(path)[1].lower()
    kind = "video" if vs is not None and ext not in AUDIO_EXT else "audio"
    c = Clip(path=path, kind=kind)
    c.duration = float(fmt.get("duration") or (vs or (aus[0] if aus else {})).get("duration") or 0)
    if vs is not None:
        c.fps = parse_rate(vs.get("r_frame_rate")) or parse_rate(vs.get("avg_frame_rate"))
        c.width, c.height = int(vs.get("width") or 0), int(vs.get("height") or 0)
    if aus:
        a = aus[0]
        c.sample_rate = int(a.get("sample_rate") or 48000)
        c.channels = int(a.get("channels") or 0) if len(aus) == 1 else len(aus)
    # timecode: video stream tag, any data stream (tmcd), format tag, then BWF time_reference
    tags = [(vs or {}).get("tags", {})] + [s.get("tags", {}) for s in streams] + [fmt.get("tags", {})]
    for t in tags:
        tc = t.get("timecode") or t.get("TIMECODE")
        if tc:
            sec = tc_to_seconds(tc, c.fps or 25.0)
            if sec is not None:
                c.tc_start, c.tc_string = sec, tc
                break
    ct = fmt.get("tags", {}).get("creation_time") or ((vs or {}).get("tags", {}) or {}).get("creation_time")
    if ct:
        try:
            from datetime import datetime
            c.created = datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
        except ValueError:
            c.created = None
    if c.created is None:
        try:
            c.created = os.path.getmtime(path) - c.duration   # most cameras write mtime at end of clip
        except OSError:
            pass
    if c.tc_start is None:
        tr = fmt.get("tags", {}).get("time_reference")
        if tr and str(tr).isdigit() and c.sample_rate:
            c.tc_start = int(tr) / c.sample_rate
            c.tc_string = "BWF"
    return c


ZOOM_TRACK = re.compile(r"^(?P<take>.+?)[_-](?P<trk>Tr[A-Za-z0-9]+|TrLR|LR|MS|Mix|[LR])$", re.I)


def recorder_key(path: str) -> tuple[str, str]:
    """Return (take, input) for a recorder file, e.g. ZOOM0003_Tr2.WAV -> ("ZOOM0003", "Tr2")."""
    stem = os.path.splitext(os.path.basename(path))[0]
    m = ZOOM_TRACK.match(stem)
    if m:
        return m.group("take"), m.group("trk").upper().replace("TR", "Tr")
    return stem, ""


def list_media(folder: str, exts: set[str]) -> list[str]:
    out = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if f.startswith("._"):
                continue
            if os.path.splitext(f)[1].lower() in exts:
                out.append(os.path.join(root, f))
    return sorted(out, key=lambda p: (os.path.dirname(p).lower(), os.path.basename(p).lower()))


def build_recorder_layers(paths: list[str], recorder_name: str, progress=None) -> list[Layer]:
    """Each recorder input becomes its own layer. Polyphonic files are split one layer per channel.
    Files of one take (same name prefix) share a take id so they sync together."""
    layers: dict[str, Layer] = {}
    for i, p in enumerate(paths):
        if progress:
            progress(f"Reading {os.path.basename(p)}")
        base = make_clip(p)
        if base.channels == 0:
            continue
        take, inp = recorder_key(p)
        base.take = f"{recorder_name}:{take}"
        if inp:
            chans = range(base.channels) if base.channels > 2 else [None]
            for ch in chans:
                key = inp if ch is None else f"{inp} ch{ch + 1}"
                c = Clip(**{**base.__dict__})
                c.channel = 0 if ch is None else ch
                layers.setdefault(key, Layer(f"{recorder_name} {key}", "audio", source="recorder")).clips.append(c)
                if ch is None and base.channels == 2:
                    c2 = Clip(**{**base.__dict__}); c2.channel = 1
                    layers.setdefault(f"{key} R", Layer(f"{recorder_name} {key} R", "audio", source="recorder")).clips.append(c2)
                    layers[key].name = f"{recorder_name} {key} L"
        else:
            # polyphonic file (one file holding every input): one layer per channel
            for ch in range(base.channels):
                c = Clip(**{**base.__dict__}); c.channel = ch
                key = f"In{ch + 1}"
                layers.setdefault(key, Layer(f"{recorder_name} Input {ch + 1}", "audio", source="recorder")).clips.append(c)

    def order(k: str):
        m = re.search(r"(\d+)", k)
        return (0 if m else 1, int(m.group(1)) if m else 0, k)
    return [layers[k] for k in sorted(layers, key=order)]
