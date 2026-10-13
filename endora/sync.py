"""Sync engine: audio waveform matching, timecode placement, and timecode + audio verification.

Every clip with sound is turned into an onset envelope (200 per second). Clips are matched against a
growing composite of everything already placed, so cameras that never overlap the main recorder can
still sync through another camera that does.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from .media import Clip, Layer, popen, tool

ENV_RATE = 200            # envelope samples per second (5 ms)
DECODE_RATE = 8000
MIN_Z = 10.0              # match strength needed to trust an audio match
CACHE_DIR = None          # set by the app to a cache folder


@dataclass
class Unit:
    """Things that move together: one camera clip, or every file of one recorder take."""
    clips: list[Clip]
    env: Optional[np.ndarray] = None
    tc: Optional[float] = None
    start: Optional[float] = None
    length: float = 0.0
    label: str = ""
    method: str = ""
    status: str = "wait"
    confidence: float = 0.0
    note: str = ""
    src: Optional[str] = None


def decode_envelope(clip: Clip) -> Optional[np.ndarray]:
    if clip.channels == 0:
        return None
    if CACHE_DIR:
        st = os.stat(clip.path)
        key = hashlib.sha1(f"{clip.path}|{st.st_size}|{int(st.st_mtime)}|v1".encode()).hexdigest() + ".npy"
        cp = os.path.join(CACHE_DIR, key)
        if os.path.exists(cp):
            return np.load(cp)
    cmd = [tool("ffmpeg"), "-v", "error", "-nostdin", "-i", clip.path, "-vn", "-map", "0:a:0",
           "-ac", "1", "-ar", str(DECODE_RATE), "-f", "s16le", "-"]
    proc = popen(cmd, stdout=-1, stderr=-1)
    hop = DECODE_RATE // ENV_RATE
    chunks, rest = [], b""
    while True:
        buf = proc.stdout.read(DECODE_RATE * 2 * 30)
        if not buf:
            break
        buf = rest + buf
        n = (len(buf) // (2 * hop)) * 2 * hop
        rest = buf[n:]
        x = np.frombuffer(buf[:n], dtype=np.int16).astype(np.float32).reshape(-1, hop)
        chunks.append(np.log1p((x * x).mean(axis=1)))
    proc.wait()
    if not chunks:
        return None
    e = np.concatenate(chunks)
    on = np.maximum(np.diff(e, prepend=e[0]), 0.0)
    # remove slow loudness changes so mics with different gain still match
    k = np.ones(ENV_RATE // 2, dtype=np.float32) / (ENV_RATE // 2)
    on = on - np.convolve(on, k, mode="same")
    on = np.clip(on, 0, None).astype(np.float32)
    sd = on.std()
    if sd < 1e-6:
        return None
    on = on / sd
    if CACHE_DIR:
        try:
            np.save(os.path.join(CACHE_DIR, key), on)
        except OSError:
            pass
    return on


def next_fast_len(n: int) -> int:
    """Smallest 2^a * 3^b * 5^c >= n (fast FFT sizes)."""
    best = 1 << (n - 1).bit_length()
    p5 = 1
    while p5 < best:
        p35 = p5
        while p35 < best:
            m = p35
            while m < n:
                m *= 2
            best = min(best, m)
            p35 *= 3
        p5 *= 5
    return best


def match(clip_env: np.ndarray, ref: np.ndarray, lag_window: Optional[tuple[int, int]] = None, candidates: Optional[list] = None):
    """Find lag L (in envelope samples) where clip_env best lines up with ref[L:]. Returns (lag, z, coverage).
    If a list is passed as `candidates`, the best few distinct lags are appended to it."""
    if len(clip_env) < ENV_RATE * 2 or len(ref) < ENV_RATE * 2:
        return None, 0.0, 0.0
    n = len(clip_env) + len(ref) - 1
    nfft = next_fast_len(n)
    corr = np.fft.irfft(np.fft.rfft(ref, nfft) * np.conj(np.fft.rfft(clip_env, nfft)), nfft)
    # lags 0..len(ref)-1 are at corr[0..], negative lags wrap to the end
    neg = corr[nfft - (len(clip_env) - 1):] if len(clip_env) > 1 else corr[:0]
    corr = np.concatenate([neg, corr[:len(ref)]])
    lags = np.arange(-(len(clip_env) - 1), len(ref))
    # normalise by overlap so partial overlaps aren't punished or rewarded
    ov = np.minimum(lags + len(clip_env), len(ref)) - np.maximum(lags, 0)
    ov = np.maximum(ov, 1)
    min_ov = min(len(clip_env), len(ref), ENV_RATE * 8) * 0.5
    score = np.where(ov >= min_ov, corr / np.sqrt(ov), 0.0)
    if lag_window is not None:
        lo, hi = lag_window
        mask = (lags < lo) | (lags > hi)
        score = np.where(mask, 0.0, score)
    valid = score[score != 0]
    if valid.size < 10:
        return None, 0.0, 0.0
    i = int(np.argmax(score))
    peak = score[i]
    # strongest rival at least half a second away
    excl = score.copy()
    excl[max(0, i - ENV_RATE // 2): i + ENV_RATE // 2] = 0
    rival = float(np.max(excl)) if excl.size else 0.0
    # Pearson correlation over the overlapping part at the best lag
    L = int(lags[i])
    a0, b0 = max(0, -L), max(0, L)
    n_ov = int(ov[i])
    x, y = clip_env[a0:a0 + n_ov], ref[b0:b0 + n_ov]
    x, y = x - x.mean(), y - y.mean()
    ncc = float((x * y).sum() / (np.sqrt((x * x).sum() * (y * y).sum()) + 1e-9))
    ratio = peak / rival if rival > 0 else 10.0
    z = strength(ncc, ratio)
    if candidates is not None:
        sc = score.copy()
        for _ in range(6):
            j = int(np.argmax(sc))
            if sc[j] <= 0:
                break
            candidates.append(float(lags[j]))
            sc[max(0, j - ENV_RATE // 2): j + ENV_RATE // 2] = 0
    if os.environ.get("ENDORA_DEBUG"):
        print(f"  ncc={ncc:.3f} ratio={ratio:.2f} z={z:.1f}")
    # sub-sample refinement
    lag = float(lags[i])
    if 0 < i < len(score) - 1:
        a, b, c = score[i - 1], score[i], score[i + 1]
        den = a - 2 * b + c
        if den != 0:
            lag += 0.5 * (a - c) / den
    return lag, float(z), float(ov[i] / len(clip_env))


def strength(ncc: float, ratio: float) -> float:
    """Single match score: 10 or more is trusted. Needs a clear correlation AND no look-alike rival."""
    return 10.0 * min(ncc / 0.2, (ratio - 1.0) / 0.4)


def _raw(path: str, start: float, dur: float) -> np.ndarray:
    cmd = [tool("ffmpeg"), "-v", "error", "-nostdin", "-ss", f"{max(0.0, start):.4f}", "-t", f"{dur:.4f}", "-i", path,
           "-vn", "-map", "0:a:0", "-ac", "1", "-ar", str(DECODE_RATE), "-f", "s16le", "-"]
    out = popen(cmd, stdout=-1, stderr=-1).communicate()[0]
    x = np.frombuffer(out, dtype=np.int16).astype(np.float32)
    return np.diff(x) if x.size > 1 else x   # pre-emphasis: drop rumble so mics compare fairly


def raw_check(u: "Unit", start: float, placed: list["Unit"], window: float = 20.0, search: float = 0.06):
    """Compare real waveforms at a candidate position against the placed recording that overlaps most.
    Returns (normalised correlation, refined start)."""
    src = getattr(u, "src", None)
    best_p, best_ov = None, 0.0
    for p in placed:
        if getattr(p, "src", None) is None or p.start is None:
            continue
        ov = min(start + u.length, p.start + p.length) - max(start, p.start)
        if ov > best_ov:
            best_p, best_ov = p, ov
    if src is None or best_p is None or best_ov < 2.0:
        return 0.0, start
    a = max(start, best_p.start)
    mid = a + best_ov / 2
    w = min(window, best_ov - 2 * search)
    t0 = mid - w / 2
    x = _raw(src, t0 - start, w)
    y = _raw(best_p.src, t0 - best_p.start - search, w + 2 * search)
    if x.size < DECODE_RATE or y.size < x.size:
        return 0.0, start
    n = next_fast_len(len(x) + len(y))
    c = np.fft.irfft(np.fft.rfft(y, n) * np.conj(np.fft.rfft(x, n)), n)[: len(y) - len(x) + 1]
    # normalise by the energy of each window
    y2 = np.concatenate([[0.0], np.cumsum(y.astype(np.float64) ** 2)])
    ey = np.sqrt(np.maximum(y2[len(x):] - y2[: len(y2) - len(x)], 1e-9))[: c.size]
    nc = c / (ey * np.sqrt((x.astype(np.float64) ** 2).sum()) + 1e-9)
    k = int(np.argmax(np.abs(nc)))
    shift = k / DECODE_RATE - search
    return float(abs(nc[k])), start + shift


RAW_MIN = 0.08   # waveform correlation that proves two recordings heard the same sound


def conf_from_z(z: float) -> float:
    return float(np.clip(0.75 + (z - MIN_Z) / 40.0, 0.75, 0.995)) if z >= MIN_Z else float(np.clip(z / MIN_Z * 0.6, 0, 0.6))


class Composite:
    def __init__(self):
        self.origin = 0.0     # seconds at index 0
        self.sig = np.zeros(0, dtype=np.float32)
        self.count = np.zeros(0, dtype=np.float32)

    def add(self, env: np.ndarray, start: float, sign: float = 1.0):
        i0 = int(round((start - self.origin) * ENV_RATE))
        if self.sig.size == 0:
            self.origin = start
            self.sig = np.zeros(len(env), np.float32)
            self.count = np.zeros(len(env), np.float32)
            i0 = 0
        if i0 < 0:
            pad = -i0
            self.sig = np.concatenate([np.zeros(pad, np.float32), self.sig])
            self.count = np.concatenate([np.zeros(pad, np.float32), self.count])
            self.origin -= pad / ENV_RATE
            i0 = 0
        end = i0 + len(env)
        if end > len(self.sig):
            self.sig = np.concatenate([self.sig, np.zeros(end - len(self.sig), np.float32)])
            self.count = np.concatenate([self.count, np.zeros(end - len(self.count), np.float32)])
        self.sig[i0:end] += sign * env
        self.count[i0:end] += sign

    def ref(self) -> np.ndarray:
        return self.sig / np.sqrt(np.maximum(self.count, 1.0))

    def empty(self) -> bool:
        return self.sig.size == 0 or not np.any(self.count > 0)


def build_units(layers: list[Layer], include_camera_audio: bool = True) -> list[Unit]:
    units: list[Unit] = []
    takes: dict[str, Unit] = {}
    for layer in layers:
        for c in layer.clips:
            if layer.source == "camera-audio":
                continue  # moves with its video clip
            if c.take:
                u = takes.get(c.take)
                if u is None:
                    u = takes[c.take] = Unit(clips=[], label=c.take.split(":", 1)[-1])
                    units.append(u)
                u.clips.append(c)
            else:
                units.append(Unit(clips=[c], label=c.name))
    for u in units:
        u.length = max(c.duration for c in u.clips)
        tcs = [c.tc_start for c in u.clips if c.tc_start is not None]
        u.tc = tcs[0] if tcs else None
    return units


def unit_audio_source(u: Unit) -> Optional[Clip]:
    """Pick the file in a unit whose sound is best for matching (a recorder's LR mix if there is one)."""
    with_audio = [c for c in u.clips if c.channels > 0]
    if not with_audio:
        return None
    for c in with_audio:
        if "LR" in os.path.basename(c.path).upper().split("_")[-1]:
            return c
    return max(with_audio, key=lambda c: c.duration)


def run_sync(layers: list[Layer], mode: str, log: Callable[[str], None] = print,
             progress: Callable[[float], None] = lambda f: None, verify_window: float = 3.0,
             fix_drift: bool = True, drift_dir: Optional[str] = None) -> list[Unit]:
    """mode: 'audio' | 'tc' | 'tcv'. Writes start/status/method/confidence onto every clip.
    With fix_drift, long recorder files that run at a slightly different speed than the cameras get a
    corrected copy (same pitch), and everything is synced again with it."""
    for l in layers:                      # start from the original recordings every time
        for c in l.clips:
            if c.original_path:
                c.path, c.duration = c.original_path, c.original_duration
                c.original_path, c.drift_ms = None, 0.0
    scale = 0.85 if fix_drift and mode != "tc" else 1.0
    units = _run_once(layers, mode, log, lambda f: progress(f * scale), verify_window)
    if scale == 1.0:
        return units
    from . import drift
    fixed = drift.fix_units(units, log, lambda f: progress(0.85 + 0.1 * f), drift_dir)
    if fixed:
        log(f"Re-syncing with {fixed} drift-corrected recording(s)")
        for l in layers:
            for c in l.clips:
                c.start, c.status, c.method, c.confidence, c.note = None, "wait", "", 0.0, ""
        units = _run_once(layers, mode, log, lambda f: progress(0.95 + 0.05 * f), verify_window)
        for l in layers:
            for c in l.clips:
                if c.original_path and c.status in ("ok", "warn"):
                    c.note = (f"Clock drift {c.drift_ms:+.0f} ms fixed. {c.note}. "
                              f"Using corrected copy (same pitch) in 'Endora drift fixed'.")
                    if c.method:
                        c.method = c.method + " + drift fix"
    progress(1.0)
    return units


def _run_once(layers: list[Layer], mode: str, log, progress, verify_window: float) -> list[Unit]:
    units = build_units(layers)
    if not units:
        raise RuntimeError("Add some cameras or audio first.")

    need_audio = mode in ("audio", "tcv")
    if need_audio:
        seen: dict[str, np.ndarray] = {}
        for i, u in enumerate(units):
            src = unit_audio_source(u)
            u.src = src.path if src is not None else None
            if src is not None:
                log(f"Listening to {src.name}")
                if src.path not in seen:
                    env = decode_envelope(src)
                    seen[src.path] = env
                u.env = seen[src.path]
            progress(0.6 * (i + 1) / len(units))

    if mode == "tc":
        for u in units:
            if u.tc is not None:
                u.start, u.method, u.status, u.confidence = u.tc, "Timecode", "ok", 1.0
                u.note = "Placed by timecode"
            else:
                u.method, u.status, u.note = "Timecode", "bad", "No timecode in file. Use audio or TC + audio verify."
        progress(1.0)

    elif mode == "audio":
        _audio_place(units, log, progress)

    else:  # tcv
        comp = Composite()
        tc_units = [u for u in units if u.tc is not None]
        for u in tc_units:
            u.start, u.method = u.tc, "TC"
            if u.env is not None:
                comp.add(u.env, u.tc)
        for i, u in enumerate(tc_units):
            if u.env is None:
                u.status, u.confidence, u.method = "ok", 0.9, "TC only"
                u.note = "Timecode trusted (no audio to check)"
                continue
            comp.add(u.env, u.tc, -1.0)           # compare against everyone else
            if comp.empty():
                u.status, u.confidence, u.note = "ok", 0.9, "Timecode (nothing else to compare against)"
            else:
                ref = comp.ref()
                expect = (u.tc - comp.origin) * ENV_RATE
                w = verify_window * ENV_RATE
                lag, z, _ = match(u.env, ref, (int(expect - w), int(expect + w)))
                if lag is None or z < MIN_Z:
                    u.status, u.confidence = "warn", 0.6
                    u.note = "Timecode used. Audio too quiet or different to double-check."
                else:
                    audio_start = comp.origin + lag / ENV_RATE
                    delta = audio_start - u.tc
                    fps = next((c.fps for c in u.clips if c.fps), 25.0)
                    df = delta * fps
                    u.confidence = conf_from_z(z)
                    if abs(df) < 0.5:
                        u.status, u.method = "ok", "TC + audio"
                        u.note = "Timecode and audio agree (0 frames)"
                    else:
                        u.start, u.status, u.method = audio_start, "warn", "TC -> audio"
                        u.note = f"Timecode was off by {df:+.0f} frames. Corrected by audio."
            comp.add(u.env, u.tc, 1.0)
            progress(0.6 + 0.25 * (i + 1) / max(1, len(tc_units)))
        # rebuild composite with corrected positions, then place clips that had no timecode by audio
        rest = [u for u in units if u.tc is None]
        if rest:
            log(f"{len(rest)} clip(s) have no timecode. Placing them by audio.")
            _audio_place(units, log, progress, preplaced=[u for u in units if u.start is not None])
            for u in rest:
                if u.status in ("ok", "warn") and u.start is not None:
                    u.method = "Audio (no TC)"
                    u.note = "No timecode, placed by audio waveform"
        progress(1.0)

    # copy results to clips; recorder takes keep their relative offsets
    for u in units:
        for c in u.clips:
            c.status, c.method, c.confidence, c.note = u.status, u.method, u.confidence, u.note
            if u.start is None:
                c.start = None
            elif u.tc is not None and c.tc_start is not None and len(u.clips) > 1:
                c.start = u.start + (c.tc_start - u.tc)
            else:
                c.start = u.start
    place_unsynced(layers)
    _sync_camera_audio_layers(layers)
    return units


def _audio_place(units: list[Unit], log, progress, preplaced: Optional[list[Unit]] = None):
    comp = Composite()
    placed: list[Unit] = []
    if preplaced:
        for u in preplaced:
            if u.env is not None:
                comp.add(u.env, u.start)
            placed.append(u)
    else:
        cands = [u for u in units if u.env is not None]
        if not cands:
            for u in units:
                u.status, u.note, u.method = "bad", "No audio to sync with", "Audio"
            return
        anchor = max(cands, key=lambda u: u.length)
        anchor.start = anchor.tc if anchor.tc is not None else 0.0
        anchor.status, anchor.method, anchor.confidence = "ok", "Audio (reference)", 1.0
        anchor.note = "Reference: longest recording"
        comp.add(anchor.env, anchor.start)
        placed.append(anchor)
        log(f"Reference recording: {anchor.label}")
    todo = [u for u in units if u not in placed]
    total = max(1, len(todo))
    done = 0
    # repeat so clips that only overlap other cameras can chain in
    changed = True
    best: dict[int, float] = {}
    while todo and changed:
        changed = False
        for u in list(todo):
            if u.env is None:
                continue
            ref = comp.ref()
            cands: list = []
            lag, z, cov = match(u.env, ref, candidates=cands)
            best[id(u)] = max(best.get(id(u), 0.0), z)
            start = None
            if lag is not None and z >= MIN_Z:
                start, conf = comp.origin + lag / ENV_RATE, conf_from_z(z)
                r, refined = raw_check(u, start, placed)
                if r >= RAW_MIN:
                    start = refined
            elif cands:
                # weak or look-alike match: let the real waveform decide between the top candidates
                checks = [raw_check(u, comp.origin + c / ENV_RATE, placed) for c in cands]
                order = sorted(range(len(checks)), key=lambda i: -checks[i][0])
                r1 = checks[order[0]][0]
                r2 = checks[order[1]][0] if len(order) > 1 else 0.0
                if os.environ.get("ENDORA_DEBUG"):
                    print("  raw", [round(c[0], 3) for c in checks])
                if r1 >= RAW_MIN and r1 >= 2.5 * r2:
                    start = checks[order[0]][1]
                    conf = float(np.clip(0.7 + r1, 0.75, 0.97))
            if start is not None:
                u.start = start
                u.method, u.status, u.confidence = "Audio waveform", "ok", conf
                u.note = f"Matched audio ({u.confidence * 100:.0f}%)"
                comp.add(u.env, u.start)
                todo.remove(u)
                placed.append(u)
                changed = True
                done += 1
                log(f"Synced {u.label}")
                progress(0.6 + 0.4 * done / total)
    for u in todo:
        u.method = "Audio"
        u.status = "bad"
        u.confidence = conf_from_z(best.get(id(u), 0.0))
        u.note = "No audio in this clip" if u.env is None else "No match found. Check this clip by hand."


def place_unsynced(layers: list[Layer], gap: float = 1.0):
    """Keep clips that couldn't be synced on the timeline, in file order on their own layer.
    If the clip's recorded time is known, use it (shifted like the synced clips of the same layer);
    otherwise put it right after the clip before it, or at the end of the layer if there's no room."""
    all_starts = [c.start for l in layers for c in l.clips if c.start is not None]
    origin = min(all_starts) if all_starts else 0.0
    for layer in layers:
        if layer.source == "camera-audio":
            continue
        clips = sorted(layer.clips, key=lambda c: (os.path.dirname(c.path).lower(), os.path.basename(c.path).lower()))
        if all(c.start is not None for c in clips):
            continue
        # how this camera's clock relates to the timeline, from clips that did sync
        diffs = sorted(c.start - c.created for c in clips if c.start is not None and c.created is not None)
        clock = diffs[len(diffs) // 2] if diffs else None

        def free(s, d):
            return all(o.start is None or o is c or s + d <= o.start or s >= o.start + o.duration for o in clips)

        prev_end = origin
        pending = []
        for c in clips:
            if c.start is not None:
                prev_end = max(prev_end, c.start + c.duration)
                continue
            guess = c.created + clock if (clock is not None and c.created is not None) else None
            if guess is not None and guess >= prev_end - 0.001 and free(guess, c.duration):
                c.start = guess
                c.note = "Couldn't sync. Placed by the camera's clock, check by hand."
            elif free(prev_end + (gap if prev_end > origin else 0), c.duration):
                c.start = prev_end + (gap if prev_end > origin else 0)
                c.note = "Couldn't sync. Placed in file order, check by hand."
            else:
                pending.append(c)
                continue
            c.method, c.status = "Not synced", "bad"
            prev_end = c.start + c.duration
        for c in pending:   # no room in its slot: add at the end of the layer, still in order
            end = max([o.start + o.duration for o in clips if o.start is not None] + [origin])
            c.start = end + gap
            c.method, c.status = "Not synced", "bad"
            c.note = "Couldn't sync. Added at the end of its layer, check by hand."
        # recorder takes: keep every input of the take together
        for c in clips:
            if c.take and c.method == "Not synced":
                for l2 in layers:
                    for o in l2.clips:
                        if o.take == c.take and o.start is None:
                            o.start, o.method, o.status, o.note = c.start, c.method, c.status, c.note


def _sync_camera_audio_layers(layers: list[Layer]):
    """Camera-audio layers mirror their video clip's position."""
    by_path = {}
    for layer in layers:
        if layer.kind == "video":
            for c in layer.clips:
                by_path[c.path] = c
    for layer in layers:
        if layer.source == "camera-audio":
            for c in layer.clips:
                v = by_path.get(c.path)
                if v:
                    c.start, c.status, c.method, c.confidence, c.note = v.start, v.status, v.method, v.confidence, v.note
