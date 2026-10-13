"""Recorder clock drift: measure it against the cameras and write a corrected copy of the audio.

A Zoom and a camera never run at exactly the same speed. Over a long ceremony the recorder slowly
slides out of sync (typically a frame or more per 20-30 minutes). We match many short windows of
the recording against the cameras, fit a straight line through the offsets, and if the recording
drifts we write a new WAV whose length matches the cameras' clock.

The fix keeps the sample rate and never resamples, so the pitch is untouched: it adds or drops a
sample or two once a second with a short crossfade, at the same spots in every track of a take.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from .media import Clip, popen, probe, tool
from .sync import DECODE_RATE, ENV_RATE, MIN_Z, RAW_MIN, Composite, Unit, _raw, match, next_fast_len

WINDOW = 20.0          # seconds per measured window
MIN_LENGTH = 90.0      # shorter recordings can't drift enough to matter
MAX_WINDOWS = 40
MAX_RATE = 0.002       # 2000 ppm: anything bigger is not clock drift
SPLICE_EVERY = 1.0     # seconds between length adjustments
XFADE = 512            # samples


@dataclass
class Drift:
    offset: float      # timeline time of file time 0
    rate: float        # extra seconds of real time per second of file (+ = recorder runs fast)
    points: int
    span: float        # longest stretch of the file measured against one camera clip
    resid_ms: float
    stderr: float = 0.0

    def drift_over(self, length: float) -> float:
        return self.rate * length


def measure(u: Unit, cams: list[Unit], search: float = 2.0) -> Optional[Drift]:
    """Match short windows of recorder take u against each long camera clip and fit the drift.

    Each camera clip keeps its own clock, so the slope is fitted inside each clip (a separate offset per
    clip). That works whether or not the clip itself synced: an unsynced clip is searched in full."""
    if u.env is None or u.length < MIN_LENGTH:
        return None
    w = int(WINDOW * ENV_RATE)
    n = len(u.env)
    count = int(min(MAX_WINDOWS, max(3, u.length // (WINDOW / 2))))
    starts = np.linspace(0, max(0, n - w), count).astype(int)
    pts: list[tuple[float, float, int]] = []          # (file time, offset, camera group)
    long_cams = [p for p in cams if p.env is not None and p.length >= 2 * WINDOW]
    u_placed = u.start is not None and u.status in ("ok", "warn")
    for i0 in starts:
        seg = u.env[i0:i0 + w]
        if seg.std() < 1e-3:
            continue
        t = i0 / ENV_RATE
        for g, p in enumerate(long_cams):
            p_placed = p.start is not None and p.status in ("ok", "warn")
            win = None
            if u_placed and p_placed:
                at = u.start + t
                if at < p.start - 1 or at + WINDOW > p.start + p.length + 1:
                    continue
                e = (at - p.start) * ENV_RATE
                win = (int(e - search * ENV_RATE), int(e + search * ENV_RATE))
            lag, z, cov = match(seg, p.env, win)
            if lag is None or cov < 0.95 or z < 2.0:
                continue
            base = p.start if p_placed else 0.0
            T = base + lag / ENV_RATE
            r = 0.0
            if u.src and p.src:
                q = Unit(clips=[], start=base, length=p.length, src=p.src)
                T, r = _refine(u.src, t, T, [q])
            # trust a window when the real waveforms agree, or the envelope match alone is strong
            if r >= RAW_MIN or z >= MIN_Z:
                pts.append((t, T - t, g))
    return fit(pts, u.length)


def fit(pts: list[tuple[float, float, int]], length: float) -> Optional[Drift]:
    """Common slope, one offset per camera group, outliers dropped."""
    groups: dict[int, list[tuple[float, float]]] = {}
    for t, d, g in pts:
        groups.setdefault(g, []).append((t, d))
    slopes = []
    for g in groups.values():
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                if g[j][0] - g[i][0] > WINDOW:
                    slopes.append((g[j][1] - g[i][1]) / (g[j][0] - g[i][0]))
    if len(slopes) < 2:
        return None
    b = float(np.median(slopes))
    keep_t, keep_d, keep_g = [], [], []
    for k, g in groups.items():
        ts = np.array([x[0] for x in g]); ds = np.array([x[1] for x in g])
        a = float(np.median(ds - b * ts))
        ok = np.abs(ds - (a + b * ts)) < 0.03
        if ok.sum() >= 2:
            keep_t.append(ts[ok]); keep_d.append(ds[ok]); keep_g.append(k)
    if not keep_t:
        return None
    # least squares with a separate intercept per group
    num = den = 0.0
    for ts, ds in zip(keep_t, keep_d):
        tc, dc = ts - ts.mean(), ds - ds.mean()
        num += float((tc * dc).sum()); den += float((tc * tc).sum())
    if den <= 0:
        return None
    b = num / den
    resid = np.concatenate([(ds - ds.mean()) - b * (ts - ts.mean()) for ts, ds in zip(keep_t, keep_d)])
    dof = max(1, resid.size - len(keep_t) - 1)
    stderr = float(np.sqrt((resid ** 2).sum() / dof / den))
    span = max(float(ts.max() - ts.min()) for ts in keep_t) + WINDOW
    # offset from the group with the most windows
    big = max(range(len(keep_t)), key=lambda i: len(keep_t[i]))
    a = float(np.mean(keep_d[big] - b * keep_t[big]))
    if abs(b) > MAX_RATE or span < 60:
        return None
    return Drift(a, float(b), int(resid.size), span, float(np.sqrt((resid ** 2).mean()) * 1000), stderr)


def _refine(src: str, t: float, T: float, placed: list[Unit], w: float = 8.0, search: float = 0.02) -> tuple[float, float]:
    """Sharpen a window's position with the real waveform of the camera that overlaps it most."""
    best, best_ov = None, 0.0
    for p in placed:
        if not p.src or p.start is None:
            continue
        ov = min(T + WINDOW, p.start + p.length) - max(T, p.start)
        if ov > best_ov:
            best, best_ov = p, ov
    if best is None or best_ov < w + 2 * search:
        return T, 0.0
    mid = max(T, best.start) + best_ov / 2
    x = _raw(src, t + (mid - T) - w / 2, w)
    y = _raw(best.src, mid - best.start - w / 2 - search, w + 2 * search)
    if x.size < DECODE_RATE or y.size < x.size:
        return T, 0.0
    nf = next_fast_len(len(x) + len(y))
    c = np.fft.irfft(np.fft.rfft(y, nf) * np.conj(np.fft.rfft(x, nf)), nf)[: len(y) - len(x) + 1]
    y2 = np.concatenate([[0.0], np.cumsum(y.astype(np.float64) ** 2)])
    ey = np.sqrt(np.maximum(y2[len(x):] - y2[: len(y2) - len(x)], 1e-9))[: c.size]
    nc = np.abs(c / (ey * np.sqrt((x.astype(np.float64) ** 2).sum()) + 1e-9))
    k = int(np.argmax(nc))
    if nc[k] < RAW_MIN:
        return T, float(nc[k])
    frac = 0.0
    if 0 < k < len(nc) - 1:
        a, b, cc = nc[k - 1], nc[k], nc[k + 1]
        den = a - 2 * b + cc
        if den != 0:
            frac = 0.5 * (a - cc) / den
    return T + ((k + frac) / DECODE_RATE - search), float(nc[k])


