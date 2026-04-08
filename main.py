#!/usr/bin/env python3
"""
MotionVpaper — Video wallpaper manager for Hyprland
Control mpvpaper with a clean GTK4/libadwaita interface.
"""

import os
import sys
import json
import subprocess
import hashlib
import time
from pathlib import Path

import gi
gi.require_versions({'Gtk': '4.0', 'Adw': '1', 'Gdk': '4.0', 'Gio': '2.0', 'Pango': '1.0'})
from gi.repository import Gtk, Gdk, Adw, GLib, GdkPixbuf, Pango, Gio

# ── Constants ────────────────────────────────────────────────────────────────

APP_NAME = "MotionVpaper"
APP_ID = "com.motionvpaper.gui"
CONFIG_DIR_NAME = "motionvpaper"

XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
XDG_CACHE  = Path(os.environ.get("XDG_CACHE_HOME",  str(Path.home() / ".cache")))

APP_CONFIG_DIR = XDG_CONFIG / CONFIG_DIR_NAME
APP_CACHE_DIR  = XDG_CACHE  / CONFIG_DIR_NAME
THUMB_DIR      = APP_CACHE_DIR / "thumbs"

CONFIG_FILE = APP_CONFIG_DIR / "library.json"
STATE_FILE  = APP_CONFIG_DIR / "state.json"

APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
APP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
THUMB_DIR.mkdir(parents=True, exist_ok=True)

# Migrate config from old mpvpaper-gui if motionvpaper config doesn't exist yet
OLD_CONFIG = XDG_CONFIG / "mpvpaper-gui"
OLD_CACHE = XDG_CACHE / "mpvpaper-gui"
if OLD_CONFIG.exists() and not CONFIG_FILE.exists():
    import shutil
    for src_name in ["library.json", "state.json"]:
        src = OLD_CONFIG / src_name
        if src.exists():
            shutil.copy2(src, APP_CONFIG_DIR / src_name)
    old_thumbs = OLD_CACHE / "thumbs"
    new_thumbs = THUMB_DIR
    if old_thumbs.exists() and not new_thumbs.exists():
        shutil.copytree(old_thumbs, new_thumbs)

VIDEO_EXTENSIONS = {"mp4", "mkv", "webm", "mov", "avi", "wmv", "flv"}

# ── Style ────────────────────────────────────────────────────────────────────

CSS = b"""
window, .background {
    background: #0d0d0f;
    color: #cccccc;
}
headerbar {
    background: #141416;
    border-bottom: 1px solid #222228;
    min-height: 28px;
    padding: 0px 8px;
}
headerbar label {
    font-size: 12px;
    color: #888888;
}
flowboxchild {
    background: transparent;
    border-radius: 10px;
    padding: 0;
}
flowboxchild:selected {
    background: rgba(255,255,255,0.04);
    outline: 2px solid #555555;
    outline-offset: -2px;
    border-radius: 10px;
}
.video-name {
    color: #888888;
    font-size: 11px;
}
.empty-state {
    color: #555555;
}
.controls-bar {
    background: #141416;
    border-top: 1px solid #222228;
    padding: 8px 0;
}
.play-btn {
    background: #333333;
    color: #cccccc;
    font-weight: 600;
    border-radius: 6px;
    min-height: 32px;
    padding: 0 18px;
}
.play-btn:hover {
    background: #444444;
}
.play-btn:disabled {
    background: #222222;
    color: #444444;
}
.stop-btn {
    background: #555555;
    color: #cccccc;
    font-weight: 600;
    border-radius: 6px;
    min-height: 32px;
    padding: 0 18px;
}
.stop-btn:hover {
    background: #666666;
}
.quit-btn {
    background: transparent;
    color: #555555;
    border: 1px solid #333333;
    border-radius: 6px;
    min-height: 32px;
    padding: 0 14px;
}
.quit-btn:hover {
    background: #222222;
    color: #cccccc;
}
.add-card {
    background: transparent;
    border: 2px dashed #333333;
    border-radius: 10px;
}
.add-card:hover {
    border-color: #555555;
    background: rgba(255,255,255,0.02);
}
"""


