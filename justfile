py := ".venv/bin/python"

alias d := daemon
alias s := start
alias x := stop
alias t := toggle
alias p := ping
alias dev := devices
alias c := config

# lista las recetas
default:
    @just --list

# arranca el daemon (deja la terminal abierta)
daemon:
    {{py}} cli.py daemon

# empieza a grabar
start:
    {{py}} cli.py start

# para, transcribe y envía al sink
stop:
    {{py}} cli.py stop

# alterna grabación
toggle:
    {{py}} cli.py toggle

# comprueba que el daemon responde
ping:
    {{py}} cli.py ping

# lista dispositivos de audio
devices:
    {{py}} cli.py devices

# muestra (y crea si falta) la config activa
config:
    {{py}} cli.py config