def _codec_for(path: str) -> str:
    try:
        info = probe(path)
        a = next(s for s in info["streams"] if s.get("codec_type") == "audio")
        name = a.get("codec_name", "")
        if name.startswith("pcm_") and name in ("pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le"):
            return name
    except (StopIteration, RuntimeError, KeyError):
        pass
    return "pcm_s24le"


def write_corrected(c: Clip, rate: float, out_path: str, new_tc: Optional[float],
                    progress: Callable[[float], None] = lambda f: None) -> float:
    """Write c's audio stretched by (1 + rate) without resampling. Returns the new duration in seconds."""
    sr, ch = c.sample_rate or 48000, max(1, c.channels)
    stretch = 1.0 + rate
    dec = popen([tool("ffmpeg"), "-v", "error", "-nostdin", "-i", c.path, "-vn", "-map", "0:a:0",
                 "-f", "f32le", "-ac", str(ch), "-ar", str(sr), "-"], stdout=-1, stderr=-1)
    tmp = out_path + ".part.wav"
    enc_cmd = [tool("ffmpeg"), "-v", "error", "-y", "-nostdin", "-f", "f32le", "-ar", str(sr), "-ac", str(ch), "-i", "-",
               "-c:a", _codec_for(c.path)]
    if new_tc is not None:
        enc_cmd += ["-write_bext", "1", "-metadata", f"time_reference={max(0, int(round(new_tc * sr)))}"]
    enc = popen(enc_cmd + ["-f", "wav", tmp], stdin=-1, stderr=-1)
    block = int(SPLICE_EVERY * sr)
    q = block // 2
    fade_out = np.linspace(1.0, 0.0, XFADE, dtype=np.float32)[:, None]
    fade_in = 1.0 - fade_out
    total_in = total_out = 0
    expected = max(1, int(c.duration * sr))
    buf = b""
    frame_bytes = 4 * ch
    try:
        while True:
            need = block * frame_bytes - len(buf)
            data = dec.stdout.read(need) if need > 0 else b""
            if data:
                buf += data
            if len(buf) < block * frame_bytes and data:
                continue
            if not buf:
                break
            usable = len(buf) // frame_bytes * frame_bytes
            x = np.frombuffer(buf[:usable], dtype=np.float32).reshape(-1, ch)
            buf = buf[usable:]
            total_in += len(x)
            target = int(round(total_in * stretch))
            d = target - (total_out + len(x))
            if d != 0 and len(x) >= q + XFADE + abs(d) + 1 and q - d >= 0:
                y = np.concatenate([x[:q], x[q:q + XFADE] * fade_out + x[q - d:q - d + XFADE] * fade_in, x[q - d + XFADE:]])
            else:
                y = x
            total_out += len(y)
            enc.stdin.write(np.ascontiguousarray(y, dtype=np.float32).tobytes())
            progress(min(1.0, total_in / expected))
            if not data:
                break
    finally:
        dec.stdout.close()
        dec.wait()
        enc.stdin.close()
        enc.wait()
    if enc.returncode != 0 or not os.path.exists(tmp):
        err = enc.stderr.read().decode(errors="ignore")[:300] if enc.stderr else ""
        raise RuntimeError(f"Couldn't write the corrected copy of {c.name}. {err}")
    os.replace(tmp, out_path)
    return total_out / sr


