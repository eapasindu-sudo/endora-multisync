"""Export a synced project as Final Cut Pro 7 XML (xmeml v5).
DaVinci Resolve (File > Import > Timeline) and Premiere Pro (File > Import) both read it."""
from __future__ import annotations

import math
import os
import urllib.parse
from collections import Counter
from xml.sax.saxutils import escape

from .media import Clip, Layer


def rate_info(fps: float) -> tuple[int, bool]:
    tb = round(fps)
    ntsc = abs(fps - tb) > 0.001
    return tb, ntsc


def frames_to_tc(frames: int, tb: int) -> str:
    frames = max(0, frames)
    f = frames % tb
    s = frames // tb
    return f"{s // 3600 % 24:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


def path_url(p: str) -> str:
    p = os.path.abspath(p).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p           # C:/x -> /C:/x
    return "file://localhost" + urllib.parse.quote(p, safe="/:")


def pick_fps(layers: list[Layer]) -> float:
    fps = [round(c.fps, 3) for l in layers if l.kind == "video" for c in l.clips if c.fps]
    return Counter(fps).most_common(1)[0][0] if fps else 25.0


def pick_size(layers: list[Layer]) -> tuple[int, int]:
    sizes = [(c.width, c.height) for l in layers if l.kind == "video" for c in l.clips if c.width]
    return Counter(sizes).most_common(1)[0][0] if sizes else (1920, 1080)


def _timeline(layers: list[Layer], fps: float | None):
    fps = fps or pick_fps(layers)
    placed = [c for l in layers for c in l.clips if c.start is not None]
    if not placed:
        raise RuntimeError("Nothing is synced yet. Run sync first.")
    origin_frames = math.floor(min(c.start for c in placed) * fps + 1e-6)
    seq_end = max(round(c.start * fps) + max(1, round(c.duration * fps)) for c in placed) - origin_frames
    return fps, origin_frames, seq_end


