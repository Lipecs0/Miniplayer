"""
Miniplayer para a barra de tarefas (Windows 10/11)

Lê a sessão de mídia ativa do Windows (a mesma do controle de mídia do sistema),
então funciona com Spotify, Chrome, Edge, Firefox, Brave, VLC, Media Player etc.

Instalação:
    pip install pillow pycaw winrt-runtime winrt-Windows.Media.Control ^
        winrt-Windows.Storage.Streams winrt-Windows.Foundation ^
        winrt-Windows.Foundation.Collections

Uso:
    python miniplayer.py          (renomeie para .pyw para rodar sem console)

Recursos:
    - Capa, título, artista e app de origem
    - Anterior / Pausar-Retomar / Próxima (anterior volta ao começo se já tocou 3 s)
    - Barra de progresso clicável/arrastável (seek)
    - Volume do sistema (slider, roda do mouse sobre o player, clique no ícone = mudo)
    - Configurações (ícone de engrenagem ou botão direito): tema claro/escuro,
      translucidez, modo flutuante ou fixado na barra de tarefas, iniciar com o Windows
"""

import asyncio
import ctypes
import io
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import winreg
from ctypes import wintypes
from datetime import datetime, timezone
from tkinter import font as tkfont

from PIL import Image, ImageOps, ImageTk
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winrt.windows.storage.streams import Buffer, DataReader, InputStreamOptions

APP_NAME = "Miniplayer"
TRANSLUCENT_ALPHA = 0.85
MAX_DOCK_OFFSET = 600

# --------------------------------------------------------------------------- #
# Temas
# --------------------------------------------------------------------------- #
THEMES = {
    "dark": {
        "bg": "#1c1c1e", "panel": "#2c2c30", "fg": "#f2f2f7", "dim": "#9a9aa2",
        "accent": "#8ab4f8", "on_accent": "#14141a", "track": "#3a3a40",
        "danger": "#ff6b6b", "cover_fg": "#6e6e75",
    },
    "light": {
        "bg": "#f5f5f7", "panel": "#e3e3e8", "fg": "#1d1d1f", "dim": "#6e6e73",
        "accent": "#0a64d6", "on_accent": "#ffffff", "track": "#cfcfd6",
        "danger": "#d93025", "cover_fg": "#a0a0a8",
    },
}

APP_LABELS = {
    "spotify": "Spotify", "chrome": "Chrome", "msedge": "Edge", "edge": "Edge",
    "firefox": "Firefox", "brave": "Brave", "opera": "Opera", "vivaldi": "Vivaldi",
    "zunemusic": "Media Player", "microsoft.media": "Media Player", "vlc": "VLC",
    "itunes": "iTunes", "applemusic": "Apple Music", "deezer": "Deezer",
    "tidal": "TIDAL", "foobar": "foobar2000", "musicbee": "MusicBee", "aimp": "AIMP",
}


def friendly_app_name(app_id):
    low = (app_id or "").lower()
    for key, name in APP_LABELS.items():
        if key in low:
            return name
    return (app_id or "").split("!")[-1].replace(".exe", "").strip() or "Mídia"


