"""Auto update: ask GitHub for the latest release, download its installer and run it silently.
The installer closes the app, installs over it and starts the new version."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.request
from typing import Callable, Optional

from .version import UPDATE_REPO, VERSION

ASSET = re.compile(r"^EndoraMultiSync-Setup-[\d.]+\.exe$")


def parse(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def api_url() -> str:
    return os.environ.get("ENDORA_UPDATE_API") or f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"


def _get(url: str, timeout: float = 15):
    req = urllib.request.Request(url, headers={"User-Agent": f"EndoraMultiSync/{VERSION}",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def latest() -> Optional[dict]:
    """The newest release if it is newer than this copy, else None."""
    with _get(api_url()) as r:
        rel = json.load(r)
    tag = rel.get("tag_name") or ""
    if rel.get("draft") or rel.get("prerelease") or parse(tag) <= parse(VERSION):
        return None
    asset = next((a for a in rel.get("assets", []) if ASSET.match(a.get("name", ""))), None)
    if not asset:
        return None
    return {"version": tag.lstrip("v"), "url": asset["browser_download_url"], "name": asset["name"],
            "size": asset.get("size", 0), "notes": (rel.get("body") or "").strip()}


def download(info: dict, progress: Callable[[float], None] = lambda f: None) -> str:
    folder = os.path.join(tempfile.gettempdir(), "EndoraMultiSync-update")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, info["name"])
    part = path + ".part"
    with _get(info["url"], timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or info.get("size") or 0)
        got = 0
        while True:
            buf = r.read(1 << 20)
            if not buf:
                break
            f.write(buf)
            got += len(buf)
            if total:
                progress(min(1.0, got / total))
    if info.get("size") and os.path.getsize(part) != info["size"]:
        raise RuntimeError("The update download was incomplete. Try again later.")
    os.replace(part, path)
    return path


def install(path: str) -> None:
    """Start the installer silently; it waits for this app to close, then relaunches it."""
    subprocess.Popen([path, "/S"], close_fds=True)