# ── Library ────────────────────────────────────────────────────────────────

class VideoLibrary:
    def __init__(self):
        self.videos: list[dict] = []
        self.load()

    def load(self):
        if CONFIG_FILE.exists():
            try:
                self.videos = json.loads(CONFIG_FILE.read_text())
            except Exception:
                self.videos = []

    def save(self):
        CONFIG_FILE.write_text(json.dumps(self.videos, indent=2))

    def add(self, path: str) -> bool:
        path = os.path.abspath(path)
        if any(v["path"] == path for v in self.videos):
            return False
        self.videos.append({"path": path, "name": os.path.basename(path)})
        self.save()
        return True

    def remove(self, path: str):
        self.videos = [v for v in self.videos if v["path"] != path]
        self.save()

    def thumb_path(self, video_path: str) -> str:
        key = hashlib.md5(video_path.encode()).hexdigest()
        return str(THUMB_DIR / f"{key}.jpg")


# ── Thumbnails ─────────────────────────────────────────────────────────────

def generate_thumb(video_path: str, thumb_path: str):
    if os.path.exists(thumb_path):
        return
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path,
         "-ss", "00:00:01", "-vframes", "1",
         "-vf", "scale=320:-1", "-q:v", "2", thumb_path],
        capture_output=True, timeout=30
    )


# ── Monitor Detection ─────────────────────────────────────────────────────

def get_monitors() -> list[str]:
    try:
        out = subprocess.check_output(["hyprctl", "monitors"], text=True)
        names = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Monitor") and " " in line:
                names.append(line.split()[1].rstrip(":"))
        return names or ["HDMI-A-1"]
    except Exception:
        return ["HDMI-A-1"]


# ── mpvpaper Process Management ────────────────────────────────────────────

def mpvpaper_stop():
    """Kill all running mpvpaper instances."""
    subprocess.run(["pkill", "-x", "mpvpaper"], capture_output=True)
    # Wait for processes to actually die (up to 1s)
    for _ in range(10):
        if not mpvpaper_running():
            break
        time.sleep(0.1)


def mpvpaper_running() -> bool:
    """Check if any mpvpaper process is alive."""
    return subprocess.run(["pgrep", "-x", "mpvpaper"], capture_output=True).returncode == 0


def mpvpaper_get_pids() -> list[int]:
    """Get all mpvpaper PIDs."""
    try:
        result = subprocess.run(["pgrep", "-x", "mpvpaper"], capture_output=True, text=True)
        return [int(p) for p in result.stdout.strip().split('\n') if p.strip()]
    except Exception:
        return []


def mpvpaper_start_single(monitor: str, video_path: str) -> bool:
    """Start mpvpaper on a single monitor. Returns True on success."""
    cmd = ["mpvpaper", "-f", "-o", "loop-file=yes", monitor, video_path]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except FileNotFoundError:
        return False


# ── Watchdog ──────────────────────────────────────────────────────────────────

class MpvpaperWatchdog:
    """Auto-restart mpvpaper if it crashes (known segfault bug in mpvpaper 1.8)."""

    def __init__(self):
        self._monitors: list[str] = []
        self._video: str | None = None
        self._active = False

    def set_config(self, monitors: list[str], video: str | None):
        self._monitors = monitors
        self._video = video

    def start(self):
        self._active = True
        GLib.timeout_add(5000, self._check)

    def stop(self):
        self._active = False

    def _check(self):
        if not self._active:
            return False
        if self._video and self._monitors and not mpvpaper_get_pids():
            # All instances died — restart them
            for i, mon in enumerate(self._monitors):
                if i > 0:
                    time.sleep(0.5)
                mpvpaper_start_single(mon, self._video)
        return True


# ── Main Window ─────────────────────────────────────────────────────────────

