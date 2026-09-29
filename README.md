# dictate — push-to-talk local para agentes de terminal

Mantienes una tecla pulsada, hablas, la sueltas y el texto aparece en Claude Code
u opencode. Todo local: el audio no sale de tu máquina.

## Instalación

```bash
pip install sounddevice numpy faster-whisper pynput
```

Dependencia del sistema para capturar audio:

```bash
brew install portaudio          # macOS
sudo apt install libportaudio2  # Debian/Ubuntu
```

Genera la config y arranca:

```bash
python cli.py config     # crea ~/.config/dictate/config.json
python cli.py daemon     # descarga el modelo la primera vez (~1.5 GB)
```

En otra terminal, prueba a mano antes de atar teclas:

```bash
python cli.py start      # habla
python cli.py stop
```

## Atar la tecla

Elige **una** de las dos vías.

### Vía A: que el daemon escuche el teclado

Pon `"hotkey": "alt_r"` en la config y reinicia el daemon. Funciona en macOS y
X11. En macOS tienes que dar permiso de Accesibilidad a la app desde la que
lanzas el daemon (Ajustes → Privacidad y seguridad → Accesibilidad). En Wayland
no funciona: los hooks globales de teclado están bloqueados por diseño.

### Vía B: que lo ate tu gestor de ventanas

Más fiable, y es la única opción en Wayland. Deja `hotkey` vacío.

**Hammerspoon (macOS)**, en `~/.hammerspoon/init.lua`:

```lua
local d = "/usr/bin/python3 /ruta/a/cli.py "
hs.hotkey.bind({"cmd", "alt"}, "D",
  function() hs.execute(d .. "start") end,
  function() hs.execute(d .. "stop") end)
```

**Hyprland**, en `hyprland.conf`:

```
bind  = , F13, exec, python /ruta/a/cli.py start
bindr = , F13, exec, python /ruta/a/cli.py stop
```

**sway / i3**:

```
bindsym --no-repeat F13 exec python /ruta/a/cli.py start
bindsym --release   F13 exec python /ruta/a/cli.py stop
```

## Config

| Clave | Qué hace |
|---|---|
| `engine` | `faster-whisper` (por defecto) o `parakeet` |
| `model` | `large-v3-turbo`, `medium`, `small`... |
| `language` | `es` fijo, o `null` para autodetectar |
| `compute_type` | `int8` en CPU, `float16` si tienes GPU |
| `sink` | `type`, `clipboard`, `tmux`, `opencode` |
| `postprocess` | `true` para limpiar el texto con un LLM local |
| `hotkey` | tecla para la vía A, o `""` |
| `overlay` | `true` (por defecto) muestra la barra flotante con el micro; `false` = daemon sin ventana |

### Overlay

Al arrancar el daemon aparece una barra flotante abajo en el centro de la
pantalla, siempre encima. El botón del micro alterna grabar/parar, igual que
`toggle`; la tecla y los comandos `start`/`stop` siguen funcionando y la barra
refleja su estado.

Mientras hablas, la transcripción aparece en tiempo real: el texto ya
confirmado en claro y el final aún inestable atenuado. Al parar se transcribe
solo ese último tramo y se pega lo que viste en la ventana con foco. La barra no
roba el foco al pulsarla, así que el texto va a la ventana donde estabas
escribiendo.

Con `"overlay": false`, o sin sesión gráfica, el daemon se comporta como antes.

### Salida por tmux

Si lanzas el agente dentro de tmux, `"sink": "tmux"` inyecta el texto en el
panel sin depender del foco de ventana. Nunca manda Enter: revisas y envías tú.
Con `tmux_target` vacío escribe en el panel activo; si prefieres uno fijo, saca
el identificador con `tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index}'`.

### Post-proceso

Con `"postprocess": true` el texto crudo pasa por Ollama antes de llegar al
sink. Ahí es donde se arregla lo que de verdad rompe el dictado técnico: que el
ASR escriba "use effect" en vez de `useEffect`, o "punto ts equis" en vez de
`.tsx`. El prompt recibe además los nombres de archivo del repo actual
(`git ls-files`) como glosario, así que acierta con los identificadores de tu
proyecto. Añade términos fijos en `glossary`.

Cuesta unos 300-500 ms extra. Si Ollama no responde, el texto crudo pasa igual.

### Parakeet en vez de Whisper

Parakeet TDT 0.6B v3 es más rápido en CPU y pesa cuatro veces menos, a cambio de
cubrir solo 25 idiomas europeos. Si dictas en español y nada más, compensa:

```bash
pip install onnx-asr[cpu,hub]
```

y pon `"engine": "parakeet"`. El identificador del modelo en `onnx_asr.load_model`
cambia entre versiones del paquete, así que si falla, compruébalo en el README de
onnx-asr antes de dar por roto el script.

## Ampliar por API

`OpenCodeSink` ya está esbozado: crea una sesión contra `opencode serve` y le
manda el prompt por HTTP. Está marcado como experimental porque el esquema del
body ha ido cambiando. Antes de usarlo, levanta el servidor y mira la spec real:

```bash
opencode serve --port 4096
curl -s localhost:4096/doc | jq '.paths | keys'
```

Para Claude Code el equivalente sería un sink que llame a `claude -p "<texto>"`
en modo headless, o que use el SDK si quieres mantener la sesión viva entre
dictados.

Un aviso sobre esta ruta: al enviar por API pierdes el paso de revisión. Con
`type` o `tmux` el texto aparece en el prompt y tú decides si pulsas Enter; por
API el agente arranca con lo que sea que haya entendido el ASR. Para un agente
que escribe archivos y ejecuta comandos, esa diferencia importa. Yo dejaría la
vía API para flujos concretos (una sesión en modo plan, o un agente en un
contenedor) y el dictado del día a día por `type`.

## Problemas típicos

**No graba nada.** Mira `python cli.py devices` y fija `input_device` con el
índice correcto. En macOS, la app que lanza el daemon necesita permiso de
Micrófono.

**Escribe en la ventana equivocada.** El sink `type` va a donde esté el foco.
Con el push-to-talk atado al gestor de ventanas el foco no se mueve, pero si te
pasa a menudo, cámbiate al sink `tmux`.

**Los acentos salen mal en Linux.** `xdotool type` tiene problemas con algunos
layouts. Si estás en X11 y te pasa, usa `"sink": "clipboard"` y pega tú.

**Primera transcripción lentísima.** El modelo se carga al arrancar el daemon,
no en el primer dictado. Si el daemon acaba de arrancar, espera al mensaje
`listo en Xs`.
