# Voice-to-Keystroke

Aplicación de escritorio para Windows que convierte palabras habladas en pulsaciones de teclado. Dices "saltar" y se pulsa **Espacio**; dices "disparar" y se pulsa **Ctrl**. Funciona con cualquier programa o juego que tenga el foco.

El reconocimiento de voz es **100 % local y gratuito** ([Vosk](https://alphacephei.com/vosk/)): el audio nunca sale de tu PC.

## Características

- **Reglas palabra → tecla** editables desde la ventana y guardadas automáticamente.
- **Rápido**: solo busca tus palabras clave, así que reconoce en ~0,4 s.
- **Manos libres** o **pulsar para hablar** (manteniendo la tecla o alternando con ella).
- **Velocidad ↔ precisión** ajustable: pulsar al instante o esperar a confirmar la palabra.
- **Sensibilidad** con medidor de volumen en vivo y valor recomendado.
- **Filtro de bocinas**: ignora las palabras que salen de tus altavoces (videos, streams, llamadas).
- Interfaz en azul oscuro que cambia de color mientras escucha.

## Requisitos

- Windows 10 u 11.
- Un micrófono.
- Internet solo la primera vez, para descargar el modelo de voz en español (~40 MB).
- Para ejecutar desde el código: Python 3.12 o superior.

## Instalación

### Desde el código

```bat
git clone https://github.com/Deivid-SVG-AI/Voice-to-Keystroke.git
cd Voice-to-Keystroke
python -m venv .venv
.venv\Scripts\activate
pip install vosk sounddevice pynput soundcard
python main.py
```

`soundcard` solo hace falta para el filtro de bocinas; sin él la app funciona igual.

### Como .exe

Ejecuta `build.bat` (doble clic). Crea el entorno, instala las dependencias y PyInstaller, y genera `dist\VoiceToKeystroke.exe`, un único archivo que puedes copiar a cualquier carpeta o PC con Windows.

- El `.exe` no incluye configuración: crea su propio `config.json` junto a él al guardar tu primera regla o ajuste.
- Al no estar firmado, Windows SmartScreen o el antivirus pueden avisar la primera vez que lo abras.

### Modelo de voz

Al pulsar **Start Listening** por primera vez se descarga el modelo `vosk-model-small-es-0.42` a `%USERPROFILE%\.cache\vosk`. En un PC sin internet, descárgalo de <https://alphacephei.com/vosk/models> y descomprímelo en esa carpeta.

## Uso

1. Escribe una **palabra** y elige o escribe una **tecla**, y pulsa **+ Agregar**.
2. Pulsa **Start Listening**. La ventana se vuelve azul mientras escucha.
3. Cambia a la ventana del juego o programa y di la palabra.

Para borrar una regla, selecciónala y pulsa **Eliminar seleccionada** (o la tecla Supr). La barra inferior muestra cada palabra reconocida, la tecla que pulsó o por qué la ignoró.

### Teclas válidas

| Escribe | Pulsa |
|---|---|
| `a`, `7`, `-` … | esa letra, número o símbolo |
| `space` / `espacio` | Espacio |
| `enter` / `intro` | Enter |
| `tab`, `esc`, `backspace` / `borrar` | Tab, Esc, Retroceso |
| `up`, `down`, `left`, `right` (o `arriba`, `abajo`, `izquierda`, `derecha`) | Flechas |
| `f1` … `f20` | Teclas de función |
| `num0` … `num9` | Teclado numérico |
| `ctrl+c`, `shift+tab`, `alt+f4` … | Combinaciones (con `+`) |

También vale cualquier nombre de tecla de [`pynput`](https://pynput.readthedocs.io/en/latest/keyboard.html#pynput.keyboard.Key) (`home`, `page_down`, `caps_lock`…).

**Palabras clave**: una sola palabra por regla, en español. Si el modelo no conoce una palabra, la barra de estado lo indica al empezar a escuchar ("fuera del vocabulario"). Evita palabras muy cortas o comunes (`a`, `de`, `que`): se activarían con cualquier conversación.

## Ajustes

### Modo

- **Manos libres**: escucha siempre.
- **Pulsar para hablar**: solo escucha con la tecla elegida (por defecto `V`):
  - **Alternar** (por defecto): una pulsación empieza a escuchar y la siguiente lo detiene.
  - **Mantener para hablar**: solo mientras la mantienes pulsada.

La tecla de hablar funciona aunque otra ventana tenga el foco, y también le llega a esa ventana, así que elige una que tu juego no use.

### Velocidad ↔ Precisión

Cuánto tiempo debe mantenerse reconocida una palabra antes de pulsar la tecla:

- **Velocidad** (izquierda): pulsa en cuanto la oye (~0,4 s). Puede equivocarse si el reconocedor corrige la palabra un instante después.
- **Intermedio**: la confirma durante 100–900 ms.
- **Precisión** (derecha): espera a que termines la frase (~1,4 s).

### Sensibilidad

Volumen mínimo que debe tener una palabra para aceptarla. **100 %** (por defecto) acepta cualquier volumen; al bajarla, la app ignora las voces más bajas, como el eco lejano de unas bocinas.

- El medidor muestra el volumen del micrófono en vivo: **cian** si tu voz pasaría, **azul** si no.
- La marca **▼** es la sensibilidad **recomendada**, calculada del ruido de fondo de los últimos 10 s (necesita unos segundos escuchando). **Usar recomendada** la aplica.

### Ignorar lo que suena en las bocinas

Escucha también lo que Windows envía a los altavoces y descarta una palabra si también sonó ahí. Es la opción más eficaz cuando usas bocinas sin audífonos. Añade 300 ms de espera, pero solo mientras está activada.

## Si el audio de las bocinas activa comandos

De lo más simple a lo más eficaz:

1. Usa audífonos, o palabras clave poco comunes.
2. Activa la cancelación de eco de Windows: *Configuración → Sistema → Sonido → tu micrófono → Mejoras de audio*.
3. Baja la **Sensibilidad** (sirve si las bocinas suenan más bajo que tu voz).
4. Activa **Ignorar lo que suena en las bocinas**.
5. Usa **Pulsar para hablar**.

## Configuración

Las reglas y ajustes se guardan en `config.json` (junto a `main.py` o al `.exe`). Está en `.gitignore` para que tus reglas no se suban al repositorio. Ejemplo:

```json
{
  "wait": 0,
  "sensitivity": 100,
  "echo": false,
  "ptt": false,
  "ptt_toggle": true,
  "ptt_key": "v",
  "mappings": { "saltar": "space", "disparar": "ctrl" }
}
```

## Solución de problemas

| Problema | Solución |
|---|---|
| Las teclas no llegan al juego | Si el juego se ejecuta como administrador, ejecuta también esta app como administrador. |
| "«palabra» ignorada: volumen …" | Sube la **Sensibilidad** o habla más cerca del micrófono. |
| No reconoce una palabra | Comprueba que no aparezca como "fuera del vocabulario"; prueba otra palabra más común. |
| "Error: … reintentando en 2 s" | El micrófono está desconectado u ocupado; la app reintenta sola. |
| "Filtro de bocinas: falta instalar soundcard" | `pip install soundcard`. |

## Pruebas

```bat
python test_main.py
```

Genera voz con la síntesis de Windows (requiere la voz española *Microsoft Helena*, incluida en Windows en español) y la pasa por el motor en lugar del micrófono. Comprueba el reconocimiento, la velocidad/precisión, la sensibilidad, pulsar para hablar y el filtro de bocinas.

## Cómo funciona

- **Vosk** reconoce el audio del micrófono con una gramática cerrada: solo tus palabras clave más "desconocido". Por eso es rápido y apenas confunde palabras.
- **sounddevice** captura el micrófono y **soundcard** el audio de los altavoces (loopback WASAPI).
- **pynput** simula las teclas y escucha la tecla de hablar.
- La interfaz es **tkinter**; el reconocimiento corre en hilos aparte para no congelarla.