def output_dir(path: str, fallback: Optional[str]) -> str:
    """Corrected files go in 'Endora drift fixed' next to the originals, or the fallback if that's read-only."""
    d = os.path.join(os.path.dirname(path), "Endora drift fixed")
    try:
        os.makedirs(d, exist_ok=True)
        probe_file = os.path.join(d, ".write-test")
        with open(probe_file, "w") as f:
            f.write("x")
        os.remove(probe_file)
        return d
    except OSError:
        if not fallback:
            raise
        os.makedirs(fallback, exist_ok=True)
        return fallback


def fix_units(units: list[Unit], log: Callable[[str], None], progress: Callable[[float], None] = lambda f: None,
              fallback_dir: Optional[str] = None, min_frames: float = 0.5) -> int:
    """Measure drift for every recorder take (audio-only, long) and write corrected copies.
    The caller re-syncs afterwards, so takes that failed to sync because of drift get another chance."""
    cams = [u for u in units if u.env is not None and any(c.kind == "video" for c in u.clips)]
    recs = [u for u in units if u.env is not None and u.clips and all(c.kind == "audio" for c in u.clips)
            and u.length >= MIN_LENGTH and not any(getattr(c, "original_path", None) for c in u.clips)]
    if not cams or not recs:
        return 0
    fps = next((c.fps for u in cams for c in u.clips if c.fps), 25.0)
    fixed = 0
    for k, u in enumerate(recs):
        log(f"Checking {u.label} for clock drift")
        dr = measure(u, cams)
        if dr is None:
            continue
        total = dr.drift_over(u.length)
        if abs(total) * fps < min_frames or abs(dr.rate) < 3 * dr.stderr:
            continue
        log(f"{u.label}: drifts {total * 1000:+.0f} ms over {u.length / 60:.1f} min "
            f"({dr.rate * 1e6:+.0f} ppm). Writing a corrected copy.")
        new_paths: dict[str, tuple[str, float]] = {}
        for c in u.clips:
            if c.path in new_paths:
                continue
            d = output_dir(c.path, fallback_dir)
            stem, _ = os.path.splitext(os.path.basename(c.path))
            out = os.path.join(d, f"{stem}_driftfix.wav")
            new_paths[c.path] = (out, write_corrected(c, dr.rate, out, c.tc_start,
                                                      lambda f, k=k: progress((k + f) / len(recs))))
        for c in u.clips:
            c.original_path, c.original_duration = c.path, c.duration
            c.drift_ms = total * 1000
            c.path, c.duration = new_paths[c.path]
        u.length = max(c.duration for c in u.clips)
        fixed += 1
    return fixed
