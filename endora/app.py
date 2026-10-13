"""Endora MultiSync desktop app."""
from __future__ import annotations

import os
import sys
import threading
import traceback

import numpy as np

from PySide6.QtCore import QLineF, QObject, QPointF, QRectF, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog,
                               QFrame, QHBoxLayout, QHeaderView, QLabel, QMainWindow, QMessageBox, QProgressBar,
                               QPushButton, QScrollArea, QSizePolicy, QSlider, QSplitter, QTableWidget,
                               QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import sync as sync_mod
from .media import AUDIO_EXT, MAX_AUDIO_LAYERS, MAX_VIDEO_LAYERS, VIDEO_EXT, Layer, list_media
from .player import Player, clip_at, read_frame
from .project import PROJECT_EXT, LimitError, Project, event_layers, load_project, save_project, split_events
from .resolve_export import export_resolve_script, scripts_dir
from .waveform import PEAK_RATE, WaveCache, columns
from .fcpxml_multicam import export_fcpxml_multicam
from .xml_export import export_premiere, export_xml, pick_fps

APP_NAME = "Endora MultiSync"
from .version import VERSION
from . import updater

C = {
    "bg": "#0f1216", "panel": "#161a20", "panel2": "#1c212a", "line": "#272e39", "line2": "#323b48",
    "fg": "#e7eaef", "muted": "#8a94a5", "dim": "#5d6676", "accent": "#f2a33a", "audio": "#4fc3d9",
    "video": "#7f8cff", "ok": "#4fd18b", "warn": "#f2c94c", "bad": "#ff6b6b",
}

QSS = f"""
* {{ font-family: 'Segoe UI', 'Archivo', sans-serif; font-size: 10pt; color: {C['fg']}; }}
QMainWindow, QWidget#root {{ background: {C['bg']}; }}
QFrame#panel {{ background: {C['panel']}; border: 1px solid {C['line']}; border-radius: 8px; }}
QFrame#topbar {{ background: {C['panel']}; border-bottom: 1px solid {C['line']}; }}
QLabel#h2 {{ color: {C['muted']}; font-size: 8.5pt; font-weight: 600; letter-spacing: 2px; }}
QLabel#logo {{ font-size: 14pt; font-weight: 800; letter-spacing: 1px; }}
QLabel#muted {{ color: {C['muted']}; }}
QLabel#big {{ font-family: Consolas, 'JetBrains Mono', monospace; font-size: 16pt; font-weight: 700; }}
QPushButton {{ background: {C['panel2']}; border: 1px solid {C['line2']}; border-radius: 6px; padding: 6px 12px; font-weight: 600; }}
QPushButton:hover {{ border-color: {C['dim']}; }}
QPushButton:disabled {{ color: {C['dim']}; }}
QPushButton#primary {{ background: {C['accent']}; border-color: {C['accent']}; color: #1a1205; }}
QPushButton#primary:disabled {{ background: #6b5330; color: #2a2110; }}
QPushButton#seg {{ background: transparent; border: 1px solid transparent; color: {C['muted']}; padding: 6px 12px; }}
QPushButton#seg:checked {{ background: {C['panel2']}; border: 1px solid {C['line2']}; color: {C['fg']}; }}
QFrame#segwrap {{ background: {C['bg']}; border: 1px solid {C['line2']}; border-radius: 8px; }}
QTreeWidget, QTableWidget {{ background: {C['panel']}; border: none; alternate-background-color: {C['panel2']};
    gridline-color: {C['line']}; selection-background-color: #2a3242; }}
QHeaderView::section {{ background: {C['panel']}; color: {C['dim']}; border: none; border-bottom: 1px solid {C['line']};
    padding: 5px; font-size: 8.5pt; font-weight: 600; }}
QScrollArea {{ background: {C['panel']}; border: none; }}
QScrollBar:vertical, QScrollBar:horizontal {{ background: {C['panel']}; width: 10px; height: 10px; }}
QScrollBar::handle {{ background: {C['line2']}; border-radius: 4px; min-height: 20px; min-width: 20px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QProgressBar {{ background: {C['line']}; border: none; border-radius: 2px; height: 4px; }}
QProgressBar::chunk {{ background: {C['accent']}; border-radius: 2px; }}
QComboBox {{ background: {C['panel2']}; border: 1px solid {C['line2']}; border-radius: 6px; padding: 4px 8px; }}
QComboBox QAbstractItemView {{ background: {C['panel2']}; }}
QCheckBox::indicator {{ width: 14px; height: 14px; }}
QSplitter::handle {{ background: {C['bg']}; }}
QMessageBox {{ background: {C['panel']}; }}
QToolTip {{ background: {C['panel2']}; border: 1px solid {C['line2']}; color: {C['fg']}; }}
"""

MODES = [("audio", "Audio waveform"), ("tc", "Timecode"), ("tcv", "Timecode + audio verify")]
STATUS_TEXT = {"ok": "Locked", "warn": "Corrected", "bad": "Needs you", "wait": "Waiting"}
STATUS_COLOR = {"ok": C["ok"], "warn": C["warn"], "bad": C["bad"], "wait": C["dim"]}


def tc_string(sec: float | None, fps: float) -> str:
    if sec is None:
        return "—"
    tb = round(fps)
    fr = round(sec * fps)
    f, s = fr % tb, fr // tb
    return f"{s // 3600 % 24:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


class Worker(QObject):
    message = Signal(str)
    progress = Signal(float)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn(self))
        except LimitError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001 - shown to the user
            traceback.print_exc()
            self.failed.emit(str(e) or e.__class__.__name__)


