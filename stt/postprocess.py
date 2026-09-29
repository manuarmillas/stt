from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from config import Config


# ─────────────────────────────────────────────────────────────────────────────
# Post-proceso con LLM local (opcional pero es donde está la magia)
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Eres un corrector de dictado para instrucciones dirigidas a un agente de programación.

Recibes una transcripción automática. Devuelve SOLO el texto corregido, sin comentarios ni comillas.

Reglas:
- Elimina muletillas y repeticiones por titubeo ("eh", "o sea", "bueno bueno").
- No cambies el sentido ni añadas nada. No respondas a la instrucción, solo límpiala.
- Convierte identificadores dictados a su forma real: "use effect" -> useEffect,
  "punto ts equis" -> .tsx, "guion bajo" -> _, "barra" -> /, "arroba" -> @.
- Rodea de comillas invertidas rutas, nombres de archivo, comandos y símbolos de código.
- Respeta el idioma original.
"""


class PostProcessor:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def run(self, text: str) -> str:
        if not self.cfg.postprocess or not text:
            return text
        import urllib.request

        glossary = ""
        terms = list(self.cfg.glossary) + _project_glossary()
        if terms:
            glossary = "\n\nTérminos del proyecto actual: " + ", ".join(sorted(set(terms)))

        payload = {
            "model": self.cfg.ollama_model,
            "prompt": text,
            "system": SYSTEM_PROMPT + glossary,
            "stream": False,
            "think": False,
            "options": {"temperature": 0.1, "num_predict": 512},
        }
        req = urllib.request.Request(
            self.cfg.ollama_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                out = json.loads(r.read()).get("response", "").strip()
            return out or text
        except Exception as e:
            print(f"[postproc] fallo, uso el texto crudo: {e}", file=sys.stderr)
            return text


def _project_glossary(limit: int = 120) -> list[str]:
    """Saca nombres de archivo del repo actual para que el LLM acierte con los identificadores."""
    try:
        out = subprocess.run(
            ["git", "ls-files"], capture_output=True, text=True, timeout=3
        ).stdout
    except Exception:
        return []
    names = {Path(p).name for p in out.splitlines() if p}
    return sorted(names)[:limit]