def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def fmt_time(seconds):
    seconds = int(max(seconds, 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def fit_text(text, font, max_width):
    if font.measure(text) <= max_width:
        return text
    while text and font.measure(text + "…") > max_width:
        text = text[:-1]
    return text + "…"


# --------------------------------------------------------------------------- #
# Configurações salvas em %APPDATA%\Miniplayer\config.json
# --------------------------------------------------------------------------- #
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_CONFIG = {
    "theme": "dark",          # dark | light
    "translucent": False,
    "mode": "floating",       # floating | docked
    "dock_side": "right",     # left | right
    "dock_offset": 0,         # px afastados do canto escolhido (modo fixado)
    "x": None,
    "y": None,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Iniciar com o Windows (registro: HKCU\...\Run)
# --------------------------------------------------------------------------- #
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _startup_command():
    if getattr(sys, "frozen", False):  # executável gerado pelo PyInstaller
        return f'"{sys.executable}"'
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw  # evita abrir uma janela de console
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _startup_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
    except OSError as exc:
        print("não foi possível alterar a inicialização automática:", exc)


# --------------------------------------------------------------------------- #
# Funções do Windows (user32 / dwmapi / kernel32)
# --------------------------------------------------------------------------- #
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    dwmapi = ctypes.WinDLL("dwmapi")
    dwmapi.DwmSetWindowAttribute.argtypes = [
        wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
except OSError:
    dwmapi = None


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND,
                                 wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowExW.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.SystemParametersInfoW.argtypes = [ctypes.c_uint, ctypes.c_uint,
                                         ctypes.c_void_p, ctypes.c_uint]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = ctypes.c_void_p

_mutex_handle = None


def already_running():
    """Impede duas cópias abertas (útil com a inicialização automática)."""
    global _mutex_handle
    _mutex_handle = kernel32.CreateMutexW(None, False, "Local\\MiniplayerSingleton")
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def work_area():
    rect = wintypes.RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
    return rect.left, rect.top, rect.right, rect.bottom


def virtual_screen():
    left = user32.GetSystemMetrics(76)
    top = user32.GetSystemMetrics(77)
    return left, top, left + user32.GetSystemMetrics(78), top + user32.GetSystemMetrics(79)


def toplevel_hwnd(widget):
    wid = widget.winfo_id()
    return user32.GetParent(wid) or wid


def round_corners(widget):
    """Cantos arredondados no Windows 11 (ignorado em versões antigas)."""
    if dwmapi is None:
        return
    try:
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        dwmapi.DwmSetWindowAttribute(toplevel_hwnd(widget), 33, ctypes.byref(pref), 4)
    except Exception:
        pass


def set_dark_titlebar(widget, dark):
    if dwmapi is None:
        return
    try:
        widget.update_idletasks()
        value = ctypes.c_int(1 if dark else 0)
        hwnd = toplevel_hwnd(widget)
        for attr in (20, 19):
            if dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), 4) == 0:
                break
    except Exception:
        pass


def keep_on_top(hwnd):
    # HWND_TOPMOST = -1; NOMOVE | NOSIZE | NOACTIVATE
    user32.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0013)


def taskbar_info():
    """Retângulo da barra de tarefas principal (só horizontal é suportada)."""
    tray = user32.FindWindowW("Shell_TrayWnd", None)
    if not tray:
        return None
    rect = wintypes.RECT()
    if not user32.GetWindowRect(tray, ctypes.byref(rect)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0 or height >= width:
        return None
    notify_left = None
    notify = user32.FindWindowExW(tray, None, "TrayNotifyWnd", None)
    if notify:
        nrect = wintypes.RECT()
        if (user32.GetWindowRect(notify, ctypes.byref(nrect))
                and nrect.right - nrect.left > 0
                and nrect.left > rect.left + width // 2):
            notify_left = nrect.left
    return {"left": rect.left, "top": rect.top, "right": rect.right,
            "bottom": rect.bottom, "notify_left": notify_left}


def info_signature(info):
    if not info:
        return None
    return (info["left"], info["top"], info["right"], info["bottom"], info["notify_left"])


def foreground_fullscreen(own_hwnd):
    """True se a janela em primeiro plano cobre o monitor inteiro (vídeo em tela cheia)."""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd or hwnd == own_hwnd:
            return False
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, name, 64)
        if name.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            return False
        if user32.GetWindowLongW(hwnd, -16) & 0x00C00000:  # tem barra de título
            return False
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        monitor = user32.MonitorFromWindow(hwnd, 2)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(monitor, ctypes.byref(info))
        mon = info.rcMonitor
        return (rect.left <= mon.left and rect.top <= mon.top
                and rect.right >= mon.right and rect.bottom >= mon.bottom)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Backend de mídia (SMTC) em thread separada
# --------------------------------------------------------------------------- #
class MediaBackend:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = {
            "active": False, "title": "", "artist": "", "app": "",
            "playing": False, "thumb": None, "position": 0.0, "duration": 0.0,
        }
        self._key = None
        self._thumb = None
        self._thumb_tries = 0
        self._start = 0.0
        self.manager = None
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self):
        self.manager = await SessionManager.request_async()
        while True:
            try:
                await self._tick()
            except Exception as exc:
                print("erro ao ler mídia:", exc)
            await asyncio.sleep(0.5)

    async def _tick(self):
        session = self.manager.get_current_session()
        if session is None:
            self._key = None
            self._thumb = None
            self._set(active=False, title="", artist="", app="", playing=False,
                      thumb=None, position=0.0, duration=0.0)
            return

        info = await session.try_get_media_properties_async()
        playback = session.get_playback_info()
        timeline = session.get_timeline_properties()
        app_id = session.source_app_user_model_id

        key = (app_id, info.title, info.artist)
        if key != self._key:
            self._key = key
            self._thumb = None
            self._thumb_tries = 0
        if self._thumb is None and self._thumb_tries < 6:
            self._thumb_tries += 1
            self._thumb = await self._read_thumbnail(info.thumbnail)

        playing = playback.playback_status == PlaybackStatus.PLAYING
        position, duration = self._timeline(timeline, playing)
        self._set(
            active=True, title=info.title or "",
            artist=info.artist or info.album_title or "",
            app=friendly_app_name(app_id), playing=playing, thumb=self._thumb,
            position=position, duration=duration,
        )

    @staticmethod
    async def _read_thumbnail(ref):
        if ref is None:
            return None
        try:
            stream = await ref.open_read_async()
            size = int(stream.size)
            if size <= 0:
                return None
            buf = Buffer(size)
            await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
            try:
                data = bytes(memoryview(buf))[: buf.length]
            except TypeError:
                reader = DataReader.from_buffer(buf)
                arr = bytearray(buf.length)
                reader.read_bytes(arr)
                data = bytes(arr)
            stream.close()
            return data or None
        except Exception:
            return None

    def _timeline(self, tl, playing):
        try:
            self._start = tl.start_time.total_seconds()
            duration = max(tl.end_time.total_seconds() - self._start, 0.0)
            position = tl.position.total_seconds() - self._start
            if playing and duration:
                updated = tl.last_updated_time
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                delta = (datetime.now(timezone.utc) - updated).total_seconds()
                if 0 <= delta < duration:
                    position += delta
            if duration:
                position = min(max(position, 0.0), duration)
            return max(position, 0.0), duration
        except Exception:
            return 0.0, 0.0

    def _set(self, **values):
        with self._lock:
            self._state.update(values)

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    # ---- comandos ---------------------------------------------------------- #
    def send(self, command, arg=None):
        if self.manager is None:
            return
        asyncio.run_coroutine_threadsafe(self._command(command, arg), self.loop)

    async def _command(self, command, arg):
        session = self.manager.get_current_session()
        if session is None:
            return
        if command == "toggle":
            await session.try_toggle_play_pause_async()
        elif command == "next":
            await session.try_skip_next_async()
        elif command == "seek":
            ticks = int((self._start + float(arg)) * 10_000_000)
            await session.try_change_playback_position_async(ticks)
        elif command == "prev":
            # Já tocou mais de 3 s: volta ao começo; senão, faixa anterior.
            if self.snapshot()["position"] > 3:
                if await session.try_change_playback_position_async(0):
                    return
            await session.try_skip_previous_async()


# --------------------------------------------------------------------------- #
# Volume do sistema (pycaw) em thread própria, por causa do COM
# --------------------------------------------------------------------------- #
class SystemVolume:
    def __init__(self):
        self._level = None
        self._muted = False
        self._queue = queue.Queue()
        self._warned = False
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def level(self):
        return self._level

    @property
    def muted(self):
        return self._muted

    def set_level(self, value):
        value = clamp(value)
        self._level = value
        self._queue.put(("level", value))

    def toggle_mute(self):
        self._queue.put(("mute", None))

    @staticmethod
    def _open_endpoint():
        from pycaw.pycaw import AudioUtilities
        speakers = AudioUtilities.GetSpeakers()
        endpoint = getattr(speakers, "EndpointVolume", None)  # pycaw mais novo
        if endpoint is not None:
            return endpoint
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import IAudioEndpointVolume
        iface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return ctypes.cast(iface, ctypes.POINTER(IAudioEndpointVolume))

    def _run(self):
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass
        endpoint = None
        while True:
            try:
                if endpoint is None:
                    endpoint = self._open_endpoint()
                try:
                    item = self._queue.get(timeout=0.4)
                except queue.Empty:
                    item = None
                while item is not None:
                    kind, value = item
                    if kind == "level":
                        if endpoint.GetMute():
                            endpoint.SetMute(0, None)
                        endpoint.SetMasterVolumeLevelScalar(value, None)
                    elif kind == "mute":
                        endpoint.SetMute(0 if endpoint.GetMute() else 1, None)
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        item = None
                self._level = float(endpoint.GetMasterVolumeLevelScalar())
                self._muted = bool(endpoint.GetMute())
            except Exception as exc:
                if not self._warned:
                    print("controle de volume indisponível:", exc)
                    self._warned = True
                endpoint = None
                self._level = None
                time.sleep(2)


# --------------------------------------------------------------------------- #
# Widgets próprios
# --------------------------------------------------------------------------- #
class Slider:
    """Barra arrastável (usada para progresso, volume e deslocamento)."""

    def __init__(self, parent, width, height, theme, bg, on_change=None, on_commit=None,
                 knob=True, thickness=4, knob_radius=6, value=0.0):
        self.w, self.h = width, height
        self.theme = theme
        self.on_change, self.on_commit = on_change, on_commit
        self.knob = knob
        self.th = thickness
        self.r = knob_radius
        self.pad = knob_radius if knob else thickness / 2
        self.value = value
        self.enabled = True
        self.dragging = False
        self.canvas = tk.Canvas(parent, width=width, height=height, bg=bg,
                                highlightthickness=0, cursor="hand2")
        self.canvas.bind("<ButtonPress-1>", self._down)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._up)
        self.draw()

    def _value_at(self, x):
        span = max(1.0, self.w - 2 * self.pad)
        return clamp((x - self.pad) / span)

    def _down(self, event):
        if not self.enabled:
            return
        self.dragging = True
        self.value = self._value_at(event.x)
        self.draw()
        if self.on_change:
            self.on_change(self.value)

    def _move(self, event):
        if not self.dragging:
            return
        self.value = self._value_at(event.x)
        self.draw()
        if self.on_change:
            self.on_change(self.value)

    def _up(self, event):
        if not self.dragging:
            return
        self.dragging = False
        self.value = self._value_at(event.x)
        self.draw()
        if self.on_commit:
            self.on_commit(self.value)

    def set(self, value, enabled=True):
        if self.dragging:
            return
        value = clamp(value)
        if value != self.value or enabled != self.enabled:
            self.value, self.enabled = value, enabled
            self.draw()

    def draw(self):
        c = self.canvas
        c.delete("all")
        y = self.h / 2
        x0, x1 = self.pad, self.w - self.pad
        c.create_line(x0, y, x1, y, width=self.th, capstyle="round",
                      fill=self.theme["track"])
        xv = x0 + (x1 - x0) * self.value
        if self.value > 0.001:
            fill = self.theme["accent"] if self.enabled else self.theme["dim"]
            c.create_line(x0, y, xv, y, width=self.th, capstyle="round", fill=fill)
        if self.knob and self.enabled:
            r = self.r
            c.create_oval(xv - r, y - r, xv + r, y + r, fill=self.theme["fg"], outline="")


