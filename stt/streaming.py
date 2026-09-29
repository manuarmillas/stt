"""Transcripción en streaming: segmentos confirmados + cola re-transcrita."""

from __future__ import annotations

import sys
import threading
from typing import Callable

from config import SAMPLE_RATE

MIN_TICK_SECONDS = 0.5   # menos audio que esto no merece una pasada de ASR
MIN_FINAL_SECONDS = 0.3  # igual que el umbral de descarte del dictado normal


def _join(*parts: str) -> str:
    return " ".join(" ".join(parts).split())


class StreamingTranscriber:
    """faster-whisper no hace streaming nativo: cada `interval` s se transcribe el
    audio desde `commit_offset`; los segmentos que terminan al menos `commit_margin`
    s antes del final del buffer se confirman (se fija su texto y se avanza el
    offset) y el resto es la cola inestable que se re-transcribe en el siguiente tick.
    """

    def __init__(
        self,
        engine,
        recorder,
        lock: threading.Lock,
        on_partial: Callable[[str, str], None],
        interval: float = 0.8,
        commit_margin: float = 1.5,
    ):
        self.engine = engine
        self.recorder = recorder
        self.lock = lock
        self.on_partial = on_partial
        self.interval = interval
        self.commit_margin = commit_margin
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._committed = ""
        self._tail = ""
        self._commit_offset = 0

    # Invariante: _committed/_tail/_commit_offset los toca solo el hilo del tick
    # mientras corre; finish() los lee/escribe únicamente después de hacer join.
    def start(self) -> None:
        self._committed = ""
        self._tail = ""
        self._commit_offset = 0
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._tick()
            except Exception as e:
                # Un tick malo no debe matar el streaming: se registra y se sigue.
                print(f"[stream] fallo en tick, sigo: {e!r}", file=sys.stderr)

    def _tick(self) -> None:
        audio = self.recorder.snapshot(self._commit_offset)
        if len(audio) < SAMPLE_RATE * MIN_TICK_SECONDS:
            return
        # Con timeout para que finish() nunca quede esperando detrás de un tick
        # bloqueado por otro poseedor del lock.
        if not self.lock.acquire(timeout=self.interval):
            return
        try:
            segs = self.engine.transcribe_segments(audio)
        finally:
            self.lock.release()
        if self._stop.is_set():
            # finish() ya está en marcha: transcribirá la cola él mismo desde el
            # offset actual, y no queremos avanzarlo bajo sus pies.
            return

        limit = len(audio) / SAMPLE_RATE - self.commit_margin
        n_commit = 0
        for _, end, _ in segs:
            if end > limit:
                break
            n_commit += 1
        if n_commit:
            self._committed = _join(self._committed, *(t for _, _, t in segs[:n_commit]))
            self._commit_offset += int(segs[n_commit - 1][1] * SAMPLE_RATE)
        self._tail = _join(*(t for _, _, t in segs[n_commit:]))
        self.on_partial(self._committed, self._tail)

    def finish(self, audio_total) -> str:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

        rest = audio_total[self._commit_offset:]
        tail = ""
        if len(rest) >= SAMPLE_RATE * MIN_FINAL_SECONDS:
            with self.lock:
                segs = self.engine.transcribe_segments(rest)
            tail = _join(*(t for _, _, t in segs))
        self._tail = tail
        self.on_partial(self._committed, tail)
        return _join(self._committed, tail)
