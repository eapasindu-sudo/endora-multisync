"""Timeline playback: picture from one camera, sound mixed from the Zoom and the camera, kept in sync.

Everything is decoded with the bundled ffmpeg, so any file the sync engine can read also plays here.
Sound goes out through the Windows waveOut API (no extra libraries); its play position is the master
clock and the picture follows it, dropping frames if the computer can't keep up."""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from .media import Clip, Layer, popen, tool

RATE = 48000
CH = 2
PREVIEW_W, PREVIEW_H = 640, 360
WINDOW = 1800.0          # seconds of sound prepared per start; playback restarts itself after that
MAX_INPUTS = 48


# ---------------------------------------------------------------- audio output
class _NullOut:
    """No sound device: keep time with the wall clock."""

    def __init__(self):
        self.t0 = None
        self.written = 0

    def write(self, data: bytes, stop: threading.Event):
        if self.t0 is None:
            self.t0 = time.monotonic()
        self.written += len(data) // (2 * CH)
        ahead = self.written / RATE - (time.monotonic() - self.t0)
        while ahead > 0.3 and not stop.is_set():
            time.sleep(0.02)
            ahead = self.written / RATE - (time.monotonic() - self.t0)

    def position(self) -> float:
        return 0.0 if self.t0 is None else min(self.written / RATE, time.monotonic() - self.t0)

    def close(self):
        pass


class _WaveOut:
    """Windows waveOut with a ring of buffers."""
    NBUF, FRAMES = 8, 4096

    def __init__(self):
        from ctypes import wintypes as wt

        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [("wFormatTag", wt.WORD), ("nChannels", wt.WORD), ("nSamplesPerSec", wt.DWORD),
                        ("nAvgBytesPerSec", wt.DWORD), ("nBlockAlign", wt.WORD), ("wBitsPerSample", wt.WORD),
                        ("cbSize", wt.WORD)]

        class WAVEHDR(ctypes.Structure):
            _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", wt.DWORD), ("dwBytesRecorded", wt.DWORD),
                        ("dwUser", ctypes.c_size_t), ("dwFlags", wt.DWORD), ("dwLoops", wt.DWORD),
                        ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]

        class MMTIME(ctypes.Structure):          # mmsystem.h is byte-packed: 4 + 8 bytes
            _pack_ = 1
            _fields_ = [("wType", ctypes.c_uint), ("u", ctypes.c_uint), ("u_hi", ctypes.c_uint)]

        self.MMTIME, self.WAVEHDR = MMTIME, WAVEHDR
        self.w = ctypes.windll.winmm
        fmt = WAVEFORMATEX(1, CH, RATE, RATE * CH * 2, CH * 2, 16, 0)
        self.h = ctypes.c_void_p()
        r = self.w.waveOutOpen(ctypes.byref(self.h), ctypes.c_uint(0xFFFFFFFF), ctypes.byref(fmt), 0, 0, 0)
        if r != 0:
            raise OSError(f"waveOutOpen failed ({r})")
        self.bufs = [ctypes.create_string_buffer(self.FRAMES * CH * 2) for _ in range(self.NBUF)]
        self.hdrs = [WAVEHDR() for _ in range(self.NBUF)]
        self.busy = [False] * self.NBUF
        self.i = 0
        self.pending = b""

    def _submit(self, chunk: bytes, stop: threading.Event) -> bool:
        k = self.i
        while self.busy[k]:
            if self.hdrs[k].dwFlags & 1:      # WHDR_DONE
                self.w.waveOutUnprepareHeader(self.h, ctypes.byref(self.hdrs[k]), ctypes.sizeof(self.WAVEHDR))
                self.busy[k] = False
                break
            if stop.is_set():
                return False
            time.sleep(0.005)
        ctypes.memmove(self.bufs[k], chunk, len(chunk))
        hd = self.hdrs[k]
        hd.lpData = ctypes.cast(self.bufs[k], ctypes.c_void_p)
        hd.dwBufferLength = len(chunk)
        hd.dwFlags = 0
        self.w.waveOutPrepareHeader(self.h, ctypes.byref(hd), ctypes.sizeof(self.WAVEHDR))
        self.w.waveOutWrite(self.h, ctypes.byref(hd), ctypes.sizeof(self.WAVEHDR))
        self.busy[k] = True
        self.i = (k + 1) % self.NBUF
        return True

    def write(self, data: bytes, stop: threading.Event):
        self.pending += data
        size = self.FRAMES * CH * 2
        while len(self.pending) >= size:
            if not self._submit(self.pending[:size], stop):
                return
            self.pending = self.pending[size:]

    def position(self) -> float:
        mt = self.MMTIME()
        mt.wType = 2                           # TIME_SAMPLES
        self.w.waveOutGetPosition(self.h, ctypes.byref(mt), ctypes.sizeof(mt))
        if mt.wType == 2:
            return mt.u / RATE
        return mt.u / (RATE * CH * 2)

    def close(self):
        self.w.waveOutReset(self.h)
        for k, hd in enumerate(self.hdrs):
            if self.busy[k]:
                self.w.waveOutUnprepareHeader(self.h, ctypes.byref(hd), ctypes.sizeof(self.WAVEHDR))
        self.w.waveOutClose(self.h)


