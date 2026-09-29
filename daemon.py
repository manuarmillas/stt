"""
Daemon: cablea el servicio de dictado con el socket unix, el hotkey y (opcional) el overlay.

Con overlay, Tk ocupa el hilo principal y todo lo demás va en hilos daemon.
Sin overlay (o sin display) se comporta como el daemon original: el socket en el principal.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import threading

from config import Config, SOCKET_PATH
from stt.service import DictationService

# Referencia al socket de escucha para poder cerrarlo desde el hilo principal.
_server: socket.socket | None = None


def run(cfg: Config) -> None:
    service = DictationService(cfg)
    # Tk solo habla X11 (en Wayland va por XWayland): sin DISPLAY no hay overlay posible.
    has_display = bool(os.environ.get("DISPLAY"))
    if cfg.overlay and has_display:
        _run_with_overlay(cfg, service)
    else:
        _run_headless(cfg, service)


def _run_headless(cfg: Config, service: DictationService) -> None:
    service.warm()
    if cfg.hotkey:
        threading.Thread(target=_keyboard_loop, args=(cfg, service), daemon=True).start()
    _serve_socket(service)


def _run_with_overlay(cfg: Config, service: DictationService) -> None:
    # Import perezoso: en headless no debe hacer falta tkinter.
    import tkinter as tk

    from ui.overlay import Overlay

    root = tk.Tk()
    overlay = Overlay(root, service)
    # El listener se asigna antes de arrancar warm() para no perder ningún evento.
    service.listener = overlay.post
    overlay.post("state", {"state": "loading"})

    def warm() -> None:
        try:
            service.warm()
        except Exception:
            # warm() ya ha logueado y emitido el error; el daemon no es útil sin modelo.
            root.after(0, root.destroy)

    threading.Thread(target=warm, daemon=True).start()
    sock_thread = threading.Thread(target=_serve_socket, args=(service,), daemon=True)
    sock_thread.start()
    if cfg.hotkey:
        threading.Thread(target=_keyboard_loop, args=(cfg, service), daemon=True).start()

    # Tk no cede el control a Python con la frecuencia suficiente para ver SIGINT:
    # el handler solo marca una bandera y un tick periódico de Tk la atiende.
    interrupted = threading.Event()
    signal.signal(signal.SIGINT, lambda signum, frame: interrupted.set())

    def tick() -> None:
        if interrupted.is_set():
            root.destroy()
        else:
            root.after(200, tick)

    root.after(200, tick)
    try:
        root.mainloop()
    finally:
        _close_server()
        sock_thread.join(timeout=2)
        SOCKET_PATH.unlink(missing_ok=True)


def _serve_socket(service: DictationService) -> None:
    global _server
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(SOCKET_PATH))
    srv.listen(8)
    _server = srv
    print(f"[daemon] escuchando en {SOCKET_PATH}", flush=True)
    try:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                # Ruta de apagado esperada: el hilo principal cerró el socket.
                return
            with conn:
                cmd = conn.recv(256).decode().strip()
                conn.sendall(service.handle(cmd).encode())
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        SOCKET_PATH.unlink(missing_ok=True)


def _close_server() -> None:
    srv = _server
    if srv is None:
        return
    try:
        # shutdown despierta el accept() bloqueado en el otro hilo; close solo no basta en Linux.
        srv.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # ya cerrado o sin conexión: nada que despertar
    srv.close()


def _keyboard_loop(cfg: Config, service: DictationService) -> None:
    """Push-to-talk gestionado por el propio daemon. No funciona en Wayland:
    ahí ata las teclas desde el compositor y usa `start`/`stop`."""
    from pynput import keyboard

    name = cfg.hotkey
    target = getattr(keyboard.Key, name, None) or keyboard.KeyCode.from_char(name)

    def on_press(key):
        if key == target and service.state == "idle":
            service.start()

    def on_release(key):
        if key == target and service.state == "recording":
            service.stop()

    print(f"[daemon] push-to-talk con <{name}>", flush=True)
    keyboard.Listener(on_press=on_press, on_release=on_release).run()
