"""
Overlay: barra flotante con botón de micrófono y transcripción en vivo.

Tk vive en el hilo principal. Los hilos de trabajo (servicio, streaming)
llaman a `Overlay.post()`, que solo encola; `_drain` vacía la cola desde el
bucle de Tk, que es el único sitio donde se tocan widgets.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk

WIDTH = 520
HEIGHT = 84
BOTTOM_MARGIN = 56          # px sobre el borde inferior de la pantalla
MAX_CHARS = 140             # ~3 líneas; más allá solo se ve el final
POLL_MS = 50
PULSE_MS = 550

BG = "#1e1e1e"
FG = "#e6e6e6"
FG_DIM = "#8a8a8a"
FG_ERROR = "#e5484d"

BTN_LOADING = "#3a3a3a"
BTN_IDLE = "#5a5a5a"
BTN_REC = "#e5484d"
BTN_REC_DIM = "#b23a3e"
BTN_PROC = "#f5a524"
ICON_FG = "#f2f2f2"
ICON_FG_DISABLED = "#7a7a7a"

STATUS = {
    "loading": "cargando modelo…",
    "idle": "listo",
    "recording": "escuchando…",
    "processing": "procesando…",
}

BTN_SIZE = 40


class Overlay:
    def __init__(self, root: tk.Tk, service) -> None:
        self.root = root
        self.service = service
        self._events: queue.Queue[tuple[str, dict]] = queue.Queue()
        self._state = "loading"
        self._pulse_on = False
        self._pulse_job: str | None = None
        self._committed = ""
        self._tail = ""
        self._last_final = ""
        self._error = ""

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=BG)

        # El texto se pega en la ventana que tenga el foco (Ctrl+Shift+V), así
        # que el overlay no debe robarlo jamás: sin focus()/grab, takefocus=0 y
        # un Canvas como botón. Las ventanas override-redirect no reciben foco
        # de Mutter al hacer clic, por lo que la ventana donde escribes lo conserva.
        frame = tk.Frame(root, bg=BG, takefocus=0)
        frame.pack(fill="both", expand=True, padx=14, pady=12)

        self.canvas = tk.Canvas(
            frame, width=BTN_SIZE, height=BTN_SIZE, bg=BG,
            highlightthickness=0, takefocus=0,
        )
        self.canvas.pack(side="left", padx=(0, 14))
        self.canvas.bind("<ButtonRelease-1>", self._on_click)

        self.text = tk.Text(
            frame, height=3, wrap="word", bg=BG, fg=FG, bd=0,
            highlightthickness=0, padx=0, pady=0, font=("Sans", 11),
            cursor="", insertwidth=0, takefocus=0, state="disabled",
            selectbackground=BG, inactiveselectbackground=BG,
        )
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("committed", foreground=FG)
        self.text.tag_configure("tail", foreground=FG_DIM)
        self.text.tag_configure("error", foreground=FG_ERROR)
        # Un Text deshabilitado no coge foco, pero se cancela igualmente para
        # que la selección o el arrastre no interfieran.
        for seq in ("<Button-1>", "<B1-Motion>", "<Double-Button-1>", "<Triple-Button-1>"):
            self.text.bind(seq, lambda _e: "break")

        self._place()
        self._draw_button()
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
        self._pulse_on = False
        self._draw_button()
        if state == "recording":
            self._schedule_pulse()
        elif self._pulse_job is not None:
            self.root.after_cancel(self._pulse_job)
            self._pulse_job = None

    # ── Botón ──────────────────────────────────────────────────────────────

    def _on_click(self, _event) -> None:
        if self._state not in ("idle", "recording"):
            return
        threading.Thread(target=self._toggle, daemon=True).start()

    def _toggle(self) -> None:
        # toggle() puede bloquear un momento; va fuera del bucle de Tk.
        try:
            self.service.toggle()
        except Exception as e:
            self.post("error", {"message": f"toggle falló: {e}"})
            raise

    def _schedule_pulse(self) -> None:
        def tick() -> None:
            self._pulse_on = not self._pulse_on
            self._draw_button()
            self._pulse_job = self.root.after(PULSE_MS, tick)

        self._pulse_job = self.root.after(PULSE_MS, tick)

    def _draw_button(self) -> None:
        c = self.canvas
        c.delete("all")
        disabled = self._state in ("loading", "processing")
        bg = {
            "loading": BTN_LOADING,
            "idle": BTN_IDLE,
            "recording": BTN_REC_DIM if self._pulse_on else BTN_REC,
            "processing": BTN_PROC,
        }[self._state]
        fg = ICON_FG_DISABLED if self._state == "loading" else ICON_FG
        s = BTN_SIZE
        c.create_oval(1, 1, s - 1, s - 1, fill=bg, outline="")
        # Cápsula del micro: dos óvalos y un rectángulo (Tk no tiene rect redondeado).
        c.create_oval(15, 8, 25, 18, fill=fg, outline="")
        c.create_oval(15, 16, 25, 26, fill=fg, outline="")
        c.create_rectangle(15, 13, 25, 21, fill=fg, outline="")
        # Soporte en U, tallo y base.
        c.create_arc(11, 12, 29, 30, start=180, extent=180, style="arc", outline=fg, width=2)
        c.create_line(20, 30, 20, 33, fill=fg, width=2)
        c.create_line(15, 33, 25, 33, fill=fg, width=2, capstyle="round")
        c.configure(cursor="" if disabled else "hand2")

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
