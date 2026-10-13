"""Real waveforms for the timeline: peak level 50 times a second, decoded once and cached on disk."""
from __future__ import annotations

import hashlib
import os
import queue
import threading
from typing import Callable, Optional

import numpy as np

from .media import Clip, popen, tool

PEAK_RATE = 50
_DECODE = 4000


def _key(c: Clip) -> str:
    try:
        st = os.stat(c.path)
        sig = f"{c.path}|{st.st_size}|{int(st.st_mtime)}|{c.channel if c.channels > 2 else 0}|wf1"
    except OSError:
        sig = c.path
    return hashlib.sha1(sig.encode()).hexdigest()


def compute(c: Clip, cache_dir: Optional[str]) -> Optional[np.ndarray]:
    if not c.channels:
        return None
    cp = os.path.join(cache_dir, _key(c) + ".wf.npy") if cache_dir else None
    if cp and os.path.exists(cp):
        try:
            return np.load(cp)
        except (OSError, ValueError):
            pass
    pan = ["-af", f"pan=mono|c0=c{c.channel}"] if c.channels > 2 else ["-ac", "1"]
    cmd = [tool("ffmpeg"), "-v", "error", "-nostdin", "-i", c.path, "-vn", "-map", "0:a:0", *pan,
           "-ar", str(_DECODE), "-f", "s16le", "-"]
    p = popen(cmd, stdout=-1, stderr=-1)
    hop = _DECODE // PEAK_RATE
    out, rest = [], b""
    while True:
        buf = p.stdout.read(_DECODE * 2 * 30)
        if not buf:
            break
        buf = rest + buf
        n = len(buf) // (2 * hop) * 2 * hop
        rest = buf[n:]
        x = np.abs(np.frombuffer(buf[:n], dtype=np.int16).astype(np.float32)).reshape(-1, hop)
        out.append(x.max(axis=1) / 32768.0)
    p.wait()
    if not out:
        return None
    pk = np.concatenate(out).astype(np.float32)
    if cp:
        try:
            np.save(cp, pk)
        except OSError:
            pass
    return pk


class WaveCache:
    """Hands out peaks immediately if known, otherwise decodes them in the background and calls
    on_ready() (from a worker thread) when new ones arrive."""

    def __init__(self, cache_dir: Optional[str], on_ready: Callable[[], None], workers: int = 2):
        self.cache_dir = cache_dir
        self.on_ready = on_ready
        self.data: dict[str, Optional[np.ndarray]] = {}
        self.q: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        for _ in range(workers):
            threading.Thread(target=self._run, daemon=True).start()

    @staticmethod
    def ident(c: Clip) -> str:
        return f"{c.path}|{c.channel if c.channels > 2 else 0}"

    def get(self, c: Clip) -> Optional[np.ndarray]:
        k = self.ident(c)
        with self.lock:
            if k in self.data:
                return self.data[k]
            self.data[k] = None          # pending
        self.q.put(c)
        return None

    def _run(self):
        while True:
            c = self.q.get()
            try:
                pk = compute(c, self.cache_dir)
            except Exception:  # noqa: BLE001 - a bad file just has no waveform
                pk = None
            if pk is not None:
                with self.lock:
                    self.data[self.ident(c)] = pk
                self.on_ready()


def columns(pk: np.ndarray, duration: float, x0: float, x1: float, pps: float, clip_x: float):
    """Peak per pixel column for the part of a clip (drawn at clip_x) that is visible in [x0, x1).
    Returns (first pixel x, peaks)."""
    xs = max(x0, clip_x)
    xe = min(x1, clip_x + duration * pps)
    n = int(xe - xs)
    if n <= 0 or not len(pk):
        return xs, np.zeros(0, np.float32)
    a, b = (xs - clip_x) / pps, (xe - clip_x) / pps
    edges = np.linspace(a * PEAK_RATE, b * PEAK_RATE, n + 1).astype(int)
    edges = np.clip(edges, 0, len(pk) - 1)
    # max over each column's range; equal neighbours (zoomed in) just repeat the sample
    return xs, np.maximum.reduceat(pk[: edges[-1] + 1], edges[:-1])