def open_output():
    if sys.platform == "win32" and not os.environ.get("ENDORA_NO_SOUND"):
        try:
            return _WaveOut()
        except (OSError, AttributeError):
            pass
    return _NullOut()


# ---------------------------------------------------------------- helpers
def clip_at(layers: list[Layer], t: float) -> tuple[Optional[Layer], Optional[Clip]]:
    for l in layers:
        for c in l.clips:
            if c.start is not None and c.start <= t < c.start + c.duration:
                return l, c
    return None, None


def next_start(layers: list[Layer], t: float) -> Optional[float]:
    s = [c.start for l in layers for c in l.clips if c.start is not None and c.start > t]
    return min(s) if s else None


def read_frame(path: str, offset: float) -> Optional[QImage]:
    cmd = [tool("ffmpeg"), "-v", "error", "-nostdin", "-ss", f"{max(0.0, offset):.3f}", "-i", path, "-an",
           "-frames:v", "1", "-vf", f"scale={PREVIEW_W}:{PREVIEW_H}:force_original_aspect_ratio=decrease,"
           f"pad={PREVIEW_W}:{PREVIEW_H}:(ow-iw)/2:(oh-ih)/2", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    out = popen(cmd, stdout=-1, stderr=-1).communicate()[0]
    n = PREVIEW_W * PREVIEW_H * 3
    if len(out) < n:
        return None
    return QImage(out[:n], PREVIEW_W, PREVIEW_H, PREVIEW_W * 3, QImage.Format_RGB888).copy()


# ---------------------------------------------------------------- engine
class Player(QObject):
    frame = Signal(QImage, str)     # picture, caption
    ended = Signal()

    def __init__(self):
        super().__init__()
        self.stop_evt = threading.Event()
        self.threads: list[threading.Thread] = []
        self.procs: list = []
        self.out = None
        self.t0 = 0.0
        self.playing = False
        self.lock = threading.Lock()

    # time on the timeline (seconds), driven by the sound card
    def clock(self) -> float:
        if not self.playing or self.out is None:
            return self.t0
        return self.t0 + self.out.position()

    def play(self, t0: float, video_layers: list[Layer], sound: list[tuple[Clip, float]], end: float, fps: float):
        """video_layers: picture sources in priority order. sound: (clip, gain) pairs on the timeline."""
        self.stop()
        self.stop_evt = threading.Event()
        self.t0 = t0
        self.out = open_output()
        self.playing = True
        self.end = end
        stop = self.stop_evt
        a = threading.Thread(target=self._audio, args=(t0, sound, end, stop), daemon=True)
        v = threading.Thread(target=self._video, args=(video_layers, min(30.0, fps or 25.0), stop), daemon=True)
        self.threads = [a, v]
        a.start()
        v.start()

    def stop(self) -> float:
        t = self.clock()
        if self.playing:
            self.stop_evt.set()
            with self.lock:
                for p in self.procs:
                    try:
                        p.kill()
                    except OSError:
                        pass
                self.procs = []
            for th in self.threads:
                th.join(timeout=2)
            if self.out is not None:
                try:
                    self.out.close()
                except OSError:
                    pass
            self.playing = False
            self.t0 = t
        return t

    def _spawn(self, cmd, **kw):
        p = popen(cmd, **kw)
        with self.lock:
            self.procs.append(p)
        return p

    def _audio(self, t0: float, sound: list[tuple[Clip, float]], end: float, stop: threading.Event):
        length = max(0.5, min(WINDOW, end - t0))
        use = [(c, g) for c, g in sound if c.start is not None and c.start < t0 + length and c.start + c.duration > t0]
        use = use[:MAX_INPUTS]
        if not use:
            cmd = [tool("ffmpeg"), "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                   f"anullsrc=r={RATE}:cl=stereo", "-t", f"{length:.3f}"]
        else:
            cmd = [tool("ffmpeg"), "-v", "error", "-nostdin"]
            chains = []
            for i, (c, g) in enumerate(use):
                off = t0 - c.start
                if off > 0:
                    cmd += ["-ss", f"{off:.4f}"]
                cmd += ["-i", c.path]
                delay = int(round(max(0.0, -off) * 1000))
                pick = f"pan=stereo|c0=c{c.channel}|c1=c{c.channel}," if c.channels > 2 else ""
                chains.append(f"[{i}:a:0]aresample={RATE},{pick}aformat=sample_fmts=fltp:channel_layouts=stereo,"
                              f"adelay={delay}|{delay},volume={g:.3f}[a{i}]")
            mix = "".join(f"[a{i}]" for i in range(len(use)))
            graph = ";".join(chains) + f";{mix}amix=inputs={len(use)}:normalize=0:dropout_transition=0,apad[out]"
            cmd += ["-filter_complex", graph, "-map", "[out]", "-t", f"{length:.3f}"]
        cmd += ["-ac", str(CH), "-ar", str(RATE), "-f", "s16le", "-"]
        p = self._spawn(cmd, stdout=-1, stderr=-1)
        try:
            while not stop.is_set():
                data = p.stdout.read(RATE // 10 * CH * 2)
                if not data:
                    break
                self.out.write(data, stop)
            # let the last buffers play out
            while not stop.is_set() and self.clock() < t0 + length - 0.05:
                time.sleep(0.02)
        finally:
            try:
                p.kill()
            except OSError:
                pass
        if not stop.is_set():
            self.ended.emit()

    def _video(self, layers: list[Layer], fps: float, stop: threading.Event):
        n = PREVIEW_W * PREVIEW_H * 3
        while not stop.is_set():
            t = self.clock()
            layer, c = clip_at(layers, t)
            if c is None:
                self.frame.emit(QImage(), "No camera here")
                nxt = next_start(layers, t)
                if nxt is None:
                    return
                while not stop.is_set() and self.clock() < nxt:
                    time.sleep(0.03)
                continue
            off = t - c.start
            cmd = [tool("ffmpeg"), "-v", "error", "-nostdin", "-hwaccel", "auto", "-ss", f"{off:.3f}", "-i", c.path,
                   "-an", "-vf", f"fps={fps:.3f},scale={PREVIEW_W}:{PREVIEW_H}:force_original_aspect_ratio=decrease,"
                   f"pad={PREVIEW_W}:{PREVIEW_H}:(ow-iw)/2:(oh-ih)/2",
                   "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
            p = self._spawn(cmd, stdout=-1, stderr=-1)
            caption = f"{layer.name} · {c.name}"
            k = 0
            try:
                while not stop.is_set():
                    buf = p.stdout.read(n)
                    if len(buf) < n:
                        break
                    ft = c.start + off + k / fps
                    k += 1
                    if ft >= c.start + c.duration:
                        break
                    now = self.clock()
                    if ft < now - 0.12:
                        continue                       # late: drop
                    while not stop.is_set() and self.clock() < ft:
                        time.sleep(0.004)
                    self.frame.emit(QImage(buf, PREVIEW_W, PREVIEW_H, PREVIEW_W * 3, QImage.Format_RGB888).copy(),
                                    caption)
            finally:
                try:
                    p.kill()
                except OSError:
                    pass
            # clip finished (or decode failed): wait until the clock passes it, then pick the next one
            end = c.start + c.duration
            if self.clock() < end - 0.2 and not stop.is_set():
                # decoder stopped early (unreadable file): show nothing for the rest of this clip
                self.frame.emit(QImage(), f"Can't play {c.name}")
                while not stop.is_set() and self.clock() < end:
                    time.sleep(0.03)