class MainWindow(Adw.ApplicationWindow):

    def __init__(self, app: Adw.Application):
        super().__init__(application=app)
        self.library = VideoLibrary()
        self.selected_video: str | None = None
        self.monitors: list[str] = []
        self._last_monitor: str = ""
        self._all_monitors: bool = False
        self.watchdog = MpvpaperWatchdog()

        self.set_title(APP_NAME)
        self.set_default_size(900, 640)

        self._apply_css()
        self._load_state()
        self._build_ui()
        self._refresh_monitors()
        self._refresh_video_grid()
        self.watchdog.start()
        self.connect("close-request", self._on_close)

    # ── Theming ────────────────────────────────────────────────────────────

    def _apply_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    # ── UI Construction ────────────────────────────────────────────────────

    def _build_ui(self):
        # Toast overlay as root
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toast_overlay.set_child(main_box)

        # Header
        header = Gtk.HeaderBar()
        title = Gtk.Label(label=APP_NAME)
        attrs = Pango.AttrList.new()
        attrs.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
        title.set_attributes(attrs)
        header.set_title_widget(title)
        main_box.append(header)

        # Scrollable video grid
        scrolled = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        main_box.append(scrolled)

        self.grid = Gtk.FlowBox(
            hexpand=True, vexpand=True,
            halign=Gtk.Align.CENTER, valign=Gtk.Align.START,
        )
        self.grid.set_margin_start(12)
        self.grid.set_margin_end(12)
        self.grid.set_margin_top(12)
        self.grid.set_margin_bottom(12)
        self.grid.set_row_spacing(6)
        self.grid.set_column_spacing(6)
        self.grid.set_homogeneous(True)
        self.grid.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.grid.connect("selected-children-changed", self._on_selection_changed)
        scrolled.set_child(self.grid)

        # Empty state
        self.empty_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=16,
            halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
            hexpand=True, vexpand=True,
        )
        empty_icon = Gtk.Image(icon_name="video-x-generic-symbolic", pixel_size=64)
        empty_icon.add_css_class("empty-state")
        self.empty_box.append(empty_icon)
        empty_label = Gtk.Label(label="No videos in library")
        empty_label.add_css_class("empty-state")
        self.empty_box.append(empty_label)
        add_btn = Gtk.Button(label="Add Video")
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", lambda _: self._add_video())
        self.empty_box.append(add_btn)
        main_box.append(self.empty_box)

        # Bottom controls bar
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True)
        controls.add_css_class("controls-bar")
        main_box.append(controls)

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, hexpand=True)
        inner.set_margin_start(20)
        inner.set_margin_end(20)
        controls.append(inner)

        # Monitor selector
        mon_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, valign=Gtk.Align.CENTER)
        mon_group.append(Gtk.Label(label="Monitor"))
        self.monitor_dropdown = Gtk.DropDown()
        self.monitor_dropdown.set_size_request(140, 34)
        mon_group.append(self.monitor_dropdown)
        inner.append(mon_group)

        # All monitors toggle
        self.all_monitors_toggle = Gtk.CheckButton(label="All")
        self.all_monitors_toggle.connect("toggled", lambda _: self._on_monitor_toggled())
        inner.append(self.all_monitors_toggle)

        # Autostart toggle
        self.autostart_toggle = Gtk.CheckButton(label="Autostart")
        self.autostart_toggle.set_active(self._is_autostart_enabled())
        self.autostart_toggle.connect("toggled", lambda _: self._on_autostart_toggled())
        inner.append(self.autostart_toggle)

        # Spacer
        inner.append(Gtk.Box(hexpand=True))

        # Play / Stop / Quit buttons
        self.play_btn = Gtk.Button(label="Play")
        self.play_btn.add_css_class("play-btn")
        self.play_btn.set_sensitive(False)
        self.play_btn.connect("clicked", lambda _: self._play())
        inner.append(self.play_btn)

        self.stop_btn = Gtk.Button(label="Stop")
        self.stop_btn.add_css_class("stop-btn")
        self.stop_btn.set_visible(False)
        self.stop_btn.connect("clicked", lambda _: self._stop())
        inner.append(self.stop_btn)

        self.quit_btn = Gtk.Button(label="Quit")
        self.quit_btn.add_css_class("quit-btn")
        self.quit_btn.connect("clicked", lambda _: self._quit_app())
        inner.append(self.quit_btn)

    # ── State Persistence ─────────────────────────────────────────────────

    def _load_state(self):
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text())
                self.selected_video = state.get("selected_video")
                self._last_monitor = state.get("monitor", "")
                self._all_monitors = state.get("all_monitors", False)
            except Exception:
                pass

    def _save_state(self):
        state = {
            "selected_video": self.selected_video,
            "monitor": self._last_monitor,
            "all_monitors": self._all_monitors,
            "was_playing": mpvpaper_running() or self._is_autostart_enabled(),
        }
        STATE_FILE.write_text(json.dumps(state))

    # ── Monitors ──────────────────────────────────────────────────────────

    def _refresh_monitors(self):
        self.monitors = get_monitors()
        model = Gtk.StringList()
        for m in self.monitors:
            model.append(m)
        self.monitor_dropdown.set_model(model)
        if self._last_monitor in self.monitors:
            self.monitor_dropdown.set_selected(self.monitors.index(self._last_monitor))
        elif self.monitors:
            self.monitor_dropdown.set_selected(0)
        self.all_monitors_toggle.set_active(self._all_monitors)
        self.monitor_dropdown.set_sensitive(not self._all_monitors)

    def _on_monitor_toggled(self):
        self._all_monitors = self.all_monitors_toggle.get_active()
        self.monitor_dropdown.set_sensitive(not self._all_monitors)
        self._save_state()

    def _get_selected_monitors(self) -> list[str]:
        if self._all_monitors:
            return self.monitors
        idx = self.monitor_dropdown.get_selected()
        if 0 <= idx < len(self.monitors):
            return [self.monitors[idx]]
        return self.monitors[:1]

    def _is_autostart_enabled(self) -> bool:
        return (Path.home() / ".config/autostart/motionvpaper.desktop").exists()

    def _on_autostart_toggled(self):
        enabled = self.autostart_toggle.get_active()
        autostart_file = Path.home() / ".config/autostart/motionvpaper.desktop"
        if enabled:
            autostart_file.parent.mkdir(parents=True, exist_ok=True)
            autostart_file.write_text(
                "[Desktop Entry]\n"
                f"Name={APP_NAME}\n"
                "Comment=Video wallpaper manager for Hyprland (autostart)\n"
                f"Exec=python3 {Path(__file__).resolve()}\n"
                "Icon=video-x-generic\n"
                "Terminal=false\n"
                "Type=Application\n"
                "Categories=AudioVideo;Video;\n"
                "Keywords=wallpaper;video;mpvpaper;hyprland;\n"
                "X-GNOME-Autostart-enabled=true\n"
            )
            self._toast("Autostart enabled")
        else:
            autostart_file.unlink(missing_ok=True)
            self._toast("Autostart disabled")

    # ── Video Grid ─────────────────────────────────────────────────────────

    def _refresh_video_grid(self):
        # Clear existing children
        while (child := self.grid.get_first_child()):
            self.grid.remove(child)

        # Generate missing thumbnails
        for entry in self.library.videos:
            thumb = self.library.thumb_path(entry["path"])
            if not os.path.exists(thumb):
                generate_thumb(entry["path"], thumb)

        # Build cards
        for entry in self.library.videos:
            self.grid.append(self._make_video_card(entry["path"], entry["name"],
                                                     self.library.thumb_path(entry["path"])))
        self.grid.append(self._make_add_card())

        has_videos = len(self.library.videos) > 0
        self.grid.set_visible(has_videos)
        self.empty_box.set_visible(not has_videos)
        self._update_buttons()

    def _make_video_card(self, path: str, name: str, thumb_path: str) -> Gtk.FlowBoxChild:
        child = Gtk.FlowBoxChild()
        child.path = path

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_start(3)
        box.set_margin_end(3)
        box.set_margin_top(3)
        box.set_margin_bottom(3)
        child.set_child(box)

        # Thumbnail
        thumb_box = Gtk.Box(halign=Gtk.Align.FILL, valign=Gtk.Align.FILL)
        thumb_box.set_size_request(280, 158)
        box.append(thumb_box)
        self._load_thumbnail(thumb_box, thumb_path)

        # Filename
        label = Gtk.Label(label=name, halign=Gtk.Align.START, ellipsize=Pango.EllipsizeMode.END)
        label.add_css_class("video-name")
        label.set_margin_start(2)
        box.append(label)

        # Hover remove button
        remove_btn = Gtk.Button(label="\u2715", has_frame=False)
        remove_btn.set_valign(Gtk.Align.CENTER)
        remove_btn.set_halign(Gtk.Align.END)
        remove_btn.set_visible(False)
        remove_btn.connect("clicked", lambda _, p=path: self._remove_video(p))

        hover = Gtk.EventControllerMotion()
        hover.connect("enter", lambda *_: remove_btn.set_visible(True))
        hover.connect("leave", lambda *_: remove_btn.set_visible(False))
        box.add_controller(hover)

        return child

    def _make_add_card(self) -> Gtk.FlowBoxChild:
        item = Gtk.FlowBoxChild()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("add-card")
        box.set_size_request(286, 200)
        box.set_margin_start(3)
        box.set_margin_end(3)
        box.set_margin_top(3)
        box.set_margin_bottom(3)
        item.set_child(box)

        box.append(Gtk.Box(vexpand=True))

        icon = Gtk.Image(icon_name="list-add-symbolic", pixel_size=40)
        icon.add_css_class("empty-state")
        icon.set_halign(Gtk.Align.CENTER)
        box.append(icon)

        label = Gtk.Label(label="Add Video")
        label.add_css_class("empty-state")
        label.set_halign(Gtk.Align.CENTER)
        box.append(label)

        box.append(Gtk.Box(vexpand=True))

        click = Gtk.GestureClick()
        click.connect("pressed", lambda *_: self._add_video())
        box.add_controller(click)

        return item

    def _load_thumbnail(self, container: Gtk.Box, thumb_path: str | None):
        while (child := container.get_first_child()):
            container.remove(child)

        if thumb_path and os.path.exists(thumb_path):
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file(thumb_path)
                pic = Gtk.Picture.new_for_paintable(Gdk.Texture.new_for_pixbuf(pixbuf))
                pic.set_hexpand(True)
                pic.set_vexpand(True)
                pic.set_can_shrink(True)
                pic.set_content_fit(Gtk.ContentFit.CONTAIN)
                pic.set_halign(Gtk.Align.FILL)
                pic.set_valign(Gtk.Align.FILL)
                container.append(pic)
                return
            except Exception:
                pass

        # Fallback icon
        icon = Gtk.Image(icon_name="video-x-generic-symbolic", pixel_size=48)
        icon.add_css_class("empty-state")
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_valign(Gtk.Align.CENTER)
        container.append(icon)

    def _on_selection_changed(self, flowbox):
        selected = flowbox.get_selected_children()
        if selected and hasattr(selected[0], 'path'):
            self.selected_video = selected[0].path
            self._save_state()
        self._update_buttons()

    def _remove_video(self, path: str):
        self.library.remove(path)
        if self.selected_video == path:
            self.selected_video = None
            self._save_state()
        self._refresh_video_grid()

    def _add_video_path(self, path: str):
        ext = path.lower().rsplit('.', 1)[-1]
        if ext not in VIDEO_EXTENSIONS:
            self._toast(f"Unsupported format: .{ext}")
            return
        if self.library.add(path):
            generate_thumb(path, self.library.thumb_path(path))
            self._refresh_video_grid()
            self.selected_video = path
            self._save_state()
            self._update_buttons()
        else:
            self._toast("Video already in library")

    def _update_buttons(self):
        running = mpvpaper_running()
        self.play_btn.set_visible(not running)
        self.stop_btn.set_visible(running)
        self.play_btn.set_sensitive(self.selected_video is not None and not running)

    # ── Actions ───────────────────────────────────────────────────────────

    def _add_video(self):
        filt = Gtk.FileFilter()
        for ext in VIDEO_EXTENSIONS:
            filt.add_pattern(f"*.{ext}")
        filt.set_name("Video files")

        dialog = Gtk.FileChooserDialog(title="Select Videos", action=Gtk.FileChooserAction.OPEN)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Open", Gtk.ResponseType.ACCEPT)
        dialog.add_filter(filt)
        dialog.set_select_multiple(True)
        dialog.connect("response", self._on_file_response)
        dialog.set_transient_for(self)
        dialog.present()

    def _on_file_response(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT:
            for file in dialog.get_files():
                path = file.get_path()
                if path:
                    self._add_video_path(path)
        dialog.destroy()

    def _play(self):
        if not self.selected_video:
            return
        monitors = self._get_selected_monitors()
        if not monitors:
            self._toast("No monitors found")
            return

        self._last_monitor = monitors[0]
        self._save_state()

        mpvpaper_stop()
        for i, mon in enumerate(monitors):
            if i > 0:
                time.sleep(0.5)
            if not mpvpaper_start_single(mon, self.selected_video):
                self._toast("mpvpaper not found")
                return

        suffix = "all monitors" if len(monitors) > 1 else monitors[0]
        self._toast(f"Playing on {suffix}")
        self.watchdog.set_config(monitors, self.selected_video)
        self._update_buttons()

    def _stop(self):
        mpvpaper_stop()
        self.watchdog.set_config([], None)
        self._update_buttons()
        self._toast("Stopped")

    def _quit_app(self):
        self._save_state()
        mpvpaper_stop()
        self.get_application().quit()

    def _toast(self, msg: str):
        self.toast_overlay.add_toast(Adw.Toast(title=msg, timeout=2))

    def _on_close(self, *_):
        self._save_state()
        self.set_visible(False)


# ── Application ────────────────────────────────────────────────────────────

class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.connect("activate", self._on_activate)

    def _on_activate(self, app: Adw.Application):
        if hasattr(self, 'win') and self.win:
            self.win.present()
            return
        self.win = MainWindow(app)
        self.win.present()
        self.win.connect("close-request", self._on_close_request)
        self._autoplay_last()

    def _on_close_request(self, window):
        window.hide()
        return True

    def _autoplay_last(self):
        if not STATE_FILE.exists():
            return
        try:
            state = json.loads(STATE_FILE.read_text())
            video = state.get("selected_video")
            if state.get("was_playing") and video and os.path.exists(video):
                # Small delay for Hyprland to be ready on autostart
                time.sleep(1.5)
                monitors = self.win._get_selected_monitors()
                if monitors:
                    mpvpaper_stop()
                    for i, mon in enumerate(monitors):
                        if i > 0:
                            time.sleep(0.3)
                        mpvpaper_start_single(mon, video)
                    self.win.watchdog.set_config(monitors, video)
        except Exception:
            pass


# ── Entry Point ────────────────────────────────────────────────────────────

def kill_stale_instances():
    """Kill any other running instance of this app."""
    current_pid = os.getpid()
    try:
        result = subprocess.run(["pgrep", "-f", "motionvpaper/main.py"],
                               capture_output=True, text=True)
        for line in result.stdout.strip().split('\n'):
            pid = int(line.strip()) if line.strip() else 0
            if pid and pid != current_pid:
                os.kill(pid, 9)
    except Exception:
        pass


def main():
    if subprocess.run(["which", "mpvpaper"], capture_output=True).returncode != 0:
        print("mpvpaper not found. Install: yay -S mpvpaper", file=sys.stderr)
        sys.exit(1)

    kill_stale_instances()

    app = App()
    app.register()
    app.hold()
    app.run(sys.argv)


if __name__ == "__main__":
    main()