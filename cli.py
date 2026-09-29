from __future__ import annotations

import argparse
import json
import socket
import sys
from dataclasses import asdict

from config import CONFIG_PATH, SOCKET_PATH, Config


def client(cmd: str) -> None:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(SOCKET_PATH))
        s.sendall(cmd.encode())
        print(s.recv(256).decode())
        s.close()
    except (FileNotFoundError, ConnectionRefusedError):
        sys.exit("daemon no arrancado: ejecuta `cli.py daemon`")


# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Dictado push-to-talk local")
    p.add_argument("command", choices=["daemon", "start", "stop", "toggle", "ping", "devices", "config"])
    args = p.parse_args()

    cfg = Config.load()

    if args.command == "devices":
        import sounddevice as sd
        print(sd.query_devices())
    elif args.command == "config":
        if not CONFIG_PATH.exists():
            cfg.save()
            print(f"config creada en {CONFIG_PATH}")
        print(json.dumps(asdict(cfg), indent=2, ensure_ascii=False))
    elif args.command == "daemon":
        from daemon import run

        run(cfg)
    else:
        client(args.command)


if __name__ == "__main__":
    main()
