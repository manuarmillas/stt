"""Motores de transcripción."""

from __future__ import annotations

from config import SAMPLE_RATE, Config


class FasterWhisperEngine:
    """faster-whisper (CTranslate2). Funciona en CPU y GPU, 99+ idiomas."""

    def __init__(self, cfg: Config):
        from faster_whisper import WhisperModel

        device = cfg.device
        if device == "auto":
            device = "cuda" if _has_cuda() else "cpu"
        self.language = cfg.language or None
        self.model = WhisperModel(cfg.model, device=device, compute_type=cfg.compute_type)

    def transcribe_segments(self, audio) -> list[tuple[float, float, str]]:
        """Devuelve (inicio, fin, texto) en segundos relativos al trozo de audio."""
        segments, _ = self.model.transcribe(
            audio,
            language=self.language,
            beam_size=1,              # dictado corto: greedy va sobrado y es más rápido
            vad_filter=True,
            condition_on_previous_text=False,
        )
        # El generador es perezoso: hay que consumirlo aquí, dentro del lock del llamador.
        out = []
        for s in segments:
            text = s.text.strip()
            if text:
                out.append((float(s.start), float(s.end), text))
        return out

    def transcribe(self, audio) -> str:
        return " ".join(t for _, _, t in self.transcribe_segments(audio)).strip()


class ParakeetEngine:
    """Parakeet TDT 0.6B v3 vía onnx-asr. Más rápido en CPU, 25 idiomas europeos."""

    def __init__(self, cfg: Config):
        import onnx_asr

        # Si el id falla, mira `python -c "import onnx_asr; print(onnx_asr.__doc__)"`
        # o el README de onnx-asr: los nombres cambian entre versiones.
        self.model = onnx_asr.load_model(cfg.model or "nemo-parakeet-tdt-0.6b-v3")

    def transcribe_segments(self, audio) -> list[tuple[float, float, str]]:
        # onnx_asr no da timestamps aquí: devolvemos un único segmento que abarca
        # todo el trozo, así que el streaming nunca confirma nada antes de tiempo
        # y simplemente re-transcribe el buffer sin confirmar en cada tick.
        text = self.model.recognize(audio, sample_rate=SAMPLE_RATE).strip()
        if not text:
            return []
        return [(0.0, len(audio) / SAMPLE_RATE, text)]

    def transcribe(self, audio) -> str:
        return " ".join(t for _, _, t in self.transcribe_segments(audio)).strip()


def build_engine(cfg: Config):
    if cfg.engine == "parakeet":
        return ParakeetEngine(cfg)
    return FasterWhisperEngine(cfg)


def _has_cuda() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False
