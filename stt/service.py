"""
Servicio de dictado: orquesta grabación, transcripción en streaming, post-proceso y sink.

No sabe nada de sockets, teclado ni interfaz. Quien quiera observarlo (el overlay)
se suscribe con `listener(event, payload)`; los eventos son:

    state   {"state": "loading" | "idle" | "recording" | "processing"}
    partial {"committed": str, "tail": str}
    final   {"text": str}
    error   {"message": str}

El listener se llama desde hilos de trabajo, así que debe ser thread-safe.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable

from config import Config, SAMPLE_RATE
from stt.audio import Recorder
from stt.engines import build_engine
from stt.postprocess import PostProcessor
from stt.sinks import build_sink
from stt.streaming import StreamingTranscriber

MIN_SECONDS = 0.3  # por debajo de esto es un toque accidental de la tecla


class DictationService:
    def __init__(self, cfg: Config, listener: Callable[[str, dict], None] | None = None):
        self.cfg = cfg
        # Atributo público: el daemon puede asignarlo después de crear el overlay.
        self.listener = listener
        self.recorder = Recorder(cfg.input_device)
        self.post = PostProcessor(cfg)
        self.sink = build_sink(cfg)
        self.engine = None
        self.state = "loading"
        self._streamer: StreamingTranscriber | None = None
        self._busy = threading.Lock()         # un solo uso del motor a la vez
        self._state_lock = threading.Lock()   # transiciones de estado (socket/hotkey/UI concurrentes)

    # ── eventos ──────────────────────────────────────────────────────────────

    def _emit(self, event: str, payload: dict) -> None:
        listener = self.listener
        if listener is None:
            return
        try:
            listener(event, payload)
        except Exception as e:
            # Un fallo de la UI nunca debe tumbar el dictado, pero tampoco callarse.
            print(f"[service] listener falló en {event!r}: {e!r}", file=sys.stderr, flush=True)

    def _set_state(self, state: str) -> None:
        with self._state_lock:
            self.state = state
        self._emit("state", {"state": state})

    def _fail(self, message: str) -> None:
        print(f"[service] {message}", file=sys.stderr, flush=True)
        self._emit("error", {"message": message})

    # ── ciclo de vida ────────────────────────────────────────────────────────

    def warm(self) -> None:
        self._set_state("loading")
        t0 = time.monotonic()
        print(f"[daemon] cargando {self.cfg.engine}...", flush=True)
        try:
            self.engine = build_engine(self.cfg)
        except Exception as e:
            self._fail(f"no se pudo cargar el modelo: {e!r}")
            raise
        print(f"[daemon] listo en {time.monotonic() - t0:.1f}s", flush=True)
        self._set_state("idle")

    def start(self) -> None:
        with self._state_lock:
            if self.state != "idle":
                return
            try:
                self.recorder.start()
            except Exception as e:
                self._fail(f"no se pudo abrir el micrófono: {e!r}")
                return
            self._streamer = StreamingTranscriber(
                self.engine,
                self.recorder,
                self._busy,
                on_partial=lambda committed, tail: self._emit(
                    "partial", {"committed": committed, "tail": tail}
                ),
            )
            self._streamer.start()
            self.state = "recording"
        self._emit("state", {"state": "recording"})

    def stop(self) -> None:
        with self._state_lock:
            if self.state != "recording":
                return
            self.state = "processing"
            streamer, self._streamer = self._streamer, None
        self._emit("state", {"state": "processing"})
        threading.Thread(target=self._finish, args=(streamer,), daemon=True).start()

    def toggle(self) -> None:
        # Se decide bajo el lock para que dos toggles simultáneos no lean el mismo estado.
        with self._state_lock:
            recording = self.state == "recording"
        if recording:
            self.stop()
        else:
            self.start()

    def _finish(self, streamer: StreamingTranscriber) -> None:
        try:
            audio = self.recorder.stop()
            too_short = audio is None or len(audio) < SAMPLE_RATE * MIN_SECONDS
            if too_short:
                # Hay que terminar igualmente el streamer para que su hilo no quede vivo.
                if audio is None:
                    import numpy as np

                    audio = np.zeros(0, dtype=np.float32)
                streamer.finish(audio)
                print("[daemon] audio demasiado corto, descartado")
                return
            t0 = time.monotonic()
            text = streamer.finish(audio)
            t_asr = time.monotonic() - t0
            if not text:
                print("[daemon] sin texto")
                return
            text = self.post.run(text)
            dur = len(audio) / SAMPLE_RATE
            print(f"[daemon] {dur:.1f}s audio / {t_asr:.1f}s asr → {text!r}")
            self._emit("final", {"text": text})
            self.sink.send(text)
        except Exception as e:
            self._fail(f"error al procesar el dictado: {e!r}")
        finally:
            self._set_state("idle")

    # ── protocolo del socket ─────────────────────────────────────────────────

    def handle(self, cmd: str) -> str:
        if cmd == "ping":
            return "pong"
        if cmd in ("start", "stop", "toggle"):
            if self.state == "loading":
                return "cargando modelo"
            if cmd == "start":
                self.start()
                return "grabando"
            if cmd == "stop":
                self.stop()
                return "procesando"
            self.toggle()
            return "grabando" if self.state == "recording" else "procesando"
        return f"comando desconocido: {cmd}"
