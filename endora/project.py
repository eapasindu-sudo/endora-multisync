"""Project: the list of layers, with the 20 video / 30 audio limits enforced."""
from __future__ import annotations

import os
from typing import Callable, Optional

from .media import (AUDIO_EXT, MAX_AUDIO_LAYERS, MAX_VIDEO_LAYERS, VIDEO_EXT, Clip, Layer,
                    build_recorder_layers, list_media, make_clip)


class LimitError(Exception):
    pass


class Project:
    def __init__(self):
        self.layers: list[Layer] = []
        self.camera_audio = True   # put each camera's own sound on an audio layer too

    @property
    def video_layers(self) -> list[Layer]:
        return [l for l in self.layers if l.kind == "video"]

    @property
    def audio_layers(self) -> list[Layer]:
        return [l for l in self.layers if l.kind == "audio" and (self.camera_audio or l.source != "camera-audio")]

    def export_layers(self) -> list[Layer]:
        return self.video_layers + self.audio_layers

    def _check_audio(self, adding: int):
        if len(self.audio_layers) + adding > MAX_AUDIO_LAYERS:
            raise LimitError(f"That would make {len(self.audio_layers) + adding} audio layers. "
                             f"The limit is {MAX_AUDIO_LAYERS}. Remove some layers or turn off camera audio.")

    def add_camera(self, paths: list[str], name: Optional[str] = None, progress: Callable[[str], None] = None) -> Layer:
        if len(self.video_layers) >= MAX_VIDEO_LAYERS:
            raise LimitError(f"You already have {MAX_VIDEO_LAYERS} cameras, the most one timeline can hold.")
        clips = []
        for p in paths:
            if progress:
                progress(f"Reading {os.path.basename(p)}")
            c = make_clip(p)
            if c.kind == "video":
                clips.append(c)
        if not clips:
            raise ValueError("No video files found there.")
        n = len(self.video_layers) + 1
        layer = Layer(name or f"CAM {n:02d}", "video", clips, source="camera")
        audio = None
        if any(c.channels for c in clips):
            ac = [Clip(**{**c.__dict__}) for c in clips if c.channels]
            for c in ac:
                c.kind, c.channel = "audio", 0
            audio = Layer(f"{layer.name} audio", "audio", ac, source="camera-audio")
            if self.camera_audio:
                self._check_audio(1)
        # keep video layers together, camera audio right after other camera audio
        vi = max([i for i, l in enumerate(self.layers) if l.kind == "video"], default=-1) + 1
        self.layers.insert(vi, layer)
        if audio:
            ai = max([i for i, l in enumerate(self.layers) if l.source == "camera-audio"], default=len(self.video_layers) - 1) + 1
            self.layers.insert(ai, audio)
        return layer

    def add_camera_folder(self, folder: str, progress=None) -> Layer:
        name = os.path.basename(os.path.normpath(folder))
        return self.add_camera(list_media(folder, VIDEO_EXT), name, progress)

    def add_recorder(self, paths: list[str], name: str, progress=None) -> list[Layer]:
        new = build_recorder_layers(paths, name, progress)
        if not new:
            raise ValueError("No audio files found there.")
        self._check_audio(len(new))
        self.layers.extend(new)
        return new

    def add_recorder_folder(self, folder: str, progress=None) -> list[Layer]:
        name = os.path.basename(os.path.normpath(folder)) or "Recorder"
        return self.add_recorder(list_media(folder, AUDIO_EXT), name, progress)

    def remove(self, layer: Layer):
        if layer in self.layers:
            self.layers.remove(layer)
        if layer.kind == "video":
            paths = {c.path for c in layer.clips}
            self.layers = [l for l in self.layers
                           if not (l.source == "camera-audio" and {c.path for c in l.clips} <= paths)]


# ---------------------------------------------------------------- save / open
PROJECT_EXT = ".endora"


def save_project(project: Project, path: str, extra: Optional[dict] = None):
    """Everything needed to reopen a sync job without syncing again: layers, clips and their positions."""
    import dataclasses
    import json
    data = {"app": "Endora MultiSync", "format": 1, "camera_audio": project.camera_audio,
            "settings": extra or {},
            "layers": [{"name": l.name, "kind": l.kind, "source": l.source,
                        "clips": [dataclasses.asdict(c) for c in l.clips]} for l in project.layers]}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def load_project(path: str) -> tuple[Project, dict, list[str]]:
    """Returns the project, saved settings, and files that can't be found any more."""
    import dataclasses
    import json
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("app") != "Endora MultiSync":
        raise ValueError("That isn't an Endora MultiSync project file.")
    known = {f.name for f in dataclasses.fields(Clip)}
    p = Project()
    p.camera_audio = bool(data.get("camera_audio", True))
    missing = []
    for ld in data.get("layers", []):
        clips = [Clip(**{k: v for k, v in cd.items() if k in known}) for cd in ld.get("clips", [])]
        for c in clips:
            if not os.path.exists(c.path) and c.path not in missing:
                missing.append(c.path)
        p.layers.append(Layer(ld["name"], ld["kind"], clips, source=ld.get("source", "")))
    return p, data.get("settings", {}), missing


# ---------------------------------------------------------------- events
def split_events(layers: list[Layer], gap_minutes: float) -> list[tuple[float, float]]:
    """Group the timeline into events (Poruwa, reception, ...) wherever nothing was recording for
    longer than gap_minutes. Returns (start, end) pairs in timeline seconds."""
    spans = sorted((c.start, c.start + c.duration) for l in layers for c in l.clips if c.start is not None)
    if not spans:
        return []
    if not gap_minutes or gap_minutes <= 0:
        return [(spans[0][0], max(e for _, e in spans))]
    events = [[spans[0][0], spans[0][1]]]
    for s, e in spans[1:]:
        if s - events[-1][1] > gap_minutes * 60:
            events.append([s, e])
        else:
            events[-1][1] = max(events[-1][1], e)
    return [(s, e) for s, e in events]


def event_layers(layers: list[Layer], start: float, end: float) -> list[Layer]:
    """The same layers holding only the clips that belong to one event (empty layers are kept so
    track numbers stay the same in every event)."""
    out = []
    for l in layers:
        clips = [c for c in l.clips if c.start is not None and c.start < end and c.start + c.duration > start]
        out.append(Layer(l.name, l.kind, clips, source=l.source))
    return out
