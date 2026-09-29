from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

from config import Config


# ─────────────────────────────────────────────────────────────────────────────
# Sinks: dónde acaba el texto
# ─────────────────────────────────────────────────────────────────────────────

class ClipboardSink:
    """Solo copia. Tú pegas."""

    def send(self, text: str) -> None:
        _copy(text)
        print("[sink] copiado al portapapeles")


class TypeSink:
    """Escribe en la ventana con foco. Es el modo por defecto."""

    def send(self, text: str) -> None:
        if sys.platform == "darwin":
            prev = _paste_buffer()
            _copy(text)
            subprocess.run([
                "osascript", "-e",
                'tell application "System Events" to keystroke "v" using command down',
            ], check=False)
            time.sleep(0.25)
            if prev is not None:
                _copy(prev)
        elif shutil.which("ydotool"):
            # ydotool "type" usa códigos de tecla fijos (asume layout US): con layout
            # es u otros no-US destroza símbolos y no soporta acentos/eñes. Copiamos
            # y pegamos con Ctrl+V, que sí es independiente del layout.
            prev = _paste_buffer()
            _copy(text)
            subprocess.run(
                ["ydotool", "key", "29:1", "42:1", "47:1", "47:0", "42:0", "29:0"],  # Ctrl+Shift+V
                check=False,
            )
            time.sleep(0.25)
            if prev is not None:
                _copy(prev)
        elif shutil.which("wtype"):                       # Wayland (wlroots)
            subprocess.run(["wtype", "--", text], check=False)
        elif shutil.which("xdotool"):                     # X11
            subprocess.run(
                ["xdotool", "type", "--clearmodifiers", "--delay", "8", "--", text],
                check=False,
            )
        else:
            ClipboardSink().send(text)


class TmuxSink:
    """Inyecta el texto en un panel de tmux sin depender del foco de ventana."""

    def __init__(self, target: str = ""):
        self.target = target

    def send(self, text: str) -> None:
        cmd = ["tmux", "send-keys"]
        if self.target:
            cmd += ["-t", self.target]
        cmd += ["-l", text]        # -l = literal, y NO mandamos Enter: revisas antes
        subprocess.run(cmd, check=False)


class OpenCodeSink:
    """Envía directamente al servidor HTTP de opencode (`opencode serve`).

    EXPERIMENTAL: el esquema exacto del body puede variar entre versiones.
    Comprueba la spec OpenAPI en GET {opencode_url}/doc antes de fiarte.
    """

    def __init__(self, cfg: Config):
        self.url = cfg.opencode_url.rstrip("/")
        self.directory = cfg.opencode_directory or os.getcwd()
        self.session_id: str | None = None

    def _post(self, path: str, body: dict) -> dict:
        import urllib.request

        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")

    def send(self, text: str) -> None:
        if self.session_id is None:
            sess = self._post("/session", {"title": "dictado", "location": {"directory": self.directory}})
            self.session_id = sess.get("id")
        self._post(
            f"/session/{self.session_id}/prompt",
            {"parts": [{"type": "text", "text": text}]},
        )
        print(f"[sink] enviado a opencode (sesión {self.session_id})")


def build_sink(cfg: Config):
    return {
        "clipboard": lambda: ClipboardSink(),
        "tmux": lambda: TmuxSink(cfg.tmux_target),
        "opencode": lambda: OpenCodeSink(cfg),
    }.get(cfg.sink, lambda: TypeSink())()


def _copy(text: str) -> None:
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, input=text.encode(), check=False)
            return


def _paste_buffer() -> str | None:
    for cmd in (["pbpaste"], ["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"]):
        if shutil.which(cmd[0]):
            r = subprocess.run(cmd, capture_output=True, check=False)
            return r.stdout.decode(errors="replace")
    return None