def export_xml(layers: list[Layer], out_path: str, seq_name: str = "Endora MultiSync", fps: float | None = None,
               target: str = "resolve", path_map=None) -> dict:
    """target: 'resolve' (DaVinci Resolve) or 'premiere' (Adobe Premiere Pro). Both are FCP 7 XML, shaped the way
    each program writes it, because Premiere refuses files that Resolve happily reads."""
    if target == "premiere":
        return export_premiere(layers, out_path, seq_name, fps, path_map)
    premiere = False
    fps, origin_frames, seq_end = _timeline(layers, fps)
    tb, ntsc = rate_info(fps)
    width, height = pick_size(layers)
    rate = f"<rate><timebase>{tb}</timebase><ntsc>{'TRUE' if ntsc else 'FALSE'}</ntsc></rate>"
    tc_xml = lambda fr: (f"<timecode>{rate}<string>{frames_to_tc(fr, tb)}</string><frame>{fr}</frame>"
                         f"<displayformat>NDF</displayformat></timecode>")
    file_ids: dict[str, str] = {}
    counter = {"clip": 0}
    warnings: list[str] = []

    def url(p: str) -> str:
        p = path_map(p) if path_map else os.path.abspath(p)
        p = p.replace("\\", "/")
        if not p.startswith("/"):
            p = "/" + p
        # Premiere writes the drive colon as %3a; Resolve reads either
        return "file://localhost" + urllib.parse.quote(p, safe="/" if premiere else "/:")

    def file_xml(c: Clip) -> str:
        if c.path in file_ids:
            return f'<file id="{file_ids[c.path]}"/>'
        fid = f"file-{len(file_ids) + 1}"
        file_ids[c.path] = fid
        dur = max(1, round(c.duration * fps))
        media = []
        if c.kind == "video" or c.width:
            extra = "<anamorphic>FALSE</anamorphic><pixelaspectratio>square</pixelaspectratio><fielddominance>none</fielddominance>" if premiere else ""
            media.append(f"<video><samplecharacteristics>{rate}<width>{c.width or width}</width>"
                         f"<height>{c.height or height}</height>{extra}</samplecharacteristics></video>")
        if c.channels:
            media.append(f"<audio><samplecharacteristics><depth>16</depth><samplerate>{c.sample_rate}</samplerate>"
                         f"</samplecharacteristics><channelcount>{c.channels}</channelcount></audio>")
        tc = tc_xml(round(c.tc_start * fps)) if c.tc_start is not None else (tc_xml(0) if premiere else "")
        return (f'<file id="{fid}"><name>{escape(c.name)}</name><pathurl>{escape(url(c.path))}</pathurl>'
                f"{rate}<duration>{dur}</duration>{tc}<media>{''.join(media)}</media></file>")

    def clipitem(c: Clip, mediatype: str, stereo: bool) -> str:
        counter["clip"] += 1
        n = counter["clip"]
        start = round(c.start * fps) - origin_frames
        dur = max(1, round(c.duration * fps))
        attrs = ""
        head = ""
        tail = ""
        if premiere:
            if mediatype == "audio":
                attrs = f' premiereChannelType="{"stereo" if stereo else "mono"}"'
        if mediatype == "video":
            tail = "<alphatype>none</alphatype><pixelaspectratio>square</pixelaspectratio><anamorphic>FALSE</anamorphic>" if premiere else ""
        body = file_xml(c)
        if mediatype == "audio":
            body += f"<sourcetrack><mediatype>audio</mediatype><trackindex>{1 if stereo else c.channel + 1}</trackindex></sourcetrack>"
        label = {"ok": "", "warn": "<labels><label2>Mango</label2></labels>"}.get(c.status, "<labels><label2>Rose</label2></labels>")
        return (f'<clipitem id="clipitem-{n}"{attrs}>{head}<name>{escape(c.name)}</name><enabled>TRUE</enabled>'
                f"<duration>{dur}</duration>{rate}<start>{start}</start><end>{start + dur}</end>"
                f"<in>0</in><out>{dur}</out>{tail}{body}{label}</clipitem>")

    def track(layer: Layer, mediatype: str, stereo: bool = False, out_ch: int = 1) -> str:
        items, last_end = [], -1
        for c in sorted([c for c in layer.clips if c.start is not None], key=lambda c: c.start):
            s = round(c.start * fps) - origin_frames
            if s < last_end:
                warnings.append(f"{layer.name}: {c.name} overlaps the clip before it")
            items.append(clipitem(c, mediatype, stereo))
            last_end = s + max(1, round(c.duration * fps))
        for c in layer.clips:
            if c.start is None:
                warnings.append(f"{layer.name}: {c.name} has no position")
        if premiere and mediatype == "audio":
            kind = "Stereo" if stereo else "Mono"
            return (f'<track currentExplodedTrackIndex="0" totalExplodedTrackCount="1" premiereTrackType="{kind}">'
                    f"{''.join(items)}<enabled>TRUE</enabled><locked>FALSE</locked>"
                    f"<outputchannelindex>{out_ch}</outputchannelindex></track>")
        return f"<track>{''.join(items)}<enabled>TRUE</enabled><locked>FALSE</locked></track>"

    v_layers = [l for l in layers if l.kind == "video"]
    a_layers = [l for l in layers if l.kind == "audio"]
    # Premiere opens a stereo file as ONE stereo clip, so an L/R pair becomes a single stereo track there
    a_tracks: list[tuple[Layer, bool]] = []
    if premiere:
        used = set()
        for l in a_layers:
            if id(l) in used:
                continue
            stereo = bool(l.clips) and all(c.channels == 2 for c in l.clips)
            if stereo:
                paths = {c.path for c in l.clips}
                for o in a_layers:
                    if o is not l and {c.path for c in o.clips} == paths:
                        used.add(id(o))
            a_tracks.append((l, stereo))
    else:
        a_tracks = [(l, False) for l in a_layers]

    out: list[str] = []
    w = out.append
    w(f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="{4 if premiere else 5}">')
    w(f'<sequence id="sequence-1">')
    if premiere:
        import uuid
        w(f"<uuid>{uuid.uuid4()}</uuid>")
    w(f"<duration>{seq_end}</duration>{rate}<name>{escape(seq_name)}</name>")
    w("<media><video><format><samplecharacteristics>"
      f"{rate}<width>{width}</width><height>{height}</height><anamorphic>FALSE</anamorphic>"
      "<pixelaspectratio>square</pixelaspectratio><fielddominance>none</fielddominance>"
      "<colordepth>24</colordepth></samplecharacteristics></format>")
    for l in v_layers:               # FCP XML lists video tracks bottom-up: V1 first
        w(track(l, "video"))
    w("</video><audio><numOutputChannels>2</numOutputChannels><format><samplecharacteristics>"
      "<depth>16</depth><samplerate>48000</samplerate></samplecharacteristics></format>")
    if premiere:
        w("<outputs><group><index>1</index><numchannels>1</numchannels><downmix>0</downmix><channel><index>1</index>"
          "</channel></group><group><index>2</index><numchannels>1</numchannels><downmix>0</downmix><channel>"
          "<index>2</index></channel></group></outputs>")
    for i, (l, stereo) in enumerate(a_tracks):
        w(track(l, "audio", stereo, 1 if stereo else (i % 2) + 1))
    w(f"</audio></media>{tc_xml(origin_frames)}</sequence></xmeml>\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("".join(out))
    return {"fps": fps, "video_tracks": len(v_layers), "audio_tracks": len(a_tracks),
            "start_tc": frames_to_tc(origin_frames, tb), "warnings": warnings}


TICKS = 254016000000   # Premiere ticks per second


def export_premiere(layers: list[Layer], out_path: str, seq_name: str = "Endora MultiSync", fps: float | None = None,
                    path_map=None, multicam: bool = False) -> dict:
    """FCP 7 XML laid out exactly like Premiere Pro writes it: stereo sound is a pair of 'exploded' tracks
    (left and right halves), every clip names its master clip, and a camera's picture and sound are linked."""
    fps, origin_frames, seq_end = _timeline(layers, fps)
    tb, ntsc = rate_info(fps)
    width, height = pick_size(layers)
    rate = f"<rate><timebase>{tb}</timebase><ntsc>{'TRUE' if ntsc else 'FALSE'}</ntsc></rate>"
    warnings: list[str] = []

    def tc_xml(fr: int) -> str:
        return (f"<timecode>{rate}<string>{frames_to_tc(fr, tb)}</string><frame>{fr}</frame>"
                f"<displayformat>NDF</displayformat></timecode>")

    def url(p: str) -> str:
        p = path_map(p) if path_map else os.path.abspath(p)
        p = p.replace("\\", "/")
        if not p.startswith("/"):
            p = "/" + p
        return "file://localhost" + urllib.parse.quote(p, safe="/").replace("%3A", "%3a")

    files: dict[str, int] = {}
    defined: set[str] = set()

    def fid(c: Clip) -> int:
        return files.setdefault(c.path, len(files) + 1)

    def file_xml(c: Clip) -> str:
        n = fid(c)
        if c.path in defined:
            return f'<file id="file-{n}"/>'
        defined.add(c.path)
        dur = max(1, round(c.duration * fps))
        media = []
        if c.kind == "video":
            media.append(f"<video><samplecharacteristics>{rate}<width>{c.width or width}</width>"
                         f"<height>{c.height or height}</height><anamorphic>FALSE</anamorphic>"
                         f"<pixelaspectratio>square</pixelaspectratio><fielddominance>none</fielddominance>"
                         f"</samplecharacteristics></video>")
        if c.channels:
            media.append(f"<audio><samplecharacteristics><depth>16</depth><samplerate>{c.sample_rate}</samplerate>"
                         f"</samplecharacteristics><channelcount>{c.channels}</channelcount></audio>")
        tcf = round(c.tc_start * fps) if c.tc_start is not None else 0
        return (f'<file id="file-{n}"><name>{escape(c.name)}</name><pathurl>{escape(url(c.path))}</pathurl>'
                f"{rate}<duration>{dur}</duration>{tc_xml(tcf)}<media>{''.join(media)}</media></file>")

    # ---- lay out tracks: each entry is (kind, attrs, [(clip, sourcetrack, channel_type)], outputchannel)
    v_layers = [l for l in layers if l.kind == "video"]
    a_layers = [l for l in layers if l.kind == "audio"]

    def placed(l: Layer) -> list[Clip]:
        for c in l.clips:
            if c.start is None:
                warnings.append(f"{l.name}: {c.name} has no position")
        return sorted([c for c in l.clips if c.start is not None], key=lambda c: c.start)

    v_tracks = [placed(l) for l in v_layers]
    a_tracks: list[dict] = []
    track_layers: list[Layer] = []
    used = set()
    for l in a_layers:
        if id(l) in used:
            continue
        clips = placed(l)
        chans = Counter(c.channels for c in clips).most_common(1)[0][0] if clips else 1
        if chans == 2:
            # a stereo file: one Premiere stereo track = two exploded halves
            paths = {c.path for c in l.clips}
            for o in a_layers:
                if o is not l and o.kind == "audio" and {c.path for c in o.clips} == paths:
                    used.add(id(o))
            for half in (0, 1):
                a_tracks.append({"type": "Stereo", "index": half, "total": 2, "out": half + 1,
                                 "clips": [(c, half + 1, "stereo") for c in clips if c.channels >= 2 or half == 0]})
                track_layers.append(l)
        else:
            # mono file, or one channel of a multi-channel file
            a_tracks.append({"type": "Mono", "index": 0, "total": 1, "out": len(a_tracks) % 2 + 1,
                             "clips": [(c, (c.channel + 1) if c.channels > 1 else 1, "mono") for c in clips]})
            track_layers.append(l)

    def label(c: Clip) -> str:
        return {"ok": "", "warn": "<labels><label2>Mango</label2></labels>"}.get(c.status, "<labels><label2>Rose</label2></labels>")

    def clip_common(c: Clip) -> str:
        start = round(c.start * fps) - origin_frames
        dur = max(1, round(c.duration * fps))
        ticks = round(dur / fps * TICKS)
        return (f"<masterclipid>masterclip-{fid(c)}</masterclipid><name>{escape(c.name)}</name>"
                f"<enabled>TRUE</enabled><duration>{dur}</duration>{rate}<start>{start}</start><end>{start + dur}</end>"
                f"<in>0</in><out>{dur}</out><pproTicksIn>0</pproTicksIn><pproTicksOut>{ticks}</pproTicksOut>")

    def check_overlaps(name: str, clips: list[Clip]):
        last_end = -1
        for c in clips:
            s = round(c.start * fps) - origin_frames
            if s < last_end:
                warnings.append(f"{name}: {c.name} overlaps the clip before it")
            last_end = s + max(1, round(c.duration * fps))

    counter = [0]

    def media_xml(v_list: list[tuple[str, list[Clip]]], a_list: list[dict], nest: str = "") -> str:
        """<media> of one sequence. nest: full xml of a sequence to place on V1 as one nested clip."""
        ids: dict[tuple, str] = {}
        where: dict[str, tuple[str, int, int]] = {}      # clipitem id -> (mediatype, trackindex, clipindex)
        by_path: dict[str, list[str]] = {}

        def new_id() -> str:
            counter[0] += 1
            return f"clipitem-{counter[0]}"
        for ti, (_n, clips) in enumerate(v_list, 1):
            for ci, c in enumerate(clips, 1):
                cid = ids[("v", ti, ci)] = new_id()
                where[cid] = ("video", ti, ci)
                by_path.setdefault(c.path, []).append(cid)
        for ti, tr in enumerate(a_list, 1):
            for ci, (c, st, ct) in enumerate(tr["clips"], 1):
                cid = ids[("a", ti, ci)] = new_id()
                where[cid] = ("audio", ti, ci)
                # only link camera sound to its picture (same file); recorder files stand alone
                if any(c.path == v.path for _n, clips in v_list for v in clips):
                    by_path.setdefault(c.path, []).append(cid)

        def links(cid: str, c: Clip) -> str:
            group = by_path.get(c.path, [])
            if len(group) < 2 or cid not in group:
                return ""
            out = []
            for other in group:
                mt, ti, ci = where[other]
                g = "<groupindex>1</groupindex>" if mt == "audio" else ""
                out.append(f"<link><linkclipref>{other}</linkclipref><mediatype>{mt}</mediatype>"
                           f"<trackindex>{ti}</trackindex><clipindex>{ci}</clipindex>{g}</link>")
            return "".join(out)

        out: list[str] = []
        w = out.append
        w(f"<media><video><format><samplecharacteristics>{rate}<width>{width}</width>"
          f"<height>{height}</height><anamorphic>FALSE</anamorphic><pixelaspectratio>square</pixelaspectratio>"
          f"<fielddominance>none</fielddominance><colordepth>24</colordepth></samplecharacteristics></format>\n")
        if nest:
            ticks = round(seq_end / fps * TICKS)
            w(f'<track><clipitem id="{new_id()}" frameBlend="FALSE"><name>{escape(seq_name)} (multicam)</name>'
              f"<enabled>TRUE</enabled><duration>{seq_end}</duration>{rate}<start>0</start><end>{seq_end}</end>"
              f"<in>0</in><out>{seq_end}</out><pproTicksIn>0</pproTicksIn><pproTicksOut>{ticks}</pproTicksOut>"
              f"<alphatype>none</alphatype><pixelaspectratio>square</pixelaspectratio><anamorphic>FALSE</anamorphic>"
              f"{nest}</clipitem>\n<enabled>TRUE</enabled><locked>FALSE</locked></track>\n")
        for ti, (lname, clips) in enumerate(v_list, 1):
            check_overlaps(lname, clips)
            w("<track>")
            for ci, c in enumerate(clips, 1):
                cid = ids[("v", ti, ci)]
                w(f'<clipitem id="{cid}" frameBlend="FALSE">{clip_common(c)}<alphatype>none</alphatype>'
                  f"<pixelaspectratio>square</pixelaspectratio><anamorphic>FALSE</anamorphic>{file_xml(c)}"
                  f"{links(cid, c)}{label(c)}</clipitem>\n")
            w("<enabled>TRUE</enabled><locked>FALSE</locked></track>\n")
        w("</video>\n<audio><numOutputChannels>2</numOutputChannels><format><samplecharacteristics><depth>16</depth>"
          "<samplerate>48000</samplerate></samplecharacteristics></format><outputs>"
          "<group><index>1</index><numchannels>1</numchannels><downmix>0</downmix><channel><index>1</index></channel></group>"
          "<group><index>2</index><numchannels>1</numchannels><downmix>0</downmix><channel><index>2</index></channel></group>"
          "</outputs>\n")
        for ti, tr in enumerate(a_list, 1):
            w(f'<track currentExplodedTrackIndex="{tr["index"]}" totalExplodedTrackCount="{tr["total"]}" '
              f'premiereTrackType="{tr["type"]}">')
            for ci, (c, st, ct) in enumerate(tr["clips"], 1):
                cid = ids[("a", ti, ci)]
                w(f'<clipitem id="{cid}" frameBlend="FALSE" premiereChannelType="{ct}">{clip_common(c)}{file_xml(c)}'
                  f"<sourcetrack><mediatype>audio</mediatype><trackindex>{st}</trackindex></sourcetrack>"
                  f"{links(cid, c)}{label(c)}</clipitem>\n")
            w(f"<enabled>TRUE</enabled><locked>FALSE</locked><outputchannelindex>{tr['out']}</outputchannelindex></track>\n")
        w("</audio></media>")
        return "".join(out)

    import uuid

    def sequence(sid: str, name: str, media: str) -> str:
        return (f'<sequence id="{sid}" explodedTracks="true"><uuid>{uuid.uuid4()}</uuid><duration>{seq_end}</duration>'
                f"{rate}<name>{escape(name)}</name>\n{media}{tc_xml(origin_frames)}</sequence>")

    v_list = [(l.name, clips) for l, clips in zip(v_layers, v_tracks)]
    synced = sequence("sequence-1", seq_name, media_xml(v_list, a_tracks))
    body = synced
    if multicam:
        # the synced sequence nested on V1 (cameras = angles), with the recorder sound underneath
        rec_tracks = [tr for tr, l in zip(a_tracks, track_layers) if l.source == "recorder"]
        body = sequence("sequence-2", f"{seq_name} - Multicam edit", media_xml([], rec_tracks, nest=synced))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">\n' + body + "</xmeml>\n")
    return {"fps": fps, "video_tracks": len(v_layers), "audio_tracks": len(a_tracks),
            "start_tc": frames_to_tc(origin_frames, tb), "warnings": warnings}
