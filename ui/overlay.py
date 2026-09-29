"""
Overlay: barra flotante con botón de micrófono, onda de voz y transcripción en vivo.

Tk vive en el hilo principal. Los hilos de trabajo (servicio, streaming)
llaman a `Overlay.post()`, que solo encola; `_drain` vacía la cola desde el
bucle de Tk, que es el único sitio donde se tocan widgets.
"""

from __future__ import annotations

import math
import queue
import threading
import time
import tkinter as tk
from collections import deque

import numpy as np

# python-xlib llega con pynput (su backend X11); Tk no sabe dar forma a la ventana.
from Xlib import display as xdisplay
from Xlib.ext import shape

WIDTH = 560
HEIGHT = 96
BOTTOM_MARGIN = 56          # px sobre el borde inferior de la pantalla
MAX_CHARS = 100             # ~2 líneas; más allá solo se ve el final
POLL_MS = 50
FRAME_MS = 33               # ~30 fps para onda, anillos y spinner
# Tk solo sabe atenuar la ventana entera (texto incluido), no el fondo ni
# desenfocar lo de detrás; 0.93 insinúa lo de debajo sin restar lectura.
ALPHA = 0.93
CORNER_R = 28               # radio de las esquinas recortadas con SHAPE

BG = "#121215"
FG = "#f4f4f5"
FG_DIM = "#8b8b8f"
FG_ERROR = "#ff8a8d"
ACCENT = "#e5484d"
AMBER = "#f5a524"
ICON_DARK = "#141416"

STATUS = {
    "loading": "cargando modelo…",
    "idle": "pulsa el micro para dictar",
    "recording": "escuchando…",
    "processing": "procesando…",
}
META = {
    "loading": "CARGANDO",
    "idle": "LISTO",
    "processing": "PROCESANDO",
}

BTN_BOX = 80                # lienzo del botón: deja sitio a los anillos
BTN_R = 28
RING_MS = 1800
SPIN_MS = 900

WAVE_H = 20
BARS = 36
BAR_W = 3
BAR_EVERY = 2               # frames por barra nueva: ~2.4 s de historia visible
# RMS en dBFS: -50 dB es silencio de habitación, -12 dB voz fuerte cerca del micro.
DB_FLOOR = -50.0
DB_CEIL = -12.0


def _mix(a: str, b: str, t: float) -> str:
    """Color entre `a` (t=0) y `b` (t=1): Tk no tiene alfa por item."""
    t = min(max(t, 0.0), 1.0)
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


# ── Rasterizado del botón ────────────────────────────────────────────────────
# El Canvas de Tk no suaviza bordes, así que el botón se pinta como imagen: cada
# forma es una distancia con signo al borde y la cobertura de cada píxel sale de
# ella, lo que da un antialias exacto sin supermuestreo.

_PX = np.arange(BTN_BOX, dtype=np.float32) + 0.5
_DX, _DY = np.meshgrid(_PX - BTN_BOX / 2, _PX - BTN_BOX / 2)
_DIST = np.hypot(_DX, _DY)
_PPM_HEADER = f"P6 {BTN_BOX} {BTN_BOX} 255\n".encode()


def _rgb(color: str) -> np.ndarray:
    return np.array([int(color[i:i + 2], 16) for i in (1, 3, 5)], dtype=np.float32)


def _cover(d: np.ndarray) -> np.ndarray:
    return np.clip(0.5 - d, 0.0, 1.0)


def _segment(x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
    """Distancia al segmento; restarle media anchura da un trazo con extremos redondos."""
    vx, vy = x1 - x0, y1 - y0
    t = np.clip(((_DX - x0) * vx + (_DY - y0) * vy) / (vx * vx + vy * vy), 0.0, 1.0)
    return np.hypot(_DX - x0 - t * vx, _DY - y0 - t * vy)


def _arc(cy: float, radius: float, start: float, extent: float) -> np.ndarray:
    """Distancia a un arco centrado en (0, cy), de `start` a `start + extent` radianes."""
    dist = np.hypot(_DX, _DY - cy)
    angle = np.arctan2(_DY - cy, _DX) % (2 * math.pi)
    inside = (angle - start) % (2 * math.pi) <= extent
    ends = [
        np.hypot(_DX - radius * math.cos(a), _DY - cy - radius * math.sin(a))
        for a in (start, start + extent)
    ]
    return np.where(inside, np.abs(dist - radius), np.minimum(*ends))


def _rounded_box(half: float, radius: float) -> np.ndarray:
    qx = np.abs(_DX) - (half - radius)
    qy = np.abs(_DY) - (half - radius)
    outside = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0))
    return outside + np.minimum(np.maximum(qx, qy), 0) - radius


