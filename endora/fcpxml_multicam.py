"""Multicam clip as FCPXML, for DaVinci Resolve (File > Import > Timeline, or the Endora script).

Every camera layer becomes one angle (its clips in order, with gaps between them), every recorder input
becomes an audio angle. The edit timeline holds one multicam clip showing the first camera with the
Zoom sound underneath, so you can start cutting angles straight away."""
from __future__ import annotations

import os
import re
import urllib.parse
from fractions import Fraction
from xml.sax.saxutils import quoteattr

from .media import Clip, Layer
from .xml_export import _timeline, pick_size


def frame_duration(fps: float) -> Fraction:
    nominal = round(fps)
    if abs(fps - nominal) > 0.001:
        return Fraction(1001, nominal * 1000)
    return Fraction(1, nominal)


def unique_name(layer: Layer, c: Clip) -> str:
    """Clip names that stay unique even when every camera writes C0001.mov."""
    return f"{layer.name} - {c.name}"


def export_fcpxml_multicam(layers: list[Layer], out_path: str, name: str = "Endora MultiSync",
                           fps: float | None = None, path_map=None) -> dict:
    fps, origin_frames, seq_end = _timeline(layers, fps)
    fd = frame_duration(fps)
    width, height = pick_size(layers)

    def t(frames: int) -> str:
        v = fd * frames
        return "0s" if v == 0 else f"{v.numerator}/{v.denominator}s"

    def src(p: str) -> str:
        p = path_map(p) if path_map else os.path.abspath(p)
        p = p.replace("\\", "/")
        if not p.startswith("/"):
            p = "/" + p
        return "file://" + urllib.parse.quote(p, safe="/:")

    def frames(sec: float) -> int:
        return round(sec * fps)

    assets: dict[str, tuple[str, Clip, Layer]] = {}
    res = [f'<format id="r0" frameDuration="{t(1)}" width="{width}" height="{height}"/>']

    def asset(c: Clip, layer: Layer) -> str:
        if c.path in assets:
            return assets[c.path][0]
        aid = f"a{len(assets) + 1}"
        assets[c.path] = (aid, c, layer)
        tc = frames(c.tc_start) if c.tc_start is not None else 0
        video = c.kind == "video"
        attrs = (f'id="{aid}" name={quoteattr(unique_name(layer, c))} src={quoteattr(src(c.path))} start="{t(tc)}" '
                 f'duration="{t(max(1, frames(c.duration)))}" hasVideo="{1 if video else 0}" '
                 f'hasAudio="{1 if c.channels else 0}"')
        if video:
            attrs += ' format="r0"'
        if c.channels:
            attrs += f' audioSources="1" audioChannels="{c.channels}" audioRate="{c.sample_rate}"'
        res.append(f"<asset {attrs}/>")
        return aid

    def angle(layer: Layer, aid_name: str) -> str:
        """One angle: the layer's clips laid end to end with gaps, on the multicam's clock."""
        items, pos = [], origin_frames
        for c in sorted([c for c in layer.clips if c.start is not None], key=lambda c: c.start):
            s = frames(c.start)
            if s < pos:                      # overlap: trim the start so the angle stays in order
                cut = pos - s
                if cut >= frames(c.duration):
                    continue
            else:
                cut = 0
                if s > pos:
                    items.append(f'<gap name="Gap" offset="{t(pos)}" duration="{t(s - pos)}" start="{t(pos)}"/>')
                    pos = s
            dur = max(1, frames(c.duration) - cut)
            tc = frames(c.tc_start) if c.tc_start is not None else 0
            items.append(f'<asset-clip ref="{asset(c, layer)}" name={quoteattr(unique_name(layer, c))} '
                         f'offset="{t(pos)}" start="{t(tc + cut)}" duration="{t(dur)}"/>')
            pos += dur
        end = origin_frames + seq_end
        if pos < end:
            items.append(f'<gap name="Gap" offset="{t(pos)}" duration="{t(end - pos)}" start="{t(pos)}"/>')
        aname = re.sub(r"\s+L$", "", layer.name) if layer.kind == "audio" else layer.name
        return f'<mc-angle name={quoteattr(aname)} angleID="{aid_name}">{"".join(items)}</mc-angle>'

    v_layers = [l for l in layers if l.kind == "video" and any(c.start is not None for c in l.clips)]
    recs, seen = [], set()
    for l in layers:
        if l.kind == "audio" and l.source == "recorder":
            key = frozenset(c.path for c in l.clips)
            if key not in seen:
                seen.add(key)
                recs.append(l)
    angles = [(l, f"v{i + 1}") for i, l in enumerate(v_layers)] + [(l, f"r{i + 1}") for i, l in enumerate(recs)]
    angle_xml = "".join(angle(l, a) for l, a in angles)
    # sound for the edit: the Zoom's LR mix if there is one, else the first recorder input, else camera 1
    lr = next((a for l, a in angles if a.startswith("r") and "LR" in l.name.upper()), None)
    snd = lr or next((a for l, a in angles if a.startswith("r")), "v1")
    res.append(f'<media id="m1" name={quoteattr(name + " multicam")}><multicam format="r0" tcStart="{t(origin_frames)}" '
               f'tcFormat="NDF">{angle_xml}</multicam></media>')
    spine = (f'<mc-clip ref="m1" name={quoteattr(name + " multicam")} offset="{t(origin_frames)}" '
             f'start="{t(origin_frames)}" duration="{t(seq_end)}">'
             f'<mc-source angleID="v1" srcEnable="video"/><mc-source angleID="{snd}" srcEnable="audio"/></mc-clip>')
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n<fcpxml version="1.8">\n'
           f"<resources>{''.join(res)}</resources>\n"
           f'<library><event name={quoteattr(name)}><project name={quoteattr(name + " - Multicam edit")}>'
           f'<sequence format="r0" duration="{t(seq_end)}" tcStart="{t(origin_frames)}" tcFormat="NDF">'
           f"<spine>{spine}</spine></sequence></project></event></library>\n</fcpxml>\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xml)
    return {"angles": len(angles), "video_angles": len(v_layers), "audio_angles": len(recs),
            "names": {c.path: unique_name(l, c) for _aid, c, l in assets.values()}}
