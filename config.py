from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

SAMPLE_RATE = 16_000
MAX_SECONDS = 180  # cortafuegos por si te dejas la tecla pegada

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
SOCKET_PATH = RUNTIME_DIR / "dictate.sock"
CONFIG_PATH = Path(os.path.expanduser("~/.config/dictate/config.json"))


# ─────────────────────────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Config:
    # ASR
    engine: str = "faster-whisper"      # faster-whisper | parakeet
    model: str = "large-v3-turbo"       # para faster-whisper
    language: str = "es"                # None = autodetectar
    compute_type: str = "int8"          # int8 | int8_float16 | float16
    device: str = "auto"                # auto | cpu | cuda

    # Audio
    input_device: int | str | None = None   # None = dispositivo por defecto

    # Post-proceso con LLM local (opcional)
    postprocess: bool = False
    ollama_url: str = "http://127.0.0.1:11434/api/generate"
    ollama_model: str = "qwen3:4b"
    glossary: list[str] = field(default_factory=list)

    # Salida
    sink: str = "type"                  # type | clipboard | tmux | opencode
    tmux_target: str = ""               # p.ej. "dev:0.1"; vacío = panel activo
    opencode_url: str = "http://127.0.0.1:4096"
    opencode_directory: str = ""        # cwd del proyecto para la sesión

    # Teclado (si dejas que el daemon escuche directamente)
    hotkey: str = ""                    # p.ej. "alt_r", "f13". Vacío = desactivado

    # UI
    overlay: bool = True                # barra flotante con botón de micro y texto en vivo

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text())
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        return cfg

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))
