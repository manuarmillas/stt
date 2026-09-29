"""Captura de audio."""

from __future__ import annotations

import sys
import threading
import time

from config import MAX_SECONDS, SAMPLE_RATE


class Recorder:
    """Graba mono a 16 kHz mientras esté activo. Devuelve float32 en [-1, 1]."""

    def __init__(self, input_device=None):
        self.input_device = input_device
        self._frames: list = []
        self._n_samples = 0
        self._stream = None
        self._lock = threading.Lock()
        # Lock aparte para los frames: el callback no puede tomar `_lock`, porque
        # stop() lo tiene cogido mientras espera a que el stream termine el callback.
        self._frames_lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._stream is not None

    @property
    def sample_count(self) -> int:
        with self._frames_lock:
            return self._n_samples

    def start(self) -> None:
        import sounddevice as sd

        with self._lock:
            if self._stream is not None:
                return
            with self._frames_lock:
                self._frames = []
                self._n_samples = 0
            self._t0 = time.monotonic()

            def callback(indata, frames, time_info, status):
                if status:
                    print(f"[audio] {status}", file=sys.stderr)
                if time.monotonic() - self._t0 > MAX_SECONDS:
                    return
                chunk = indata.copy()
                with self._frames_lock:
                    self._frames.append(chunk)
                    self._n_samples += len(chunk)

            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                device=self.input_device,
                callback=callback,
                blocksize=1024,
            )
            self._stream.start()

    def snapshot(self, from_sample: int):
        """Copia float32 1-D del audio grabado desde `from_sample` hasta ahora."""
        import numpy as np

        # Solo copiamos la referencia de los frames bajo lock; la concatenación
        # (lo caro) se hace fuera para no bloquear el callback de audio.
        with self._frames_lock:
            frames = list(self._frames)
        parts = []
        pos = 0
        for f in frames:
            end = pos + len(f)
            if end > from_sample:
                parts.append(f[max(from_sample - pos, 0):])
            pos = end
        if not parts:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(parts, axis=0).reshape(-1).astype(np.float32, copy=False)

    def stop(self):
        import numpy as np

        with self._lock:
            if self._stream is None:
                return None
            self._stream.stop()
            self._stream.close()
            self._stream = None
            with self._frames_lock:
                frames = self._frames
                self._frames = []
                self._n_samples = 0
            if not frames:
                return None
            return np.concatenate(frames, axis=0).reshape(-1)