class IconButton(tk.Canvas):
    """Botões anterior / play / pause / próxima desenhados no canvas."""

    def __init__(self, parent, kind, width, height, theme, command, circle=False):
        super().__init__(parent, width=width, height=height, bg=theme["bg"],
                         highlightthickness=0, cursor="hand2")
        self.kind, self.bw, self.bh = kind, width, height
        self.theme, self.circle = theme, circle
        self.is_hover = False
        self.bind("<Button-1>", lambda e: command())
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.draw()

    def _hover(self, state):
        self.is_hover = state
        self.draw()

    def set_kind(self, kind):
        if kind != self.kind:
            self.kind = kind
            self.draw()

    def draw(self):
        t = self.theme
        self.delete("all")
        cx, cy = self.bw / 2, self.bh / 2
        if self.circle:
            r = min(self.bw, self.bh) / 2 - 1
            self.create_oval(cx - r, cy - r, cx + r, cy + r, outline="",
                             fill=t["accent"] if self.is_hover else t["fg"])
            color, u = t["bg"], r * 0.42
        else:
            if self.is_hover:
                self.create_rectangle(0, 0, self.bw, self.bh, fill=t["panel"], outline="")
            color, u = t["fg"], min(self.bw, self.bh) * 0.24

        if self.kind == "play":
            self.create_polygon(cx - 0.7 * u, cy - u, cx - 0.7 * u, cy + u, cx + u, cy,
                                fill=color, outline=color)
        elif self.kind == "pause":
            self.create_rectangle(cx - 0.9 * u, cy - u, cx - 0.2 * u, cy + u,
                                  fill=color, outline=color)
            self.create_rectangle(cx + 0.2 * u, cy - u, cx + 0.9 * u, cy + u,
                                  fill=color, outline=color)
        elif self.kind == "prev":
            bar = max(2.0, u * 0.35)
            self.create_rectangle(cx - u, cy - u, cx - u + bar, cy + u,
                                  fill=color, outline=color)
            self.create_polygon(cx + u, cy - u, cx + u, cy + u, cx - u + bar, cy,
                                fill=color, outline=color)
        elif self.kind == "next":
            bar = max(2.0, u * 0.35)
            self.create_rectangle(cx + u - bar, cy - u, cx + u, cy + u,
                                  fill=color, outline=color)
            self.create_polygon(cx - u, cy - u, cx - u, cy + u, cx + u - bar, cy,
                                fill=color, outline=color)


