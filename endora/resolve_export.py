"""DaVinci Resolve export: a Lua script that builds the synced timeline inside Resolve.

Resolve links XML clips by file name, so cameras that all write C0001.mov get mixed up.
The script imports every file by its full path instead, so each clip lands on the right track.
Run it from Resolve: Workspace > Scripts > Endora - <name>."""
from __future__ import annotations

import os
import re

from .media import Layer


def scripts_dir() -> str | None:
    """Resolve's per-user Utility scripts folder (shows up in Workspace > Scripts)."""
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if not base:
            return None
        return os.path.join(base, "Blackmagic Design", "DaVinci Resolve", "Support", "Fusion", "Scripts", "Utility")
    if os.uname().sysname == "Darwin":
        return os.path.expanduser("~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility")
    return os.path.expanduser("~/.local/share/DaVinciResolve/Fusion/Scripts/Utility")


def _lua_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _abs(p: str) -> str:
    return p if re.match(r"^[A-Za-z]:[\\/]", p) else os.path.abspath(p)


def _num(x: float) -> str:
    return f"{x:.6f}".rstrip("0").rstrip(".") or "0"


def _event_lua(layers: list[Layer], name: str) -> tuple[str, int, int]:
    v_layers = [l for l in layers if l.kind == "video"]
    a_layers = [l for l in layers if l.kind == "audio"]
    placed = [c for l in layers for c in l.clips if c.start is not None]
    if not placed:
        raise RuntimeError("Nothing is synced yet. Run sync first.")
    origin = min(c.start for c in placed)

    # camera sound rides with its picture (same track number), so skip camera-audio layers
    # and link audio to the video clip instead when the camera-audio layer is in the export
    cam_audio_paths = {c.path for l in a_layers if l.source == "camera-audio" for c in l.clips}
    # a stereo file shows up as an L and an R layer; Resolve places it once on a stereo track
    a_tracks: list[tuple[Layer, int]] = []
    seen: list[tuple] = []
    for l in a_layers:
        if l.source == "camera-audio":
            continue
        key = (l.name.rsplit(" ", 1)[0] if l.name.endswith((" L", " R")) else l.name,)
        paths = {c.path for c in l.clips}
        if (paths and paths in [x[1] for x in seen]) or key in [x[0] for x in seen]:
            continue
        seen.append((key, paths))
        ch = max((c.channels for c in l.clips), default=1)
        a_tracks.append((l, ch))

    out = [f"  {{name={_lua_str(name)}, origin={_num(origin)},"]
    w = out.append
    w("   V={")
    for l in v_layers:
        items = []
        for c in sorted([c for c in l.clips if c.start is not None], key=lambda c: c.start):
            items.append(f"{{p={_lua_str(_abs(c.path))}, t={_num(c.start - origin)}, s={_lua_str(c.status)}, "
                         f"a={'true' if c.path in cam_audio_paths else 'false'}}}")
        w(f"    {{name={_lua_str(l.name)}, clips={{{', '.join(items)}}}}},")
    w("   },")
    w("   A={")
    for l, ch in a_tracks:
        items = []
        for c in sorted([c for c in l.clips if c.start is not None], key=lambda c: c.start):
            items.append(f"{{p={_lua_str(_abs(c.path))}, t={_num(c.start - origin)}, s={_lua_str(c.status)}}}")
        tname = re.sub(r"\s+L$", "", l.name) if ch == 2 else l.name
        kind = "mono" if ch == 1 else "stereo"
        w(f"    {{name={_lua_str(tname)}, kind={_lua_str(kind)}, clips={{{', '.join(items)}}}}},")
    w("   }},")
    return "\n".join(out), len(v_layers), len(a_tracks)


def export_resolve_script(layers: list[Layer], out_path: str, seq_name: str = "Endora MultiSync",
                          fps: float | None = None, events: list[tuple[str, list[Layer]]] | None = None,
                          multicam: list[tuple[str, str]] | None = None, names: dict | None = None) -> dict:
    """events: optional [(timeline name, layers)] to build several timelines (one per event) in one go.
    multicam: [(fcpxml path, timeline name)] multicam edits to import after the media is in the pool.
    names: {file path: unique clip name} so clips from different cameras never share a name."""
    events = events or [(seq_name, layers)]
    lines = ["-- Endora MultiSync: builds the synced timeline(s) in the current DaVinci Resolve project",
             f"local BIN = {_lua_str(seq_name)}",
             f"local FPS = {_num(round(fps, 3)) if fps else 'nil'}",
             "local EVENTS = {"]
    nv = na = 0
    for name, ls in events:
        block, nv, na = _event_lua(ls, name)
        lines.append(block)
    lines.append("}")
    lines.append("local MULTICAM = {" + ", ".join(f"{{f={_lua_str(_abs(f))}, name={_lua_str(n)}}}"
                                                 for f, n in (multicam or [])) + "}")
    lines.append("local NAMES = {" + ", ".join(f"[{_lua_str(_abs(k))}]={_lua_str(v)}"
                                              for k, v in (names or {}).items()) + "}")
    lines.append(LUA_BODY)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    return {"video_tracks": nv, "audio_tracks": na, "timelines": len(events)}


