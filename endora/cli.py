"""Command line: endora-sync --camera DIR ... --audio DIR ... --mode tcv --out timeline.xml"""
import argparse

from .project import Project
from .sync import run_sync
from .resolve_export import export_resolve_script
from .xml_export import export_xml


def main(argv=None):
    ap = argparse.ArgumentParser(description="Endora MultiSync")
    ap.add_argument("--camera", action="append", default=[], help="folder of one camera's clips")
    ap.add_argument("--audio", action="append", default=[], help="folder of a recorder (e.g. Zoom card)")
    ap.add_argument("--mode", choices=["audio", "tc", "tcv"], default="tcv")
    ap.add_argument("--no-camera-audio", action="store_true")
    ap.add_argument("--out", required=True, help="output base name; writes <name>_Premiere.xml, <name>_DaVinci.xml and <name>_DaVinci.lua")
    a = ap.parse_args(argv)
    p = Project()
    p.camera_audio = not a.no_camera_audio
    for d in a.camera:
        p.add_camera_folder(d)
    for d in a.audio:
        p.add_recorder_folder(d)
    run_sync(p.layers, a.mode)
    for l in p.export_layers():
        for c in l.clips:
            st = "-" if c.start is None else f"{c.start:.3f}"
            print(f"{l.name:22} {c.name:22} start={st:>12} {c.status:5} {c.confidence * 100:5.1f}%  {c.method}: {c.note}")
    base = a.out[:-4] if a.out.lower().endswith(".xml") else a.out
    print(export_xml(p.export_layers(), base + "_Premiere.xml", target="premiere"))
    print(export_xml(p.export_layers(), base + "_DaVinci.xml", target="resolve"))
    print(export_resolve_script(p.export_layers(), base + "_DaVinci.lua"))


if __name__ == "__main__":
    main()