class SpeakerIcon(tk.Canvas):
    """Ícone de volume (clique = mudo)."""

    def __init__(self, parent, theme, width, height, command):
        super().__init__(parent, width=width, height=height, bg=theme["bg"],
                         highlightthickness=0, cursor="hand2")
        self.theme, self.sw, self.sh = theme, width, height
        self.level, self.muted = None, False
        self.bind("<Button-1>", lambda e: command())
        self.draw()

    def update_state(self, level, muted):
        key = (None if level is None else round(level, 2), muted)
        if key != (self.level, self.muted):
            self.level, self.muted = key
            self.draw()

    def draw(self):
        self.delete("all")
        color = self.theme["fg"]
        cy = self.sh / 2
        u = min(self.sw, self.sh) * 0.30
        x0 = self.sw * 0.18
        self.create_polygon(
            x0, cy - 0.38 * u, x0 + 0.5 * u, cy - 0.38 * u, x0 + 1.1 * u, cy - 0.9 * u,
            x0 + 1.1 * u, cy + 0.9 * u, x0 + 0.5 * u, cy + 0.38 * u, x0, cy + 0.38 * u,
            fill=color, outline=color)
        if self.muted or not self.level:
            xa = x0 + 1.5 * u
            self.create_line(xa, cy - 0.5 * u, xa + u, cy + 0.5 * u, fill=color, width=2)
            self.create_line(xa, cy + 0.5 * u, xa + u, cy - 0.5 * u, fill=color, width=2)
        else:
            cxw = x0 + 1.1 * u
            for i, threshold in enumerate((0.0, 0.5)):
                if self.level > threshold:
                    r = (0.7 + 0.5 * i) * u
                    self.create_arc(cxw - r, cy - r, cxw + r, cy + r, start=-50,
                                    extent=100, style="arc", outline=color, width=2)