LUA_BODY = r'''
local resolve = resolve or Resolve()
local pm = resolve:GetProjectManager()
local project = pm:GetCurrentProject()
if not project then print("Endora: open a project first") return end
local mp = project:GetMediaPool()

local function say(msg) print("Endora: " .. msg) end

-- 1. import every file by its full path, each once (across all events)
local want, order = {}, {}
local function need(p) if not want[p] then want[p] = true; table.insert(order, p) end end
for _, ev in ipairs(EVENTS) do
  for _, tr in ipairs(ev.V) do for _, c in ipairs(tr.clips) do need(c.p) end end
  for _, tr in ipairs(ev.A) do for _, c in ipairs(tr.clips) do need(c.p) end end
end

local function norm(p) return string.lower((string.gsub(p, "/", "\\"))) end
local items = {}
local function index(list)
  for _, it in pairs(list or {}) do
    local fp = it:GetClipProperty("File Path")
    if fp and fp ~= "" then items[norm(fp)] = it end
  end
end

local root = mp:GetRootFolder()
local bin = mp:AddSubFolder(root, BIN) or root
mp:SetCurrentFolder(bin)
index(mp:ImportMedia(order))
local missing = {}
for _, p in ipairs(order) do
  if not items[norm(p)] then
    local got = mp:ImportMedia({p})
    index(got)
    if not items[norm(p)] then table.insert(missing, p) end
  end
end

if FPS then
  local tlcount = project:GetTimelineCount()
  if tlcount == 0 then project:SetSetting("timelineFrameRate", tostring(FPS)) end
end

local function build(NAME, ORIGIN, V, A)
-- 2. empty timeline at the right rate
local tlname, n = NAME, 1
local tl = mp:CreateEmptyTimeline(tlname)
while not tl and n < 50 do n = n + 1; tlname = NAME .. " " .. n; tl = mp:CreateEmptyTimeline(tlname) end
if not tl then say("could not create a timeline") return end
project:SetCurrentTimeline(tl)
local function tlrate() return tonumber(tl:GetSetting("timelineFrameRate")) end
if FPS and math.abs((tlrate() or FPS) - FPS) > 0.01 then
  tl:SetSetting("useCustomSettings", "1")
  tl:SetSetting("timelineFrameRate", tostring(FPS))
end
local rate = tlrate() or FPS or 25
local nominal = math.floor(rate + 0.5)

local function tc(sec)
  local fr = math.floor(sec * nominal + 0.5)
  local f = fr % nominal; local s = math.floor(fr / nominal)
  return string.format("%02d:%02d:%02d:%02d", math.floor(s / 3600) % 24, math.floor(s / 60) % 60, s % 60, f)
end
tl:SetStartTimecode(tc(ORIGIN))
local base = tl:GetStartFrame()

-- 3. tracks: V1..Vn cameras with their own sound on A1..An, then one track per recorder input
while tl:GetTrackCount("video") < #V do tl:AddTrack("video") end
local firstRec = #V + 1
-- the default A1 is stereo; camera tracks stay stereo, recorder tracks get their own type
while tl:GetTrackCount("audio") < #V do tl:AddTrack("audio", "stereo") end
for i, tr in ipairs(A) do
  local idx = #V + i
  if tl:GetTrackCount("audio") < idx then tl:AddTrack("audio", tr.kind) end
end
for i, tr in ipairs(V) do tl:SetTrackName("video", i, tr.name) end
for i, tr in ipairs(V) do if i <= tl:GetTrackCount("audio") then tl:SetTrackName("audio", i, tr.name) end end
for i, tr in ipairs(A) do tl:SetTrackName("audio", #V + i, tr.name) end

-- 4. place clips at their synced positions
local infos, colors = {}, {}
local function add(c, track, mtype)
  local it = items[norm(c.p)]
  if not it then return end
  local frames = tonumber(it:GetClipProperty("Frames")) or 1
  table.insert(infos, {mediaPoolItem = it, startFrame = 0, endFrame = math.max(0, frames - 1),
                       trackIndex = track, recordFrame = base + math.floor(c.t * rate + 0.5), mediaType = mtype})
  table.insert(colors, c.s)
end
for i, tr in ipairs(V) do
  for _, c in ipairs(tr.clips) do
    add(c, i, 1)
    if c.a then add(c, i, 2) end
  end
end
for i, tr in ipairs(A) do
  for _, c in ipairs(tr.clips) do add(c, #V + i, 2) end
end

local placedCount = 0
local added = mp:AppendToTimeline(infos) or {}
for k, ti in ipairs(added) do
  placedCount = placedCount + 1
  local s = colors[k]
  if s == "bad" then ti:SetClipColor("Orange") elseif s == "warn" then ti:SetClipColor("Yellow") end
end

say(string.format("timeline '%s' built: %d of %d clip parts placed", tlname, placedCount, #infos))
end

for _, ev in ipairs(EVENTS) do build(ev.name, ev.origin, ev.V, ev.A) end

-- 5. give every clip a unique name (cameras all write C0001.mov), then bring in the multicam edits
for p, nm in pairs(NAMES) do
  local it = items[norm(p)]
  if it then it:SetClipProperty("Clip Name", nm) end
end
for _, m in ipairs(MULTICAM) do
  local ok, tl2 = pcall(function()
    return mp:ImportTimelineFromFile(m.f, {timelineName = m.name, importSourceClips = false})
  end)
  if ok and tl2 then
    say("multicam timeline '" .. m.name .. "' added")
  else
    say("couldn't add the multicam timeline automatically. Use File > Import > Timeline and pick:")
    print("  " .. m.f)
  end
end
if #missing > 0 then
  say(#missing .. " file(s) could not be imported:")
  for _, p in ipairs(missing) do print("  " .. p) end
end
'''