class Timeline(QWidget):
    HEAD = 170
    RULER = 40
    VH = 30
    AH = 34

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.pps = 4.0
        self.setMinimumHeight(200)
        self.setFocusPolicy(Qt.ClickFocus)
        self.drag = None          # (clip group, original starts, press x)

    def rows(self):
        return self.win.project.export_layers()

    def span(self):
        cl = [c for l in self.rows() for c in l.clips if c.start is not None]
        if not cl:
            cl = [c for l in self.rows() for c in l.clips]
            return 0.0, max([c.duration for c in cl] + [60])
        lo = min(c.start for c in cl)
        return lo, max(c.start + c.duration for c in cl) - lo

    def refresh(self):
        lo, length = self.span()
        h = self.RULER + sum(self.VH if l.kind == "video" else self.AH for l in self.rows()) + 4
        self.setMinimumSize(int(self.HEAD + length * self.pps + 60), max(200, h))
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(C["panel"]))
        lo, length = self.span()
        fps = self.win.current_fps()
        mono = QFont("Consolas", 8)
        # ruler
        p.setFont(mono)
        step = 10 if self.pps >= 8 else 30 if self.pps >= 3 else 60 if self.pps >= 1 else 300
        t = 0.0
        while t <= length + step:
            x = self.HEAD + t * self.pps
            p.setPen(QColor(C["line2"]))
            p.drawLine(int(x), 22, int(x), self.height())
            p.setPen(QColor(C["muted"]))
            p.drawText(int(x) + 3, 34, tc_string(lo + t, fps)[:8])
            t += step
        # events (Poruwa, reception, ...) as bands above the ruler
        evs = self.win.events()
        if len(evs) > 1:
            for i, (es, ee) in enumerate(evs):
                r = QRectF(self.HEAD + (es - lo) * self.pps, 3, max(4.0, (ee - es) * self.pps), 15)
                band = QColor(C["accent"])
                band.setAlpha(60)
                p.setBrush(band)
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(r, 3, 3)
                p.setPen(QColor(C["fg"]))
                p.setFont(QFont("Segoe UI", 8, QFont.Bold))
                p.drawText(r.adjusted(6, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft,
                           f"Event {i + 1} · {tc_string(es, fps)[:5]}–{tc_string(ee, fps)[:5]}")
            p.setFont(mono)
        p.setPen(QColor(C["line2"]))
        p.drawLine(0, self.RULER, self.width(), self.RULER)
        y = self.RULER
        v_i = a_i = 0
        for l in self.rows():
            h = self.VH if l.kind == "video" else self.AH
            if l.kind == "video":
                v_i += 1
                tag, col = f"V{v_i}", QColor(C["video"])
            else:
                a_i += 1
                tag, col = f"A{a_i}", QColor(C["audio"])
            p.setPen(QColor(C["line"]))
            p.drawLine(0, y + h, self.width(), y + h)
            for c in l.clips:
                if c.start is None:
                    continue
                x = self.HEAD + (c.start - lo) * self.pps
                w = max(2.0, c.duration * self.pps)
                r = QRectF(x, y + 3, w, h - 6)
                fill = QColor(col)
                fill.setAlpha(70 if l.kind == "video" else 45)
                border = QColor(STATUS_COLOR[c.status]) if c.status in ("warn", "bad") else QColor(col)
                if c.status == "bad":
                    fill = QColor(STATUS_COLOR["bad"])
                    fill.setAlpha(55)
                sel = c in self.win.selected_group()
                p.setPen(QPen(QColor(C["accent"]) if sel else border, 2 if sel else 1))
                p.setBrush(QBrush(fill))
                p.drawRoundedRect(r, 4, 4)
                if l.kind == "audio" and w > 6:
                    self._wave(p, r, c, _ev.rect())
                p.setPen(QColor(C["fg"]))
                p.setFont(QFont("Segoe UI", 8))
                p.drawText(r.adjusted(6, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, c.name)
            # header (drawn last so clips scroll underneath)
            p.fillRect(QRectF(0, y, self.HEAD, h), QColor(C["panel"]))
            p.setPen(QColor(C["line"]))
            p.drawLine(0, y + h, self.HEAD, y + h)
            tagc = QColor(col)
            tagc.setAlpha(50)
            p.setBrush(tagc)
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRectF(8, y + h / 2 - 9, 34, 18), 3, 3)
            p.setPen(col)
            p.setFont(QFont("Consolas", 8, QFont.Bold))
            p.drawText(QRectF(8, y + h / 2 - 9, 34, 18), Qt.AlignCenter, tag)
            p.setPen(QColor(C["muted"]))
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(QRectF(48, y, self.HEAD - 52, h), Qt.AlignVCenter, p.fontMetrics().elidedText(l.name, Qt.ElideRight, self.HEAD - 54))
            y += h
        p.fillRect(QRectF(0, 0, self.HEAD, self.RULER), QColor(C["panel"]))
        p.setPen(QColor(C["line2"]))
        p.drawLine(self.HEAD, 0, self.HEAD, self.height())
        ph = self.win.playhead
        if ph is not None and self.rows():
            x = self.HEAD + (ph - lo) * self.pps
            if x >= self.HEAD:
                p.setPen(QPen(QColor(C["accent"]), 2))
                p.drawLine(int(x), 0, int(x), self.height())
                p.setBrush(QColor(C["accent"]))
                p.setPen(Qt.NoPen)
                p.drawPolygon([QPointF(x - 6, 0), QPointF(x + 6, 0), QPointF(x, 9)])
        if not self.rows():
            p.setPen(QColor(C["dim"]))
            p.setFont(QFont("Segoe UI", 10))
            p.drawText(self.rect(), Qt.AlignCenter, "Drop camera folders and your Zoom card folder here")
        p.end()

    def x_to_time(self, x: float) -> float:
        lo, length = self.span()
        return lo + min(max(0.0, (x - self.HEAD) / self.pps), length)

    def mousePressEvent(self, e):
        x, y = e.position().x(), e.position().y()
        if e.button() != Qt.LeftButton or x <= self.HEAD or not self.rows():
            return
        _l, c = self.hit(x, y) if y > self.RULER else (None, None)
        if c is not None:
            self.win.select_clip(c)
            group = self.win.selected_group()
            self.drag = (group, [g.start for g in group], x, False)
            return
        self.win.select_clip(None)
        self.drag = None
        self.win.seek(self.x_to_time(x))

    def mouseMoveEvent(self, e):
        x = e.position().x()
        if not (e.buttons() & Qt.LeftButton) or not self.rows():
            return
        if self.drag is not None:
            group, starts, x0, _moved = self.drag
            fps = self.win.current_fps()
            frames = round((x - x0) / self.pps * fps)
            if frames or _moved:
                for g, s0 in zip(group, starts):
                    g.start = s0 + frames / fps
                self.drag = (group, starts, x0, True)
                self.win.say(f"Moving {group[0].name}: {frames:+d} frames")
                self.update()
            return
        if x > self.HEAD:
            self.win.seek(self.x_to_time(x), scrub=True)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or not self.rows():
            return
        if self.drag is not None:
            group, starts, _x0, moved = self.drag
            self.drag = None
            if moved:
                self.win.clips_moved(group, starts)
            return
        self.win.seek_done()

    def _wave(self, p: QPainter, r: QRectF, c, vis):
        """The clip's real waveform, decoded in the background the first time it's shown."""
        pk = self.win.waves.get(c)
        if pk is None:
            return
        x0, cols = columns(pk, c.duration, max(r.left(), vis.left()), min(r.right(), vis.right() + 1), self.pps, r.left())
        if not len(cols):
            return
        wc = QColor(C["audio"])
        wc.setAlpha(150)
        p.setPen(QPen(wc, 1))
        mid = r.center().y()
        half = r.height() / 2 - 2
        amp = np.sqrt(np.clip(cols, 0, 1)) * half      # sqrt so quiet speech is still visible
        lines = [QLineF(x0 + i, mid - a, x0 + i, mid + a) for i, a in enumerate(amp) if a >= 0.5]
        if lines:
            p.drawLines(lines)

    # ---------- clip picking, dragging and nudging
    def hit(self, x: float, y: float):
        lo, _ = self.span()
        yy = self.RULER
        for l in self.rows():
            h = self.VH if l.kind == "video" else self.AH
            if yy <= y < yy + h:
                t = lo + (x - self.HEAD) / self.pps
                for c in l.clips:
                    if c.start is not None and c.start <= t < c.start + c.duration:
                        return l, c
                return l, None
            yy += h
        return None, None


class _Signals(QObject):
    waves_ready = Signal()
    update_found = Signal(object)
    update_progress = Signal(float)
    update_ready = Signal(str)
    update_failed = Signal(str)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.project = Project()
        self.project_path: str | None = None
        self.selected = None            # the clip picked on the timeline
        self.undo_stack: list[list[tuple]] = []
        self.settings = QSettings("Endora", "MultiSync")
        self.thread: QThread | None = None
        self.playhead: float | None = None
        self.mode = self.settings.value("mode", "tcv")
        self.setWindowTitle(APP_NAME)
        self.resize(1400, 880)
        self.setAcceptDrops(True)
        cache = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache"), "EndoraMultiSync", "cache")
        os.makedirs(cache, exist_ok=True)
        sync_mod.CACHE_DIR = cache
        self.drift_dir = os.path.join(os.path.dirname(cache), "drift fixed")
        self._sig = _Signals()
        self.waves = WaveCache(cache, self._sig.waves_ready.emit)

        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._topbar())

        body = QSplitter(Qt.Vertical)
        body.setContentsMargins(10, 10, 10, 10)
        top = QSplitter(Qt.Horizontal)
        top.addWidget(self._media_panel())
        top.addWidget(self._report_panel())
        top.addWidget(self._viewer_panel())
        top.setSizes([460, 520, 460])
        body.addWidget(top)
        body.addWidget(self._timeline_panel())
        body.setSizes([470, 360])
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(10, 10, 10, 6)
        wl.addWidget(body)
        outer.addWidget(wrap, 1)

        foot = QHBoxLayout()
        foot.setContentsMargins(16, 0, 16, 10)
        self.status = QLabel("Ready. Add your camera folders and Zoom card, then press Sync.", objectName="muted")
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedWidth(260)
        self.bar.setRange(0, 1000)
        foot.addWidget(self.status, 1)
        foot.addWidget(self.bar)
        outer.addLayout(foot)
        self.player = Player()
        self.player.frame.connect(self._show_frame, Qt.QueuedConnection)
        self.player.ended.connect(self._play_ended, Qt.QueuedConnection)
        self.play_timer = QTimer(self)
        self.play_timer.setInterval(40)
        self.play_timer.timeout.connect(self._tick)
        self._scrubbing = False
        QShortcut(QKeySequence(Qt.Key_Space), self, activated=self.toggle_play)
        QShortcut(QKeySequence("Alt+Right"), self, activated=lambda: self.nudge(1))
        QShortcut(QKeySequence("Alt+Left"), self, activated=lambda: self.nudge(-1))
        QShortcut(QKeySequence("Alt+Shift+Right"), self, activated=lambda: self.nudge(10))
        QShortcut(QKeySequence("Alt+Shift+Left"), self, activated=lambda: self.nudge(-10))
        QShortcut(QKeySequence("."), self, activated=lambda: self.nudge(1))
        QShortcut(QKeySequence(","), self, activated=lambda: self.nudge(-1))
        QShortcut(QKeySequence.Undo, self, activated=self.undo_move)
        QShortcut(QKeySequence.Save, self, activated=self.save_project)
        QShortcut(QKeySequence.Open, self, activated=self.open_project)
        self._sig.waves_ready.connect(self.timeline.update, Qt.QueuedConnection)
        self._sig.update_found.connect(self._offer_update, Qt.QueuedConnection)
        self._sig.update_progress.connect(lambda f: self.bar.setValue(int(f * 1000)), Qt.QueuedConnection)
        self._sig.update_ready.connect(self._install_update, Qt.QueuedConnection)
        self._sig.update_failed.connect(self._update_failed, Qt.QueuedConnection)
        self.refresh()
        if getattr(sys, "frozen", False) or os.environ.get("ENDORA_UPDATE_API"):
            QTimer.singleShot(4000, self.check_update)

    # ---------- UI building ----------
    def _topbar(self):
        f = QFrame(objectName="topbar")
        h = QHBoxLayout(f)
        h.setContentsMargins(16, 10, 16, 10)
        h.setSpacing(14)
        logo = QLabel(f"ENDORA <span style='color:{C['accent']}'>MultiSync</span>", objectName="logo")
        logo.setTextFormat(Qt.RichText)
        h.addWidget(logo)
        h.addWidget(QLabel(f"v{VERSION}", objectName="muted"))
        ob = QPushButton("Open")
        ob.setToolTip("Open a saved sync project (Ctrl+O)")
        ob.clicked.connect(self.open_project)
        sb = QPushButton("Save")
        sb.setToolTip("Save this sync project (Ctrl+S)")
        sb.clicked.connect(self.save_project)
        h.addWidget(ob)
        h.addWidget(sb)
        h.addStretch(1)
        seg = QFrame(objectName="segwrap")
        sl = QHBoxLayout(seg)
        sl.setContentsMargins(3, 3, 3, 3)
        sl.setSpacing(2)
        self.mode_group = QButtonGroup(self)
        for key, label in MODES:
            b = QPushButton(label, objectName="seg")
            b.setCheckable(True)
            b.setChecked(key == self.mode)
            b.clicked.connect(lambda _=False, k=key: self.set_mode(k))
            self.mode_group.addButton(b)
            sl.addWidget(b)
        h.addWidget(seg)
        h.addWidget(QLabel("Timeline fps", objectName="muted"))
        self.fps_box = QComboBox()
        self.fps_box.addItems(["Auto", "23.976", "24", "25", "29.97", "30", "50", "59.94", "60"])
        self.fps_box.setCurrentText(self.settings.value("fps", "Auto"))
        self.fps_box.currentTextChanged.connect(lambda t: (self.settings.setValue("fps", t), self.refresh()))
        h.addWidget(self.fps_box)
        h.addWidget(QLabel("Events", objectName="muted"))
        self.events_box = QComboBox()
        self.events_box.addItems(["One timeline", "Split at 10 min gaps", "Split at 20 min gaps",
                                  "Split at 30 min gaps", "Split at 60 min gaps"])
        self.events_box.setToolTip("Make a separate timeline for each part of the day (Poruwa, reception, ...)\n"
                                   "wherever nothing was recording for this long.")
        self.events_box.setCurrentIndex(int(self.settings.value("events", 0)))
        self.events_box.currentIndexChanged.connect(lambda i: (self.settings.setValue("events", i), self.refresh()))
        h.addWidget(self.events_box)
        self.sync_btn = QPushButton("Sync all angles", objectName="primary")
        self.sync_btn.clicked.connect(self.do_sync)
        h.addWidget(self.sync_btn)
        self.export_btn = QPushButton("Export for Premiere")
        self.export_btn.clicked.connect(self.do_export_premiere)
        h.addWidget(self.export_btn)
        self.export_dv_btn = QPushButton("Export for DaVinci")
        self.export_dv_btn.clicked.connect(self.do_export_resolve)
        h.addWidget(self.export_dv_btn)
        return f

    def _panel(self, title):
        f = QFrame(objectName="panel")
        v = QVBoxLayout(f)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(12, 10, 12, 8)
        lab = QLabel(title.upper(), objectName="h2")
        head.addWidget(lab)
        head.addStretch(1)
        v.addLayout(head)
        return f, v, head

    def _media_panel(self):
        f, v, head = self._panel("Media")
        self.counts = QLabel("", objectName="muted")
        head.addWidget(self.counts)
        row = QHBoxLayout()
        row.setContentsMargins(10, 0, 10, 8)
        row.setSpacing(6)
        for text, fn in [("+ Camera folder", self.add_camera_folder), ("+ Camera files", self.add_camera_files),
                         ("+ Zoom / recorder folder", self.add_recorder_folder), ("+ Audio files", self.add_audio_files)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        v.addLayout(row)
        row2 = QHBoxLayout()
        row2.setContentsMargins(12, 0, 10, 8)
        self.cam_audio = QCheckBox("Camera sound on its own layer")
        self.cam_audio.setChecked(self.settings.value("camaudio", "true") == "true")
        self.project.camera_audio = self.cam_audio.isChecked()
        self.cam_audio.toggled.connect(self.toggle_cam_audio)
        row2.addWidget(self.cam_audio)
        self.drift_box = QCheckBox("Fix Zoom clock drift")
        self.drift_box.setToolTip("Long recorder files that run slightly fast or slow get a corrected copy\n"
                                  "(same pitch, same sample rate) in an 'Endora drift fixed' folder next to them.")
        self.drift_box.setChecked(self.settings.value("drift", "true") == "true")
        self.drift_box.toggled.connect(lambda on: self.settings.setValue("drift", "true" if on else "false"))
        row2.addWidget(self.drift_box)
        row2.addStretch(1)
        rm = QPushButton("Remove layer")
        rm.clicked.connect(self.remove_layer)
        clr = QPushButton("Clear all")
        clr.clicked.connect(self.clear_all)
        row2.addWidget(rm)
        row2.addWidget(clr)
        v.addLayout(row2)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Layer / file", "Length", "Timecode", "Channels"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        v.addWidget(self.tree, 1)
        return f

    def _report_panel(self):
        f, v, head = self._panel("Sync check")
        self.sum_labels = {}
        sums = QHBoxLayout()
        sums.setContentsMargins(12, 0, 12, 8)
        sums.setSpacing(24)
        for key, text in [("ok", "Locked"), ("warn", "Corrected"), ("bad", "Needs you")]:
            box = QVBoxLayout()
            n = QLabel("0", objectName="big")
            n.setStyleSheet(f"color:{STATUS_COLOR[key] if key != 'ok' else C['fg']}")
            box.addWidget(n)
            box.addWidget(QLabel(text.upper(), objectName="h2"))
            self.sum_labels[key] = n
            sums.addLayout(box)
        sums.addStretch(1)
        v.addLayout(sums)
        self.mode_note = QLabel("", objectName="muted")
        self.mode_note.setWordWrap(True)
        self.mode_note.setContentsMargins(12, 0, 12, 8)
        v.addWidget(self.mode_note)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Layer", "File", "Result", "Placed at", "Confidence", "What happened"])
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        v.addWidget(self.table, 1)
        return f

    def _viewer_panel(self):
        f, v, head = self._panel("Viewer")
        self.screen = QLabel()
        self.screen.setAlignment(Qt.AlignCenter)
        self.screen.setMinimumSize(320, 180)
        self.screen.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.screen.setStyleSheet("background:#000; border-radius:4px;")
        v.addWidget(self.screen, 1)
        self.caption = QLabel("Sync, then press Play or click the timeline", objectName="muted")
        cap = QHBoxLayout()
        cap.setContentsMargins(12, 6, 12, 0)
        cap.addWidget(self.caption, 1)
        self.tc_label = QLabel("00:00:00:00", objectName="big")
        cap.addWidget(self.tc_label)
        v.addLayout(cap)
        row = QHBoxLayout()
        row.setContentsMargins(12, 6, 12, 10)
        self.home_btn = QPushButton("|◀ Start")
        self.home_btn.setToolTip("Go to the start")
        self.home_btn.clicked.connect(lambda: self.seek(self.timeline.span()[0]))
        self.play_btn = QPushButton("▶  Play", objectName="primary")
        self.play_btn.setToolTip("Play / pause (Space)")
        self.play_btn.clicked.connect(self.toggle_play)
        row.addWidget(self.home_btn)
        row.addWidget(self.play_btn)
        row.addSpacing(8)
        row.addWidget(QLabel("Picture", objectName="muted"))
        self.pic_box = QComboBox()
        self.pic_box.currentIndexChanged.connect(lambda _i: self._restart_if_playing())
        row.addWidget(self.pic_box, 1)
        row.addWidget(QLabel("Sound", objectName="muted"))
        self.snd_box = QComboBox()
        self.snd_box.addItems(["Zoom + camera", "Zoom only", "Camera only"])
        self.snd_box.setToolTip("Zoom + camera: if they are in sync you hear one clean sound, if not you hear an echo")
        self.snd_box.currentIndexChanged.connect(lambda _i: self._restart_if_playing())
        row.addWidget(self.snd_box)
        v.addLayout(row)
        return f

    def _timeline_panel(self):
        f, v, head = self._panel("Timeline")
        head.addWidget(QLabel("Zoom", objectName="muted"))
        self.zoom = QSlider(Qt.Horizontal)
        self.zoom.setRange(1, 60)
        self.zoom.setValue(8)
        self.zoom.setFixedWidth(160)
        self.zoom.valueChanged.connect(self.set_zoom)
        head.addWidget(self.zoom)
        self.timeline = Timeline(self)
        sa = QScrollArea()
        self.timeline_scroll = sa
        sa.setWidget(self.timeline)
        sa.setWidgetResizable(True)
        v.addWidget(sa, 1)
        self.set_zoom(8)
        return f

    # ---------- events ----------
    def event_gap(self) -> float:
        return [0, 10, 20, 30, 60][self.events_box.currentIndex()] if hasattr(self, "events_box") else 0

    def events(self) -> list[tuple[float, float]]:
        return split_events(self.project.export_layers(), self.event_gap())

    def export_events(self, name: str) -> list[tuple[str, list[Layer]]]:
        layers = self.project.export_layers()
        evs = self.events()
        if len(evs) <= 1:
            return [(name, layers)]
        fps = self.current_fps()
        return [(f"{name} - Event {i + 1} ({tc_string(s, fps)[:5].replace(':', 'h')})", event_layers(layers, s, e))
                for i, (s, e) in enumerate(evs)]

    # ---------- manual nudge ----------
    def selected_group(self) -> list:
        """The picked clip plus everything that must move with it: the same file on other layers
        (camera sound, L/R) and the other files of a recorder take."""
        c = self.selected
        if c is None:
            return []
        out = []
        for l in self.project.layers:
            for o in l.clips:
                if o is c or o.path == c.path or (c.take and o.take == c.take):
                    out.append(o)
        return out

    def select_clip(self, c):
        self.selected = c
        if c is not None:
            fps = self.current_fps()
            self.say(f"Selected {c.name} at {tc_string(c.start, fps)}. Drag it, or nudge with , and . "
                     f"(Alt+Shift+arrows = 10 frames). Ctrl+Z undoes.")
        self.timeline.update()

    def clips_moved(self, group: list, old_starts: list):
        self.undo_stack.append(list(zip(group, old_starts)))
        fps = self.current_fps()
        frames = round((group[0].start - old_starts[0]) * fps)
        for g in group:
            g.method, g.status, g.confidence = "Manual", "ok", 1.0
            g.note = f"Moved by hand ({frames:+d} frames from where sync put it)" if not g.note.startswith("Moved") \
                else f"Moved by hand ({frames:+d} frames)"
        self.refresh()
        self.say(f"Moved {group[0].name} {frames:+d} frames. Ctrl+Z undoes.")

    def nudge(self, frames: int):
        group = self.selected_group()
        if not group or any(g.start is None for g in group):
            self.say("Click a clip on the timeline first, then nudge it with , and .")
            return
        fps = self.current_fps()
        old = [g.start for g in group]
        for g in group:
            g.start += frames / fps
        self.clips_moved(group, old)
        if self.player.playing:
            self._restart_if_playing()

    def undo_move(self):
        if not self.undo_stack:
            self.say("Nothing to undo.")
            return
        for c, s0 in self.undo_stack.pop():
            c.start = s0
        self.refresh()
        self.say("Undone.")

    # ---------- save / open ----------
    def save_project(self, ask: bool = False):
        path = self.project_path
        if not path or ask:
            default = os.path.join(self._last_dir(), "Wedding" + PROJECT_EXT)
            path, _ = QFileDialog.getSaveFileName(self, "Save sync project", default, f"Endora project (*{PROJECT_EXT})")
            if not path:
                return
            if not path.lower().endswith(PROJECT_EXT):
                path += PROJECT_EXT
        try:
            save_project(self.project, path, {"mode": self.mode, "fps": self.fps_box.currentText(),
                                              "events": self.events_box.currentIndex()})
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Couldn't save: {e}")
            return
        self.project_path = path
        self._remember(path)
        self.setWindowTitle(f"{APP_NAME} · {os.path.basename(path)}")
        self.say(f"Saved {path}")

    def open_project(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open sync project", self._last_dir(), f"Endora project (*{PROJECT_EXT})")
        if not path:
            return
        try:
            proj, st, missing = load_project(path)
        except (OSError, ValueError, KeyError, TypeError) as e:
            QMessageBox.warning(self, APP_NAME, f"Couldn't open that project: {e}")
            return
        if self.player.playing:
            self.toggle_play()
        self.project = proj
        self.project_path = path
        self.selected, self.undo_stack, self.playhead = None, [], None
        if st.get("mode") in ("audio", "tc", "tcv"):
            self.mode = st["mode"]
            for b, (key, _lbl) in zip(self.mode_group.buttons(), MODES):
                b.setChecked(key == self.mode)
        if st.get("fps"):
            self.fps_box.setCurrentText(st["fps"])
        if isinstance(st.get("events"), int):
            self.events_box.setCurrentIndex(st["events"])
        self.cam_audio.blockSignals(True)
        self.cam_audio.setChecked(proj.camera_audio)
        self.cam_audio.blockSignals(False)
        self._remember(path)
        self.setWindowTitle(f"{APP_NAME} · {os.path.basename(path)}")
        self.refresh()
        msg = f"Opened {os.path.basename(path)}"
        if missing:
            msg += f" · {len(missing)} file(s) are missing (moved or drive not connected)"
            QMessageBox.warning(self, APP_NAME, "These files can't be found. Connect the drive or put them back:\n\n"
                                + "\n".join(missing[:15]) + ("\n…" if len(missing) > 15 else ""))
        self.say(msg)

    # ---------- playback ----------
    def _video_layers(self) -> list[Layer]:
        vids = [l for l in self.project.layers if l.kind == "video"]
        i = self.pic_box.currentIndex()
        if i <= 0 or i > len(vids):
            return vids                      # Auto: first camera that has a clip
        return [vids[i - 1]]

    def _sound(self, t0: float) -> list:
        mode = self.snd_box.currentIndex()
        out = []
        if mode in (0, 1):
            seen = set()
            recs = [l for l in self.project.layers if l.kind == "audio" and l.source == "recorder"]
            for l in recs:
                for c in l.clips:
                    key = (c.path, c.channel if c.channels > 2 else 0)
                    if key not in seen:
                        seen.add(key)
                        out.append(c)
        if mode in (0, 2):
            vids = self._video_layers()
            lay, _c = clip_at(vids, t0)
            lay = lay or (vids[0] if vids else None)
            if lay is not None:
                out += [c for c in lay.clips if c.channels]
        if not out:
            return []
        # files playing at the same time share the level so the mix doesn't clip
        g = 1.0 / max(1.0, len({(c.path) for c in out if c.start is not None and c.start <= t0 + 60}) ** 0.5)
        return [(c, g) for c in out]

    def _synced(self) -> bool:
        return any(c.start is not None for l in self.project.layers for c in l.clips)

    def toggle_play(self):
        if not self._synced():
            self.say("Sync first, then you can play the timeline.")
            return
        if self.player.playing:
            self.playhead = self.player.stop()
            self.play_timer.stop()
            self.play_btn.setText("▶  Play")
            self._tick()
            return
        lo, length = self.timeline.span()
        if self.playhead is None or self.playhead >= lo + length - 0.1 or self.playhead < lo:
            self.playhead = lo
        self.player.play(self.playhead, self._video_layers(), self._sound(self.playhead), lo + length,
                         self.current_fps())
        self.play_btn.setText("II  Pause")
        self.play_timer.start()

    def _restart_if_playing(self):
        if self.player.playing:
            self.toggle_play()
            self.toggle_play()
        elif self.playhead is not None:
            self._still()

    def _play_ended(self):
        if self.player.playing:
            lo, length = self.timeline.span()
            self.playhead = self.player.stop()
            self.play_timer.stop()
            self.play_btn.setText("▶  Play")
            if self.playhead < lo + length - 0.5:     # a long timeline plays in parts: carry on
                self.toggle_play()

    def _tick(self):
        if self.player.playing:
            self.playhead = self.player.clock()
        if self.playhead is None:
            return
        self.tc_label.setText(tc_string(self.playhead, self.current_fps()))
        self.timeline.update()
        x = int(self.timeline.HEAD + (self.playhead - self.timeline.span()[0]) * self.timeline.pps)
        self.timeline_scroll.ensureVisible(x, 0, 80, 0)

    def seek(self, t: float, scrub: bool = False):
        self.playhead = t
        if self.player.playing and not scrub:
            self.player.stop()
            self.toggle_play()
        elif self.player.playing and scrub:
            self._scrubbing = True
            self.player.stop()
            self.play_timer.stop()
            self.play_btn.setText("▶  Play")
        self._tick()
        if not self.player.playing and not scrub:
            self._still()

    def seek_done(self):
        if self._scrubbing:
            self._scrubbing = False
            self.toggle_play()
        elif not self.player.playing:
            self._still()

    def _still(self):
        t = self.playhead
        lay, c = clip_at(self._video_layers(), t) if t is not None else (None, None)
        if c is None:
            self._show_frame(QImage(), "No camera here")
            return
        cap = f"{lay.name} · {c.name}"

        def grab():
            img = read_frame(c.path, t - c.start)
            if img is not None and not self.player.playing and self.playhead == t:
                self.player.frame.emit(img, cap)
        import threading
        threading.Thread(target=grab, daemon=True).start()

    def _show_frame(self, img: QImage, caption: str):
        self.caption.setText(caption)
        if img.isNull():
            self.screen.clear()
            return
        pm = QPixmap.fromImage(img).scaled(self.screen.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.screen.setPixmap(pm)

    # ---------- auto update ----------
    def check_update(self):
        def work():
            try:
                info = updater.latest()
            except Exception:  # noqa: BLE001 - offline or GitHub unreachable: try next start
                return
            if info:
                self._sig.update_found.emit(info)
        threading.Thread(target=work, daemon=True).start()

    def _offer_update(self, info):
        notes = info["notes"][:600] or "Fixes and improvements."
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setIcon(QMessageBox.Information)
        box.setText(f"Endora MultiSync {info['version']} is ready (you have {VERSION}).")
        box.setInformativeText(f"What's new:\n{notes}\n\nThe app will close, update and open again. "
                               "Save your project first if you need it.")
        now = box.addButton("Update now", QMessageBox.AcceptRole)
        box.addButton("Later", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not now:
            return
        self.say(f"Downloading update {info['version']}...")

        def work():
            try:
                self._sig.update_ready.emit(updater.download(info, self._sig.update_progress.emit))
            except Exception as e:  # noqa: BLE001 - shown to the user
                self._sig.update_failed.emit(str(e) or e.__class__.__name__)
        threading.Thread(target=work, daemon=True).start()

    def _install_update(self, path):
        self.say("Installing update...")
        try:
            updater.install(path)
        except OSError as e:
            self._update_failed(str(e))
            return
        self.close()
        QApplication.quit()

    def _update_failed(self, msg):
        self.bar.setValue(0)
        self.say(f"Update failed: {msg}")

    def closeEvent(self, e):
        self.player.stop()
        super().closeEvent(e)

    # ---------- state ----------
    def current_fps(self) -> float:
        t = self.fps_box.currentText()
        if t == "Auto":
            return pick_fps(self.project.layers)
        return {"23.976": 24000 / 1001, "29.97": 30000 / 1001, "59.94": 60000 / 1001}.get(t, None) or float(t)

    def set_mode(self, key):
        self.mode = key
        self.settings.setValue("mode", key)
        for l in self.project.layers:
            for c in l.clips:
                c.start, c.status, c.confidence, c.method, c.note = None, "wait", 0.0, "", ""
        self.refresh()
        self.say("Method changed. Press Sync all angles to run it.")

    def set_zoom(self, v):
        self.timeline.pps = v / 2.0
        self.timeline.refresh()

    def toggle_cam_audio(self, on):
        self.project.camera_audio = on
        if len(self.project.audio_layers) > MAX_AUDIO_LAYERS:
            self.project.camera_audio = False
            self.cam_audio.blockSignals(True)
            self.cam_audio.setChecked(False)
            self.cam_audio.blockSignals(False)
            QMessageBox.warning(self, APP_NAME, f"Camera sound would go over {MAX_AUDIO_LAYERS} audio layers.")
        self.settings.setValue("camaudio", "true" if self.project.camera_audio else "false")
        self.refresh()

    def say(self, msg):
        self.status.setText(msg)

    def refresh(self):
        notes = {
            "audio": "Every clip's sound is matched against the main recorder and the other cameras. Works with any camera that recorded sound.",
            "tc": "Clips are placed by their embedded timecode. Fast, but clips without timecode can't be placed.",
            "tcv": "Placed by timecode, then every clip's audio is checked against the others. Clocks that drifted are fixed to the frame, and clips without timecode are placed by audio.",
        }
        self.mode_note.setText(notes[self.mode])
        names = ["Auto (first camera with a clip)"] + [l.name for l in self.project.layers if l.kind == "video"]
        if [self.pic_box.itemText(i) for i in range(self.pic_box.count())] != names:
            keep = self.pic_box.currentText()
            self.pic_box.blockSignals(True)
            self.pic_box.clear()
            self.pic_box.addItems(names)
            self.pic_box.setCurrentIndex(max(0, names.index(keep)) if keep in names else 0)
            self.pic_box.blockSignals(False)
        self.play_btn.setEnabled(self._synced())
        fps = self.current_fps()
        # media tree
        self.tree.clear()
        vi = ai = 0
        for l in self.project.export_layers():
            if l.kind == "video":
                vi += 1
                tag = f"V{vi}"
            else:
                ai += 1
                tag = f"A{ai}"
            it = QTreeWidgetItem([f"{tag}  {l.name}", f"{len(l.clips)} file(s)", "", ""])
            it.setForeground(0, QColor(C["video"] if l.kind == "video" else C["audio"]))
            it.setData(0, Qt.UserRole, id(l))
            for c in l.clips:
                tcs = c.tc_string if c.tc_string and c.tc_string != "BWF" else (tc_string(c.tc_start, fps) if c.tc_start is not None else "none")
                ch = f"{c.channel + 1} of {c.channels}" if l.kind == "audio" else (f"{c.width}x{c.height} {c.fps or 0:.3g}p")
                child = QTreeWidgetItem([c.name, f"{int(c.duration // 60)}m {int(c.duration % 60):02d}s", tcs, ch])
                child.setToolTip(0, c.path)
                it.addChild(child)
            self.tree.addTopLevelItem(it)
        self.counts.setText(f"Video {len(self.project.video_layers)}/{MAX_VIDEO_LAYERS} · Audio {len(self.project.audio_layers)}/{MAX_AUDIO_LAYERS}")
        # report
        rows = [(l, c) for l in self.project.export_layers() if l.source != "camera-audio" for c in l.clips]
        self.table.setRowCount(len(rows))
        counts = {"ok": 0, "warn": 0, "bad": 0}
        for r, (l, c) in enumerate(rows):
            if c.status in counts:
                counts[c.status] += 1
            vals = [l.name, c.name, STATUS_TEXT.get(c.status, c.status), tc_string(c.start, fps),
                    "—" if c.status == "wait" else f"{c.confidence * 100:.0f}%",
                    (f"{c.method}: " if c.method else "") + (c.note or "Not synced yet")]
            for k, val in enumerate(vals):
                item = QTableWidgetItem(val)
                if k == 2:
                    item.setForeground(QColor(STATUS_COLOR.get(c.status, C["dim"])))
                    item.setFont(QFont("Segoe UI", 9, QFont.Bold))
                if k in (3, 4):
                    item.setFont(QFont("Consolas", 9))
                self.table.setItem(r, k, item)
        self.table.resizeColumnsToContents()
        for k, n in counts.items():
            self.sum_labels[k].setText(str(n))
        self.timeline.refresh()
        busy = self.thread is not None
        self.sync_btn.setEnabled(not busy and bool(self.project.layers))
        can_export = not busy and any(c.start is not None for l in self.project.layers for c in l.clips)
        self.export_btn.setEnabled(can_export)
        self.export_dv_btn.setEnabled(can_export)

    # ---------- background jobs ----------
    def run_job(self, fn, on_done, label):
        if self.thread is not None:
            return
        self.say(label)
        self.bar.setValue(0)
        self.thread = QThread()
        self.worker = Worker(fn)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.message.connect(self.say, Qt.QueuedConnection)
        self.worker.progress.connect(self._on_progress, Qt.QueuedConnection)
        self._on_done = on_done
        self.worker.done.connect(self._finish, Qt.QueuedConnection)
        self.worker.failed.connect(self._failed, Qt.QueuedConnection)
        self.thread.start()
        self.refresh()

    def _stop_thread(self):
        if self.thread:
            self.thread.quit()
            self.thread.wait()
            self.thread = None

    def _on_progress(self, f):
        self.bar.setValue(int(f * 1000))

    def _finish(self, res):
        self._stop_thread()
        self.bar.setValue(1000)
        self._on_done(res)
        self.refresh()

    def _failed(self, msg):
        self._stop_thread()
        self.bar.setValue(0)
        self.refresh()
        self.say(msg)
        QMessageBox.warning(self, APP_NAME, msg)

    # ---------- adding media ----------
    def _last_dir(self):
        return self.settings.value("lastdir", os.path.expanduser("~"))

    def _remember(self, path):
        self.settings.setValue("lastdir", os.path.dirname(path) if os.path.isfile(path) else path)

    def add_camera_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Choose one camera's folder", self._last_dir())
        if d:
            self._remember(d)
            self.add_folders([d], kind="camera")

    def add_camera_files(self):
        exts = " ".join("*" + e for e in sorted(VIDEO_EXT))
        files, _ = QFileDialog.getOpenFileNames(self, "Choose clips from ONE camera", self._last_dir(), f"Video ({exts})")
        if files:
            self._remember(files[0])
            name = os.path.basename(os.path.dirname(files[0]))
            self.run_job(lambda w: self.project.add_camera(files, name, w.message.emit),
                         lambda _l: self.say(f"Added camera {name}."), "Reading camera clips…")

    def add_recorder_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Choose the Zoom / recorder folder (the whole card is fine)", self._last_dir())
        if d:
            self._remember(d)
            self.add_folders([d], kind="recorder")

    def add_audio_files(self):
        exts = " ".join("*" + e for e in sorted(AUDIO_EXT))
        files, _ = QFileDialog.getOpenFileNames(self, "Choose audio files", self._last_dir(), f"Audio ({exts})")
        if files:
            self._remember(files[0])
            name = os.path.basename(os.path.dirname(files[0])) or "Audio"
            self.run_job(lambda w: self.project.add_recorder(files, name, w.message.emit),
                         lambda ls: self.say(f"Added {len(ls)} audio layer(s)."), "Reading audio files…")

    def add_folders(self, folders, kind=None):
        def job(w):
            added = []
            for d in folders:
                k = kind
                if k is None:
                    k = "camera" if list_media(d, VIDEO_EXT) else "recorder"
                if k == "camera":
                    added.append(self.project.add_camera_folder(d, w.message.emit).name)
                else:
                    added += [l.name for l in self.project.add_recorder_folder(d, w.message.emit)]
            return added
        self.run_job(job, lambda names: self.say(f"Added {', '.join(names)}"), "Reading media…")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls()]
        folders = [p for p in paths if os.path.isdir(p)]
        files = [p for p in paths if os.path.isfile(p)]
        if folders:
            self.add_folders(folders)
        elif files:
            vids = [f for f in files if os.path.splitext(f)[1].lower() in VIDEO_EXT]
            auds = [f for f in files if os.path.splitext(f)[1].lower() in AUDIO_EXT]
            if vids:
                name = os.path.basename(os.path.dirname(vids[0]))
                self.run_job(lambda w: self.project.add_camera(vids, name, w.message.emit), lambda _l: None, "Reading clips…")
            elif auds:
                name = os.path.basename(os.path.dirname(auds[0]))
                self.run_job(lambda w: self.project.add_recorder(auds, name, w.message.emit), lambda _l: None, "Reading audio…")

    def selected_layer(self) -> Layer | None:
        it = self.tree.currentItem()
        if it is None:
            return None
        while it.parent():
            it = it.parent()
        lid = it.data(0, Qt.UserRole)
        return next((l for l in self.project.layers if id(l) == lid), None)

    def remove_layer(self):
        l = self.selected_layer()
        if l:
            self.project.remove(l)
            self.refresh()

    def clear_all(self):
        if self.thread is None:
            self.project.layers.clear()
            self.selected, self.undo_stack, self.playhead = None, [], None
            self.refresh()

    # ---------- sync + export ----------
    def do_sync(self):
        if self.player.playing:
            self.toggle_play()
        self.selected, self.undo_stack = None, []
        def job(w):
            return sync_mod.run_sync(self.project.layers, self.mode, w.message.emit, w.progress.emit,
                                     fix_drift=self.drift_box.isChecked(), drift_dir=self.drift_dir)

        def done(_units):
            clips = [c for l in self.project.layers if l.source != "camera-audio" for c in l.clips]
            bad = sum(c.status == "bad" for c in clips)
            warn = sum(c.status == "warn" for c in clips)
            msg = f"Synced {len(clips) - bad} of {len(clips)} files"
            if warn:
                msg += f" · {warn} corrected by audio"
            if bad:
                msg += f" · {bad} couldn't sync, kept on the timeline in order and marked red"
            drifted = {c.original_path for c in clips if c.original_path}
            if drifted:
                msg += f" · {len(drifted)} Zoom file(s) drift-corrected"
            self.say(msg)
        self.run_job(job, done, "Syncing… listening to every clip")

    def _save_name(self, title, suffix, filt):
        default = os.path.join(self._last_dir(), f"Endora_MultiSync_{suffix}")
        path, _ = QFileDialog.getSaveFileName(self, title, default, filt)
        return path

    @staticmethod
    def _left_out(info):
        if not info.get("warnings"):
            return ""
        return "\n\nCheck:\n" + "\n".join(info["warnings"][:12])

    def do_export_premiere(self):
        path = self._save_name("Save Premiere Pro XML", "Premiere.xml", "Premiere Pro XML (*.xml)")
        if not path:
            return
        base = path[:-4] if path.lower().endswith(".xml") else path
        name = os.path.basename(base)
        saved, warns = [], []
        try:
            for ev_name, ls in self.export_events(name):
                stem = os.path.join(os.path.dirname(base), ev_name)
                info = export_premiere(ls, stem + ".xml", ev_name, self.current_fps())
                export_premiere(ls, stem + " - Multicam.xml", ev_name, self.current_fps(), multicam=True)
                saved += [os.path.basename(stem) + ".xml", os.path.basename(stem) + " - Multicam.xml"]
                warns += info["warnings"]
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        QMessageBox.information(
            self, APP_NAME,
            "Saved:\n" + "\n".join(saved) + "\n\n"
            "In Premiere Pro: File > Import… and pick an XML (don't use File > Open).\n\n"
            "• The plain XML is the synced timeline: every camera on its own track, Zoom inputs below.\n"
            "• The Multicam XML gives a ready edit sequence: the synced cameras nested on V1 with the Zoom "
            "sound underneath. Right-click the nested clip > Multi-Camera > Enable, then open the "
            "Multi-Camera view and cut between angles while it plays."
            + self._left_out({"warnings": warns}))
        self.say(f"Exported {len(saved)} file(s) to {os.path.dirname(base)}")

    def do_export_resolve(self):
        path = self._save_name("Save DaVinci Resolve timeline", "DaVinci.lua", "DaVinci Resolve script (*.lua)")
        if not path:
            return
        base = os.path.splitext(path)[0]
        name = os.path.basename(base)
        folder = os.path.dirname(base)
        events = self.export_events(name)
        multicam, names = [], {}
        try:
            for ev_name, ls in events:
                stem = os.path.join(folder, ev_name)
                export_xml(ls, stem + ".xml", ev_name, self.current_fps(), target="resolve")
                mc = export_fcpxml_multicam(ls, stem + " - Multicam.fcpxml", ev_name, self.current_fps())
                names.update(mc["names"])
                multicam.append((stem + " - Multicam.fcpxml", ev_name + " - Multicam edit"))
            info = export_resolve_script(self.project.export_layers(), base + ".lua", name, self.current_fps(),
                                         events=events, multicam=multicam, names=names)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        menu_name = f"Endora - {name}"
        installed = False
        d = scripts_dir()
        if d:
            try:
                os.makedirs(d, exist_ok=True)
                import shutil
                shutil.copyfile(base + ".lua", os.path.join(d, menu_name + ".lua"))
                installed = True
            except OSError:
                installed = False
        n = info["timelines"]
        how = (f"In DaVinci Resolve: open your project, then Workspace > Scripts > {menu_name}.\n"
               f"It imports every file by its full path and builds {n} synced timeline{'s' if n > 1 else ''} "
               "plus a Multicam edit for each (cameras as angles, Zoom sound underneath).\n"
               "If the menu item isn't there yet, restart Resolve once.")
        if not installed:
            how = ("Couldn't add the script to Resolve's Scripts menu.\n"
                   "In Resolve: Workspace > Console, pick Lua, and type:\n"
                   f"dofile([[{base}.lua]])")
        QMessageBox.information(
            self, APP_NAME,
            f"Saved {os.path.basename(base)}.lua with the timeline and multicam files next to it.\n\n{how}\n\n"
            "Use the script, not the XML: Resolve links XML clips by file name, "
            "so cameras that all have a C0001.mov would get mixed up.")
        self.say(f"Exported {base}.lua")

def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    icon_path = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "endora.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