class Toggle(tk.Canvas):
    def __init__(self, parent, theme, bg, value, command, scale):
        self.tw, self.th = int(44 * scale), int(24 * scale)
        super().__init__(parent, width=self.tw, height=self.th, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self.theme, self.value, self.command = theme, bool(value), command
        self.bind("<Button-1>", self._click)
        self.draw()

    def _click(self, event):
        self.value = not self.value
        self.draw()
        self.command(self.value)

    def draw(self):
        self.delete("all")
        w, h = self.tw, self.th
        r = h / 2
        color = self.theme["accent"] if self.value else self.theme["track"]
        self.create_line(r, r, w - r, r, width=h - 2, capstyle="round", fill=color)
        kr = r - 4
        cx = w - r if self.value else r
        self.create_oval(cx - kr, r - kr, cx + kr, r + kr, fill="#ffffff", outline="")


class Segmented(tk.Frame):
    def __init__(self, parent, theme, options, value, command, scale_fn):
        super().__init__(parent, bg=theme["panel"])
        self.theme, self.command = theme, command
        self.items = {}
        for label, val in options:
            item = tk.Label(self, text=label, font=("Segoe UI", 9), cursor="hand2",
                            padx=scale_fn(10), pady=scale_fn(3))
            item.pack(side="left", padx=scale_fn(2), pady=scale_fn(2))
            item.bind("<Button-1>", lambda e, v=val: self._pick(v))
            self.items[val] = item
        self.select(value)

    def select(self, value):
        t = self.theme
        for val, item in self.items.items():
            on = val == value
            item.config(bg=t["accent"] if on else t["panel"],
                        fg=t["on_accent"] if on else t["fg"])

    def _pick(self, value):
        self.select(value)
        self.command(value)


# --------------------------------------------------------------------------- #
# Aplicativo
# --------------------------------------------------------------------------- #
class Miniplayer:
    def __init__(self):
        enable_dpi_awareness()
        self.cfg = load_config()
        self.backend = MediaBackend()
        self.volume = SystemVolume()

        self.root = root = tk.Tk()
        root.title(APP_NAME)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        self.k = root.winfo_fpixels("1i") / 96.0

        self.f_title = tkfont.Font(family="Segoe UI Semibold", size=10)
        self.f_sub = tkfont.Font(family="Segoe UI", size=9)
        self.f_small = tkfont.Font(family="Segoe UI", size=8)
        self.f_dtitle = tkfont.Font(family="Segoe UI Semibold", size=9)
        self.f_dsub = tkfont.Font(family="Segoe UI", size=8)

        self.menu = tk.Menu(root, tearoff=0)
        self.ui = None
        self.settings_win = None
        self.docked = False
        self.t = THEMES["dark"]
        self.W = self.H = 0
        self._info = None
        self._dock_sig = None
        self._cycle = 0
        self._flash = None
        self._fs_hidden = False
        self._closing = False
        self._seek_hold = (0.0, 0.0)
        self._drag = (0, 0)
        self._shown = {}
        self._shown_thumb = object()
        self._cover_photo = None

        root.bind_all("<MouseWheel>", self._on_wheel)
        self.rebuild()
        self.refresh()

    # ---- utilidades -------------------------------------------------------- #
    def s(self, value):
        return int(round(value * self.k))

    def run(self):
        self.root.mainloop()

    def quit(self):
        self._closing = True
        save_config(self.cfg)
        self.root.destroy()

    # ---- construção da interface ------------------------------------------ #
    def rebuild(self):
        t = self.t = THEMES.get(self.cfg["theme"], THEMES["dark"])
        if self.ui is not None:
            self.ui.destroy()
        self.root.configure(bg=t["bg"])
        self.ui = tk.Frame(self.root, bg=t["bg"], highlightthickness=1,
                           highlightbackground=t["track"], highlightcolor=t["track"])
        self.ui.place(x=0, y=0, relwidth=1, relheight=1)

        info = taskbar_info() if self.cfg["mode"] == "docked" else None
        self._dock_sig = info_signature(info)
        self._info = info
        self.docked = info is not None

        self.vol_slider = self.speaker = self.lbl_elapsed = self.lbl_total = None
        if self.docked:
            self._build_docked(info)
        else:
            self._build_floating()

        self._bind_menu(self.ui)
        self._configure_menu()
        self._shown = {}
        self._shown_thumb = object()
        self._cover_photo = None
        self._apply_window()
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self._build_settings()

    def _text_button(self, text, command, size, hover=None):
        t = self.t
        lbl = tk.Label(self.ui, text=text, bg=t["bg"], fg=t["dim"], cursor="hand2",
                       font=("Segoe UI Symbol", size))
        lbl.bind("<Button-1>", lambda e: command())
        lbl.bind("<Enter>", lambda e: lbl.config(fg=hover or t["fg"]))
        lbl.bind("<Leave>", lambda e: lbl.config(fg=t["dim"]))
        return lbl

    def _label(self, font, fg, anchor="w"):
        return tk.Label(self.ui, bg=self.t["bg"], fg=fg, font=font, anchor=anchor)

    def _build_floating(self):
        s, t, ui = self.s, self.t, self.ui
        self.W, self.H = s(430), s(106)
        self.cover_size = s(82)
        self.cover = tk.Canvas(ui, width=self.cover_size, height=self.cover_size,
                               bg=t["bg"], highlightthickness=0)
        self.cover.place(x=s(12), y=s(12))

        tx = s(106)
        self.text_w = self.W - tx - s(60)
        self.title_font, self.sub_font = self.f_title, self.f_sub
        self.lbl_title = self._label(self.f_title, t["fg"])
        self.lbl_title.place(x=tx, y=s(8), width=self.text_w, height=s(22))
        self.lbl_sub = self._label(self.f_sub, t["dim"])
        self.lbl_sub.place(x=tx, y=s(30), width=self.text_w, height=s(18))

        gear = self._text_button("⚙", self.open_settings, 12)
        gear.place(relx=1.0, x=-s(34), y=s(5), anchor="ne")
        close = self._text_button("✕", self.quit, 9, hover=t["danger"])
        close.place(relx=1.0, x=-s(10), y=s(5), anchor="ne")

        by = s(52)
        self.btn_prev = IconButton(ui, "prev", s(40), s(30), t,
                                   lambda: self.backend.send("prev"))
        self.btn_play = IconButton(ui, "play", s(40), s(30), t,
                                   lambda: self.backend.send("toggle"), circle=True)
        self.btn_next = IconButton(ui, "next", s(40), s(30), t,
                                   lambda: self.backend.send("next"))
        self.btn_prev.place(x=tx, y=by)
        self.btn_play.place(x=tx + s(44), y=by)
        self.btn_next.place(x=tx + s(88), y=by)

        self.speaker = SpeakerIcon(ui, t, s(26), s(30), self.volume.toggle_mute)
        self.speaker.place(x=s(246), y=by)
        self.vol_slider = Slider(ui, s(142), s(30), t, t["bg"],
                                 on_change=self.volume.set_level,
                                 thickness=s(4), knob_radius=s(6))
        self.vol_slider.canvas.place(x=s(274), y=by)

        ry = s(86)
        label_w = s(38)
        self.lbl_elapsed = self._label(self.f_small, t["dim"], "w")
        self.lbl_elapsed.place(x=tx, y=ry, width=label_w, height=s(16))
        self.lbl_total = self._label(self.f_small, t["dim"], "e")
        self.lbl_total.place(x=self.W - s(14) - label_w, y=ry, width=label_w, height=s(16))
        sx = tx + label_w + s(2)
        sw = (self.W - s(14) - label_w - s(4)) - sx
        self.seek = Slider(ui, sw, s(16), t, t["bg"], on_change=self._on_seek_drag,
                           on_commit=self._on_seek_commit, thickness=s(4), knob_radius=s(5))
        self.seek.canvas.place(x=sx, y=ry)

        for widget in (ui, self.cover, self.lbl_title, self.lbl_sub):
            self._bind_drag(widget)

    def _build_docked(self, info):
        s, t, ui = self.s, self.t, self.ui
        tb_h = info["bottom"] - info["top"]
        bar_h = tb_h - 2 * s(2)
        prog_h = s(6)
        body_h = bar_h - prog_h - s(1)
        cov = body_h - s(2)
        self.cover_size = cov
        self.cover = tk.Canvas(ui, width=cov, height=cov, bg=t["bg"], highlightthickness=0)
        self.cover.place(x=s(4), y=s(1))

        tx = s(4) + cov + s(8)
        self.text_w = s(150)
        self.title_font, self.sub_font = self.f_dtitle, self.f_dsub
        line_h = body_h // 2
        self.lbl_title = self._label(self.f_dtitle, t["fg"])
        self.lbl_title.place(x=tx, y=0, width=self.text_w, height=line_h)
        self.lbl_sub = self._label(self.f_dsub, t["dim"])
        self.lbl_sub.place(x=tx, y=line_h, width=self.text_w, height=line_h)

        bx = tx + self.text_w + s(4)
        btn_h, by = body_h - s(2), s(1)
        self.btn_prev = IconButton(ui, "prev", s(28), btn_h, t,
                                   lambda: self.backend.send("prev"))
        self.btn_play = IconButton(ui, "play", s(34), btn_h, t,
                                   lambda: self.backend.send("toggle"), circle=True)
        self.btn_next = IconButton(ui, "next", s(28), btn_h, t,
                                   lambda: self.backend.send("next"))
        self.btn_prev.place(x=bx, y=by)
        self.btn_play.place(x=bx + s(28), y=by)
        self.btn_next.place(x=bx + s(62), y=by)
        gear = self._text_button("⚙", self.open_settings, 11)
        gear.place(x=bx + s(94), y=by, width=s(22), height=btn_h)

        self.W = bx + s(94) + s(22) + s(4)
        self.H = bar_h
        inner_w = self.W - 2  # descontando a borda de 1 px
        self.seek = Slider(ui, inner_w, prog_h, t, t["bg"], on_change=self._on_seek_drag,
                           on_commit=self._on_seek_commit, knob=False, thickness=s(2))
        self.seek.canvas.place(x=0, y=self.H - 2 - prog_h)

    # ---- posição da janela ------------------------------------------------- #
    def _compute_xy(self):
        s = self.s
        if self.docked:
            info = self._info
            y = info["top"] + s(2)
            offset = s(int(self.cfg.get("dock_offset", 0) or 0))
            if self.cfg.get("dock_side") == "left":
                x = info["left"] + s(6) + offset
            else:
                anchor = info["notify_left"] or (info["right"] - s(280))
                x = anchor - self.W - s(6) - offset
            return max(info["left"], min(x, info["right"] - self.W)), y
        vl, vt, vr, vb = virtual_screen()
        x, y = self.cfg.get("x"), self.cfg.get("y")
        if (isinstance(x, int) and isinstance(y, int)
                and vl <= x <= vr - self.W and vt <= y <= vb - self.H):
            return x, y
        _, _, right, bottom = work_area()
        return right - self.W - s(12), bottom - self.H - s(12)

    def _apply_window(self):
        x, y = self._compute_xy()
        self.root.geometry(f"{self.W}x{self.H}+{x}+{y}")
        self.root.attributes("-alpha", TRANSLUCENT_ALPHA if self.cfg["translucent"] else 1.0)
        self.root.attributes("-topmost", True)
        self.root.update_idletasks()
        round_corners(self.root)

    def _reposition(self):
        if self.docked:
            x, y = self._compute_xy()
            self.root.geometry(f"+{x}+{y}")

    # ---- arrastar / menu --------------------------------------------------- #
    def _bind_drag(self, widget):
        widget.bind("<ButtonPress-1>", self._drag_start, add="+")
        widget.bind("<B1-Motion>", self._drag_move, add="+")
        widget.bind("<ButtonRelease-1>", self._drag_end, add="+")

    def _drag_start(self, event):
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag_move(self, event):
        self.root.geometry(f"+{event.x_root - self._drag[0]}+{event.y_root - self._drag[1]}")

    def _drag_end(self, event):
        self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
        save_config(self.cfg)

    def _bind_menu(self, widget):
        widget.bind("<Button-3>", self._popup_menu, add="+")
        widget.bind("<Button-2>", lambda e: self.volume.toggle_mute(), add="+")
        for child in widget.winfo_children():
            self._bind_menu(child)

    def _popup_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _configure_menu(self):
        t = self.t
        self.menu.delete(0, "end")
        self.menu.configure(bg=t["bg"], fg=t["fg"], activebackground=t["accent"],
                            activeforeground=t["on_accent"])
        self.menu.add_command(label="Configurações", command=self.open_settings)
        self.menu.add_command(label="Silenciar / reativar som", command=self.volume.toggle_mute)
        self.menu.add_separator()
        self.menu.add_command(label="Fechar", command=self.quit)

    # ---- eventos de progresso e volume ------------------------------------- #
    def _on_seek_drag(self, value):
        duration = self.backend.snapshot()["duration"]
        if self.lbl_elapsed is not None:
            self.lbl_elapsed.config(text=fmt_time(value * duration))

    def _on_seek_commit(self, value):
        duration = self.backend.snapshot()["duration"]
        if duration <= 0:
            return
        target = value * duration
        self.backend.send("seek", target)
        self._seek_hold = (time.time() + 1.5, target)

    def _on_wheel(self, event):
        try:
            if event.widget.winfo_toplevel() is not self.root:
                return
        except Exception:
            return
        level = self.volume.level
        if level is None:
            return
        level = clamp(level + (0.05 if event.delta > 0 else -0.05))
        self.volume.set_level(level)
        self._flash = (f"Volume {round(level * 100)}%", time.time() + 1.2)

    # ---- atualização periódica --------------------------------------------- #
    def refresh(self):
        try:
            self._refresh()
        except Exception as exc:
            if self._closing:
                return
            print("erro na atualização:", exc)
        if not self._closing:
            try:
                self.root.after(400, self.refresh)
            except tk.TclError:
                pass

    def _set_text(self, key, label, text, font=None):
        if self._shown.get(key) != text:
            self._shown[key] = text
            label.config(text=fit_text(text, font, self.text_w) if font else text)

    def _refresh(self):
        st = self.backend.snapshot()
        now = time.time()

        if st["active"]:
            title = st["title"] or "Sem título"
            if self.docked:
                sub = st["artist"] or st["app"]
            else:
                sub = " · ".join(p for p in (st["artist"], st["app"]) if p)
        else:
            title = "Nenhuma mídia tocando"
            sub = "Dê play em um vídeo ou música"
        if self._flash and now < self._flash[1]:
            sub = self._flash[0]
        self._set_text("title", self.lbl_title, title, self.title_font)
        self._set_text("sub", self.lbl_sub, sub, self.sub_font)

        if st["thumb"] is not self._shown_thumb:
            self._shown_thumb = st["thumb"]
            self._draw_cover(st["thumb"])

        self.btn_play.set_kind("pause" if st["playing"] else "play")

        duration, position = st["duration"], st["position"]
        if now < self._seek_hold[0]:
            position = self._seek_hold[1]
        self.seek.set(position / duration if duration > 0 else 0.0,
                      st["active"] and duration > 0)
        if self.lbl_elapsed is not None and not self.seek.dragging:
            self._set_text("elapsed", self.lbl_elapsed, fmt_time(position))
        if self.lbl_total is not None:
            self._set_text("total", self.lbl_total,
                           fmt_time(duration) if duration > 0 else "--:--")

        level = self.volume.level
        if self.speaker is not None:
            self.speaker.update_state(level, self.volume.muted)
        if self.vol_slider is not None:
            self.vol_slider.set(level if level is not None else 0.0, level is not None)

        self._cycle += 1
        if self._cycle % 2 == 0:
            self._maintain()

    def _maintain(self):
        hwnd = toplevel_hwnd(self.root)
        fullscreen = foreground_fullscreen(hwnd)
        if fullscreen != self._fs_hidden:
            self._fs_hidden = fullscreen
            if fullscreen:
                self.root.withdraw()
            else:
                self.root.deiconify()
                self.root.attributes("-topmost", True)
        if fullscreen:
            return
        if self.cfg["mode"] == "docked":
            if info_signature(taskbar_info()) != self._dock_sig:
                self.rebuild()  # barra de tarefas mudou (ou apareceu depois do boot)
                return
            keep_on_top(hwnd)  # a barra de tarefas também é "topmost"

    def _draw_cover(self, data):
        t, c = self.t, self.cover_size
        self.cover.delete("all")
        self._cover_photo = None
        if data:
            try:
                img = ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGB"), (c, c))
                self._cover_photo = ImageTk.PhotoImage(img)
                self.cover.create_image(0, 0, anchor="nw", image=self._cover_photo)
                return
            except Exception:
                pass
        self.cover.create_rectangle(0, 0, c, c, fill=t["panel"], outline="")
        self.cover.create_text(c // 2, c // 2, text="♪", fill=t["cover_fg"],
                               font=("Segoe UI Symbol", max(8, int(c / (self.k * 4)))))

    # ---- configurações ----------------------------------------------------- #
    def set_option(self, key, value):
        self.cfg[key] = value
        save_config(self.cfg)
        if key == "translucent":
            self.root.attributes("-alpha", TRANSLUCENT_ALPHA if value else 1.0)
        else:
            self.root.after(10, self.rebuild)

    def open_settings(self):
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self.settings_win.deiconify()
            self.settings_win.lift()
            return
        win = tk.Toplevel(self.root)
        win.title(f"Configurações · {APP_NAME}")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self.close_settings)
        self.settings_win = win
        self._build_settings()
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        vl, vt, vr, vb = virtual_screen()
        x = self.root.winfo_x() + self.root.winfo_width() - w
        y = self.root.winfo_y() - h - self.s(12)
        win.geometry(f"+{max(vl, min(x, vr - w))}+{max(vt, min(y, vb - h))}")

    def close_settings(self):
        if self.settings_win is not None:
            self.settings_win.destroy()
        self.settings_win = None

    def _row(self, parent, title, subtitle=None):
        t, s = self.t, self.s
        row = tk.Frame(parent, bg=t["bg"])
        row.pack(fill="x", pady=(0, s(12)))
        left = tk.Frame(row, bg=t["bg"])
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=title, bg=t["bg"], fg=t["fg"], anchor="w",
                 font=("Segoe UI Semibold", 10)).pack(anchor="w")
        if subtitle:
            tk.Label(left, text=subtitle, bg=t["bg"], fg=t["dim"], anchor="w",
                     justify="left", wraplength=s(230), font=self.f_small).pack(anchor="w")
        right = tk.Frame(row, bg=t["bg"])
        right.pack(side="right", padx=(s(16), 0))
        return right

    def _build_settings(self):
        win, t, s = self.settings_win, self.t, self.s
        for child in win.winfo_children():
            child.destroy()
        win.configure(bg=t["bg"])
        body = tk.Frame(win, bg=t["bg"])
        body.pack(fill="both", expand=True, padx=s(20), pady=s(18))
        tk.Label(body, text="Configurações", bg=t["bg"], fg=t["fg"], anchor="w",
                 font=("Segoe UI Semibold", 14)).pack(anchor="w", pady=(0, s(14)))

        right = self._row(body, "Tema")
        Segmented(right, t, [("Escuro", "dark"), ("Claro", "light")], self.cfg["theme"],
                  lambda v: self.set_option("theme", v), s).pack()

        right = self._row(body, "Barra translúcida", "Deixa a janela levemente transparente")
        Toggle(right, t, t["bg"], self.cfg["translucent"],
               lambda v: self.set_option("translucent", v), self.k).pack()

        right = self._row(body, "Posição",
                          "Flutuante: arraste para onde quiser.\n"
                          "Na barra de tarefas: fica sobre a barra, perto dos ícones.")
        Segmented(right, t, [("Flutuante", "floating"), ("Na barra de tarefas", "docked")],
                  self.cfg["mode"], lambda v: self.set_option("mode", v), s).pack()

        right = self._row(body, "Lado na barra de tarefas", "Vale só para o modo fixado")
        Segmented(right, t, [("Esquerda", "left"), ("Direita", "right")],
                  self.cfg["dock_side"], lambda v: self.set_option("dock_side", v), s).pack()

        right = self._row(body, "Deslocamento",
                          "Afasta do canto para ficar ao lado dos ícones (modo fixado)")
        offset_lbl = tk.Label(right, text=f"{int(self.cfg['dock_offset'])} px", bg=t["bg"],
                              fg=t["dim"], font=self.f_small, width=7, anchor="e")

        def on_offset(value):
            self.cfg["dock_offset"] = int(value * MAX_DOCK_OFFSET)
            offset_lbl.config(text=f"{self.cfg['dock_offset']} px")
            self._reposition()

        offset = Slider(right, s(150), s(22), t, t["bg"], on_change=on_offset,
                        on_commit=lambda v: save_config(self.cfg), thickness=s(4),
                        knob_radius=s(6), value=clamp(self.cfg["dock_offset"] / MAX_DOCK_OFFSET))
        offset.canvas.pack(side="left")
        offset_lbl.pack(side="left")

        right = self._row(body, "Iniciar com o Windows")
        Toggle(right, t, t["bg"], autostart_enabled(), set_autostart, self.k).pack()

        if self.cfg["mode"] == "docked" and not self.docked:
            tk.Label(body, text="Não encontrei uma barra de tarefas horizontal para fixar; "
                                "usando o modo flutuante por enquanto.",
                     bg=t["bg"], fg=t["danger"], font=self.f_small, justify="left",
                     wraplength=s(360), anchor="w").pack(anchor="w", pady=(0, s(8)))
        tk.Label(body, text="Dica: role a roda do mouse sobre o player para mudar o volume; "
                            "botão do meio ou clique no ícone de som = mudo.",
                 bg=t["bg"], fg=t["dim"], font=self.f_small, justify="left",
                 wraplength=s(360), anchor="w").pack(anchor="w")
        set_dark_titlebar(win, self.cfg["theme"] == "dark")


if __name__ == "__main__":
    if already_running():
        sys.exit(0)
    Miniplayer().run()