class Overlay:
    def __init__(self, root: tk.Tk, service) -> None:
        self.root = root
        self.service = service
        self._events: queue.Queue[tuple[str, dict]] = queue.Queue()
        self._state = "loading"
        self._committed = ""
        self._tail = ""
        self._last_final = ""
        self._error = ""
        self._hover = False
        self._anim_job: str | None = None
        self._anim_t0 = 0.0
        self._rec_t0 = 0.0
        self._frame = 0
        self._env = 0.0
        self._levels: deque[float] = deque([0.0] * BARS, maxlen=BARS)

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=BG)
        # -alpha y la forma se pierden si se ponen antes de mapear la ventana.
        root.bind("<Map>", self._on_map)

        # El texto se pega en la ventana que tenga el foco (Ctrl+Shift+V), así
        # que el overlay no debe robarlo jamás: sin focus()/grab, takefocus=0 y
        # un Canvas como botón. Las ventanas override-redirect no reciben foco
        # de Mutter al hacer clic, por lo que la ventana donde escribes lo conserva.
        # Sin borde: un borde rectangular quedaría cortado en las esquinas redondeadas.
        frame = tk.Frame(root, bg=BG, takefocus=0)
        frame.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(
            frame, width=BTN_BOX, height=BTN_BOX, bg=BG,
            highlightthickness=0, takefocus=0,
        )
        self.canvas.pack(side="left", padx=(8, 10))
        self._btn_img = tk.PhotoImage(width=BTN_BOX, height=BTN_BOX)
        self.canvas.create_image(0, 0, anchor="nw", image=self._btn_img)
        self.canvas.bind("<ButtonRelease-1>", self._on_click)
        self.canvas.bind("<Enter>", lambda _e: self._set_hover(True))
        self.canvas.bind("<Leave>", lambda _e: self._set_hover(False))

        col = tk.Frame(frame, bg=BG, takefocus=0)
        col.pack(side="left", fill="both", expand=True, padx=(0, 22), pady=(16, 12))

        top = tk.Frame(col, bg=BG, takefocus=0)
        top.pack(side="top", fill="x")
        self.meta = tk.Label(
            top, bg=BG, fg=FG_DIM, font=("Monospace", 8, "bold"),
            width=10, anchor="e", takefocus=0,
        )
        self.meta.pack(side="right", padx=(14, 0))
        self.wave = tk.Canvas(top, height=WAVE_H, bg=BG, highlightthickness=0, takefocus=0)
        self.wave.pack(side="left", fill="x", expand=True)
        self.wave.bind("<Configure>", lambda _e: self._draw_wave())

        self.text = tk.Text(
            col, height=2, wrap="word", bg=BG, fg=FG, bd=0,
            highlightthickness=0, padx=0, pady=0, font=("Sans", 11),
            cursor="", insertwidth=0, takefocus=0, state="disabled",
            selectbackground=BG, inactiveselectbackground=BG,
        )
        self.text.pack(side="top", fill="both", expand=True, pady=(8, 0))
        self.text.tag_configure("committed", foreground=FG)
        self.text.tag_configure("tail", foreground=FG_DIM)
        self.text.tag_configure("error", foreground=FG_ERROR)
        # Un Text deshabilitado no coge foco, pero se cancela igualmente para
        # que la selección o el arrastre no interfieran.
        for seq in ("<Button-1>", "<B1-Motion>", "<Double-Button-1>", "<Triple-Button-1>"):
            self.text.bind(seq, lambda _e: "break")

        self._place()
        self._draw()
        self._render()
        root.after(POLL_MS, self._drain)

    # ── API thread-safe ────────────────────────────────────────────────────

    def post(self, event: str, payload: dict) -> None:
        self._events.put((event, payload))

    # ── Ventana ────────────────────────────────────────────────────────────

    def _place(self) -> None:
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - WIDTH) // 2
        y = self.root.winfo_screenheight() - HEIGHT - BOTTOM_MARGIN
        self.root.geometry(f"{WIDTH}x{HEIGHT}+{x}+{y}")

    def _on_map(self, event) -> None:
        # <Map> del toplevel lo reciben también sus hijos por bindtags.
        if event.widget is not self.root:
            return
        self.root.attributes("-alpha", ALPHA)
        self._round_corners()

    def _round_corners(self) -> None:
        """Recorta la ventana a un rectángulo redondeado con la extensión SHAPE de X."""
        d = xdisplay.Display()
        try:
            if not d.has_extension("SHAPE"):
                raise RuntimeError("el servidor X no tiene la extensión SHAPE")
            # La forma va en la ventana hija de la raíz (el wrapper de Tk),
            # no en la interna que devuelve winfo_id().
            win = d.create_resource_object("window", self.root.winfo_id())
            while True:
                parent = win.query_tree().parent
                if parent.id == d.screen().root.id:
                    break
                win = parent
            w, h, r = WIDTH, HEIGHT, CORNER_R
            mask = win.create_pixmap(w, h, 1)
            gc = mask.create_gc(foreground=0, background=0)
            mask.fill_rectangle(gc, 0, 0, w, h)
            gc.change(foreground=1)
            mask.fill_rectangle(gc, r, 0, w - 2 * r, h)
            mask.fill_rectangle(gc, 0, r, w, h - 2 * r)
            for x, y in ((0, 0), (w - 2 * r, 0), (0, h - 2 * r), (w - 2 * r, h - 2 * r)):
                mask.fill_arc(gc, x, y, 2 * r, 2 * r, 0, 360 * 64)
            win.shape_mask(shape.SO.Set, shape.SK.Bounding, 0, 0, mask)
            gc.free()
            mask.free()
            d.sync()
        finally:
            d.close()

    # ── Cola de eventos (hilo de Tk) ───────────────────────────────────────

    def _drain(self) -> None:
        # Reprogramar primero: si un evento revienta, el traceback sale por
        # stderr pero el overlay sigue vivo en vez de congelarse.
        self.root.after(POLL_MS, self._drain)
        dirty = False
        try:
            while True:
                event, payload = self._events.get_nowait()
                self._handle(event, payload)
                dirty = True
        except queue.Empty:
            pass
        if dirty:
            self._render()

    def _handle(self, event: str, payload: dict) -> None:
        if event == "state":
            self._set_state(payload["state"])
        elif event == "partial":
            self._committed = payload.get("committed", "")
            self._tail = payload.get("tail", "")
        elif event == "final":
            self._last_final = payload.get("text", "")
            self._committed = self._tail = ""
        elif event == "error":
            self._error = payload.get("message", "error")
            self._draw_meta()
        else:
            raise ValueError(f"evento de overlay desconocido: {event!r}")

    def _set_state(self, state: str) -> None:
        if state not in STATUS:
            raise ValueError(f"estado de overlay desconocido: {state!r}")
        self._state = state
        if state == "recording":
            # El error dura hasta el siguiente dictado: el servicio pasa a idle
            # justo después de emitirlo y, si se borrase ahí, nunca se vería.
            self._error = ""
            self._committed = self._tail = ""
            self._last_final = ""
            self._rec_t0 = time.monotonic()
            self._env = 0.0
            self._levels.extend([0.0] * BARS)
        if self._anim_job is not None:
            self.root.after_cancel(self._anim_job)
            self._anim_job = None
        if state in ("recording", "processing"):
            self._anim_t0 = time.monotonic()
            self._frame = 0
            self._anim_job = self.root.after(FRAME_MS, self._animate)
        self._draw()

    # ── Animación ──────────────────────────────────────────────────────────

    def _animate(self) -> None:
        self._anim_job = self.root.after(FRAME_MS, self._animate)
        if self._state == "recording":
            self._sample_level()
        self._frame += 1
        self._draw()

    def _sample_level(self) -> None:
        rms = self.service.recorder.level
        db = 20 * math.log10(rms + 1e-9)
        norm = min(max((db - DB_FLOOR) / (DB_CEIL - DB_FLOOR), 0.0), 1.0)
        # Subida inmediata y caída suave: sin esto las barras parpadean entre sílabas.
        self._env = norm if norm > self._env else self._env * 0.8 + norm * 0.2
        if self._frame % BAR_EVERY == 0:
            self._levels.append(self._env)

    def _draw(self) -> None:
        self._draw_button()
        self._draw_wave()
        self._draw_meta()

    # ── Botón ──────────────────────────────────────────────────────────────

    def _clickable(self) -> bool:
        return self._state in ("idle", "recording")

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        self._draw_button()

    def _on_click(self, _event) -> None:
        if not self._clickable():
            return
        threading.Thread(target=self._toggle, daemon=True).start()

    def _toggle(self) -> None:
        # toggle() puede bloquear un momento; va fuera del bucle de Tk.
        try:
            self.service.toggle()
        except Exception as e:
            self.post("error", {"message": f"toggle falló: {e}"})
            raise

    def _draw_button(self) -> None:
        elapsed_ms = (time.monotonic() - self._anim_t0) * 1000
        state = self._state
        r = BTN_R + (1.5 if self._hover and self._clickable() else 0)
        img = np.empty((BTN_BOX, BTN_BOX, 3), dtype=np.float32)
        img[:] = _rgb(BG)

        def paint(cover: np.ndarray, color: str, alpha: float = 1.0) -> None:
            a = (cover * alpha)[..., None]
            img[:] = img * (1 - a) + _rgb(color) * a

        if state == "recording":
            # Dos anillos desfasados medio ciclo que se expanden y se desvanecen.
            for offset in (0.0, 0.5):
                t = (elapsed_ms / RING_MS + offset) % 1.0
                rr = BTN_R * (1 + 0.42 * t)
                paint(_cover(np.abs(_DIST - rr) - 1.0), ACCENT, 0.55 * (1 - t))
            # El botón respira con la voz.
            r += 3 * self._env
            bg, fg = ACCENT, "#ffffff"
        elif state == "processing":
            bg, fg = _mix(BG, AMBER, 0.14), AMBER
        elif state == "loading":
            bg, fg = _mix(BG, FG, 0.08), _mix(BG, FG, 0.35)
        else:
            bg, fg = FG if not self._hover else "#ffffff", ICON_DARK

        paint(_cover(_DIST - r), bg)

        if state == "recording":
            paint(_cover(_rounded_box(7.0, 2.5)), fg)
        elif state == "processing":
            paint(_cover(np.abs(_DIST - 10) - 1.25), AMBER, 0.3)
            start = (elapsed_ms / SPIN_MS * 2 * math.pi) % (2 * math.pi)
            paint(_cover(_arc(0.0, 10.0, start, math.radians(100)) - 1.25), fg)
        else:
            # Cápsula del micro, soporte en U y tallo.
            paint(_cover(_segment(0, -6, 0, 0) - 3.0), fg)
            paint(_cover(_arc(-1.0, 6.5, 0.0, math.pi) - 1.0), fg)
            paint(_cover(_segment(0, 5.5, 0, 9) - 1.0), fg)

        data = np.clip(img + 0.5, 0, 255).astype(np.uint8).tobytes()
        self._btn_img.put(_PPM_HEADER + data)
        self.canvas.configure(cursor="hand2" if self._clickable() else "")

    # ── Onda y estado ──────────────────────────────────────────────────────

    def _draw_wave(self) -> None:
        w = self.wave
        w.delete("all")
        width = w.winfo_width()
        if width <= 1:
            return  # aún sin geometría; el <Configure> lo volverá a pedir
        step = (width - BAR_W) / (BARS - 1)
        mid = WAVE_H / 2
        min_h = 2.0
        state = self._state
        phase = (time.monotonic() - self._anim_t0) * 1000 / 1400

        for i in range(BARS):
            if state == "recording":
                v = self._levels[i]
                h = min_h + v * (WAVE_H - BAR_W - min_h)
                color = _mix(BG, FG, 0.4 + 0.55 * v)
            elif state == "processing":
                # Barrido de izquierda a derecha mientras el motor termina.
                v = 0.5 - 0.5 * math.cos(2 * math.pi * (phase - i * 0.025))
                h = min_h + 4 * v
                color = _mix(BG, FG, 0.16 + 0.5 * v)
            else:
                h = min_h
                color = _mix(BG, FG, 0.14 if state == "loading" else 0.28)
            x = BAR_W / 2 + i * step
            w.create_line(
                x, mid - h / 2, x, mid + h / 2,
                fill=color, width=BAR_W, capstyle="round",
            )

    def _draw_meta(self) -> None:
        if self._state == "recording":
            secs = int(time.monotonic() - self._rec_t0)
            text, color = f"{secs // 60}:{secs % 60:02d}", ACCENT
        elif self._error and self._state == "idle":
            text, color = "ERROR", FG_ERROR
        elif self._state == "processing":
            text, color = META["processing"], AMBER
        else:
            text, color = META[self._state], FG_DIM
        self.meta.configure(text=text, fg=color)

    # ── Texto ──────────────────────────────────────────────────────────────

    def _render(self) -> None:
        parts: list[tuple[str, str]]
        if self._error:
            parts = [(self._error, "error")]
        elif self._state == "loading":
            parts = [(STATUS["loading"], "tail")]
        elif self._state == "processing" and not (self._committed or self._tail):
            parts = [(STATUS["processing"], "tail")]
        elif self._state == "recording" and not (self._committed or self._tail):
            parts = [(STATUS["recording"], "tail")]
        elif self._committed or self._tail:
            parts = self._clip(self._committed, self._tail)
        elif self._last_final:
            parts = self._clip(self._last_final, "", dim=True)
        else:
            parts = [(STATUS["idle"], "tail")]

        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        for chunk, tag in parts:
            t.insert("end", chunk, tag)
        t.configure(state="disabled")

    @staticmethod
    def _clip(committed: str, tail: str, dim: bool = False) -> list[tuple[str, str]]:
        """Conserva solo el final del texto: lo último dicho es lo que importa."""
        ctag = "tail" if dim else "committed"
        excess = len(committed) + len(tail) - MAX_CHARS
        if excess <= 0:
            return [(committed, ctag), (tail, "tail")]
        prefix = "…"
        if excess >= len(committed):
            return [(prefix + tail[excess - len(committed):], "tail")]
        return [(prefix + committed[excess:], ctag), (tail, "tail")]
