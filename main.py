"""
Voice-to-Keystroke: di una palabra clave y se pulsa la tecla asignada.

Instalación (Windows):
    python -m venv .venv
    .venv\\Scripts\\activate
    pip install vosk sounddevice pynput soundcard

    Se usa sounddevice en lugar de pyaudio: trae PortAudio incluido y tiene wheels
    para cualquier versión de Python (pyaudio no las publica para 3.14).
    soundcard solo hace falta para el filtro de bocinas: captura lo que suena por los
    altavoces (loopback WASAPI), cosa que sounddevice no puede.
    La GUI usa tkinter, que ya viene con Python.

Modelo de voz (offline):
    Al pulsar "Start" por primera vez, vosk descarga solo el modelo ligero en español
    (vosk-model-small-es-0.42, ~40 MB) a %USERPROFILE%\\.cache\\vosk.
    Descarga manual: https://alphacephei.com/vosk/models -> vosk-model-small-es-0.42.zip,
    descomprímelo en %USERPROFILE%\\.cache\\vosk (o en la carpeta que indique la
    variable de entorno VOSK_MODEL_PATH).

Uso:
    python main.py
    Teclas válidas: una letra o dígito ("a", "7"), nombres de pynput ("space", "enter",
    "tab", "esc", "backspace", "up", "f5", ...), alias en español ("espacio", "intro"),
    teclado numérico "num0".."num9" y combinaciones con "+" ("ctrl+c").
    Modo: "Manos libres" escucha siempre; "Pulsar para hablar" solo mientras mantienes
    la tecla elegida (por defecto V). Esa tecla también le llega a la ventana activa.
    Velocidad/Precisión: cuánto debe mantenerse reconocida una palabra antes de pulsar
    la tecla (0 = al instante, máximo = al terminar la frase).
    Umbral de volumen: ignora palabras más bajas que el umbral (el eco de las bocinas
    llega más bajo que tu voz). La marca ▼ es el recomendado: ruido de fondo + 10 %.
    Filtro de bocinas: descarta una palabra si también sonó por los altavoces (+300 ms).
    Si la ventana destino se ejecuta como administrador, esta app también debe hacerlo.
"""
import array
import ctypes
import json
import math
import queue
import threading
import time
import tkinter as tk
import warnings
from collections import deque
from pathlib import Path
from tkinter import messagebox, ttk

import sounddevice as sd
import vosk
from pynput import keyboard
from pynput.keyboard import Controller, Key, KeyCode

CONFIG = Path(__file__).with_name("config.json")
RATE = 16000  # los modelos de vosk están entrenados a 16 kHz
CHUNK = RATE // 10  # 100 ms de audio por lectura: la unidad de tiempo de todo el motor
MAX_WAIT = 10  # posición "precisión máxima" del slider: solo el resultado final de vosk
ECHO_DELAY = 0.3  # s que espera una palabra por si el loopback de las bocinas también la oye
ECHO_WINDOW = 1.5  # s hacia atrás en que una palabra oída en las bocinas anula la del micrófono
vosk.SetLogLevel(-1)

C = {  # paleta azul oscuro
    "idle": "#0a1222", "listen": "#0b2a57", "surface": "#111d33", "surface2": "#182844",
    "row": "#142340", "border": "#26395c", "text": "#e4ecfa", "muted": "#7f93b5",
    "accent": "#3b82f6", "accent_hi": "#60a5fa", "live": "#38d9f5", "meter": "#2c4f88",
}
FONT = "Segoe UI"
BOLD = "Segoe UI Semibold"


def chunk_level(data):
    """Volumen de un bloque int16 en 0-100 % (escala dB: -60 dBFS = 0 %, 0 dBFS = 100 %)."""
    a = array.array("h", data)
    rms = math.sqrt(math.sumprod(a, a) / len(a)) if a else 0
    return max(0, min(100, round((20 * math.log10(max(rms, 1) / 32768) + 60) * 100 / 60)))


class KeyMapper:
    ALIASES = {"espacio": "space", "intro": "enter", "entrar": "enter", "escape": "esc",
               "borrar": "backspace", "tabulador": "tab", "arriba": "up", "abajo": "down",
               "izquierda": "left", "derecha": "right", "control": "ctrl", "mayus": "shift"}

    def __init__(self):
        self.kb = Controller()

    def parse(self, combo):
        """'ctrl+c' -> [Key.ctrl, 'c']. Lanza ValueError si alguna tecla no existe."""
        keys = []
        for name in combo.lower().replace(" ", "").split("+"):
            name = self.ALIASES.get(name, name)
            if len(name) == 1:
                keys.append(name)
            elif len(name) == 4 and name.startswith("num") and name[3].isdigit():
                keys.append(KeyCode.from_vk(0x60 + int(name[3])))  # VK_NUMPAD0..9
            elif name in Key.__members__:
                keys.append(Key[name])
            else:
                raise ValueError(f"Tecla desconocida: {name!r}")
        return keys

    def press(self, combo):
        keys = self.parse(combo)
        for k in keys:
            self.kb.press(k)
        for k in reversed(keys):
            self.kb.release(k)


def recognizer(model, words):
    """Gramática cerrada = solo se buscan nuestras palabras: mucho más rápido y preciso."""
    return vosk.KaldiRecognizer(model, RATE, json.dumps(words + ["[unk]"], ensure_ascii=False))


class WordStream:
    """Pasa audio a vosk y entrega cada palabra clave una sola vez: con el resultado parcial si
    se mantuvo `wait` lecturas seguidas, o como muy tarde con el final (tras el silencio)."""

    def __init__(self, model, words):
        self.rec = recognizer(model, words)
        self.rec.SetWords(True)  # tiempos exactos de cada palabra en el resultado final
        self.n = 0  # lecturas recibidas
        self.fired, self.pending, self.age = 0, [], 0

    def feed(self, data, wait=0):
        """-> [(palabra, primera_lectura, última_lectura)] listas para disparar."""
        self.n += 1
        final = self.rec.AcceptWaveform(data)
        return self._take(json.loads(self.rec.Result() if final else self.rec.PartialResult()), final, wait)

    def flush(self):
        """Cierra la frase en curso (al soltar la tecla de pulsar para hablar)."""
        return self._take(json.loads(self.rec.FinalResult()), True, 0)

    def _take(self, res, final, wait):
        if final:
            heard = [(r["word"], int(r["start"] * 10), int(r["end"] * 10))
                     for r in res.get("result", []) if r["word"] != "[unk]"]
        else:
            heard = [(w, None, None) for w in res.get("partial", "").split() if w != "[unk]"]
        new = heard[self.fired:]  # palabras de la frase actual aún sin disparar
        names = [w for w, _, _ in new]
        self.age = self.age + 1 if names == self.pending else 0
        self.pending = names
        if not final:  # los parciales no traen tiempos: vosk los da ~300 ms después del inicio de la palabra
            new = [(w, max(0, self.n - self.age - 4), self.n) for w in names]
        out = []
        # ponytail: si vosk corrige una palabra ya disparada, esa tecla ya se pulsó;
        # subir `wait` (precisión) lo evita a cambio de latencia.
        if new and (final or self.age >= wait):
            out, self.fired = new, self.fired + len(new)
        if final:
            self.fired, self.pending, self.age = 0, [], 0
        return out


class VoiceEngine:
    """Escucha el micrófono en un hilo aparte y llama on_word(palabra) por cada palabra clave."""

    def __init__(self, on_word, on_status):
        self.on_word, self.on_status = on_word, on_status
        self.model = None
        # Ajustes que la GUI cambia en vivo; el hilo los lee en cada lectura de 100 ms.
        self.wait = 0  # lecturas que una palabra debe mantenerse antes de disparar (math.inf = fin de frase)
        self.threshold = 0  # volumen mínimo (0-100 %) de una palabra para dispararla
        self.echo = False  # filtro de bocinas (se aplica al hacer start)
        self.ptt = False  # pulsar para hablar: solo se escucha mientras held
        self.held = False
        self.level = 0  # volumen de la última lectura, para el medidor
        self._recent = deque(maxlen=100)  # volúmenes de los últimos 10 s
        self._speaker = {}  # palabra -> último instante en que se oyó en las bocinas
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._stop.set()

    @property
    def running(self):
        return not self._stop.is_set()

    @property
    def capturing(self):
        return self.running and (not self.ptt or self.held)

    @property
    def recommended(self):
        """Umbral recomendado: ruido de fondo de los últimos 10 s (percentil 90) + 10 %."""
        # ponytail: heurística; si hablas mucho en esos 10 s sube. El medidor sirve para afinarlo.
        r = sorted(self._recent)
        return min(95, r[len(r) * 9 // 10] + 10) if len(r) >= 30 else None

    def start(self, words):
        self.stop()
        # Un Event por arranque: si un hilo anterior sigue cargando el modelo, muere solo sin revivir.
        self._stop = stop = threading.Event()
        words = sorted(words)
        threading.Thread(target=self._run, args=(words, stop), daemon=True).start()
        if self.echo:
            threading.Thread(target=self._run_speakers, args=(words, stop), daemon=True).start()

    def stop(self):
        self._stop.set()

    def _model(self, say):
        with self._lock:
            if self.model is None:
                say("Cargando modelo (la 1ª vez se descarga, ~40 MB)...")
                self.model = vosk.Model(lang="es")
        return self.model

    def _run(self, words, stop):
        say = lambda msg: stop.is_set() or self.on_status(msg)
        while not stop.is_set():
            try:
                model = self._model(say)
                missing = [w for w in words if model.vosk_model_find_word(w) < 0]
                ws = WordStream(model, words)
                with sd.RawInputStream(samplerate=RATE, blocksize=CHUNK, dtype="int16", channels=1) as stream:
                    say(f"Escuchando... (fuera del vocabulario, se ignoran: {', '.join(missing)})"
                        if missing else "Escuchando...")
                    levels = bytearray()  # volumen de cada lectura enviada a vosk (índice = WordStream.n - 1)
                    preroll = deque(maxlen=3)  # 300 ms antes de pulsar la tecla, para no cortar la 1ª sílaba
                    delayed = []  # (instante, palabra) esperando al filtro de bocinas
                    talking = False

                    def handle(found):
                        for w, first, last in found:
                            lvl = max(levels[first:last + 1], default=0)
                            if lvl < self.threshold:
                                say(f"«{w}» ignorada: volumen {lvl} % < umbral {self.threshold} %")
                            elif self.echo:
                                delayed.append((time.monotonic(), w))
                            else:
                                self.on_word(w)

                    while not stop.is_set():
                        data = bytes(stream.read(CHUNK)[0])
                        self.level = chunk_level(data)
                        self._recent.append(self.level)
                        if self.ptt and not self.held:
                            if talking:  # se soltó la tecla: cerrar la frase
                                handle(ws.flush())
                                talking = False
                            preroll.append((data, self.level))
                        else:
                            talking = True
                            for d, lvl in (*preroll, (data, self.level)):
                                levels.append(lvl)
                                handle(ws.feed(d, self.wait))
                            preroll.clear()
                        now = time.monotonic()
                        while delayed and now - delayed[0][0] >= ECHO_DELAY:
                            t, w = delayed.pop(0)
                            if self._speaker.get(w, -math.inf) >= t - ECHO_WINDOW:
                                say(f"«{w}» ignorada: sonó en las bocinas")
                            else:
                                self.on_word(w)
            except Exception as e:  # micrófono desconectado/ocupado, etc.: avisar y reintentar
                say(f"Error: {e} — reintentando en 2 s")
                stop.wait(2)

    def _run_speakers(self, words, stop):
        """Reconoce lo que suena en las bocinas (loopback WASAPI) para descartar su eco en el micrófono."""
        say = lambda msg: stop.is_set() or self.on_status(msg)
        while not stop.is_set():
            try:
                import soundcard as sc  # su 1ª importación inicializa COM (multihilo) en este hilo...
                ctypes.windll.ole32.CoInitializeEx(None, 0)  # ...y tras reiniciar el motor (otro hilo) hace falta aquí
                warnings.filterwarnings("ignore", message="data discontinuity")
                rec = recognizer(self._model(say), words)
                speakers = sc.get_microphone(str(sc.default_speaker().name), include_loopback=True)
                with speakers.recorder(samplerate=RATE, channels=1, blocksize=CHUNK) as loop:
                    while not stop.is_set():
                        pcm = (loop.record(CHUNK)[:, 0].clip(-1, 1) * 32767).astype("<i2").tobytes()
                        final = rec.AcceptWaveform(pcm)
                        res = json.loads(rec.Result() if final else rec.PartialResult())
                        # Todo lo que aparezca, aunque vosk lo corrija después: aquí sobra más que falte.
                        now = time.monotonic()
                        for w in res.get("text" if final else "partial", "").split():
                            self._speaker[w] = now
            except ImportError:
                return say("Filtro de bocinas: falta instalar soundcard (pip install soundcard)")
            except Exception as e:
                say(f"Error en el filtro de bocinas: {e} — reintentando en 2 s")
                stop.wait(2)


class LevelSlider(tk.Canvas):
    """Slider 0-100 % que además muestra el volumen del micrófono y la marca ▼ del recomendado."""

    def __init__(self, master, command):
        super().__init__(master, height=30, highlightthickness=0, takefocus=1, cursor="hand2")
        self.value, self.level, self.recommended, self.command = 0, 0, None, command
        self.focused = False
        drag = lambda e: self.set(round(100 * e.x / max(1, self.winfo_width())))
        self.bind("<Button-1>", lambda e: (self.focus_set(), drag(e)))
        self.bind("<B1-Motion>", drag)
        self.bind("<Left>", lambda e: self.set(self.value - 1))
        self.bind("<Right>", lambda e: self.set(self.value + 1))
        for ev, on in (("<FocusIn>", True), ("<FocusOut>", False)):
            self.bind(ev, lambda e, on=on: (setattr(self, "focused", on), self.draw()))
        self.bind("<Configure>", lambda e: self.draw())

    def set(self, value):
        self.value = max(0, min(100, value))
        self.command(self.value)
        self.draw()

    def draw(self):
        w, h = self.winfo_width(), self.winfo_height()
        x = lambda pct: 2 + pct * (w - 4) / 100
        self.delete("all")
        self.create_rectangle(0, 8, w - 1, h - 5, fill=C["surface2"], outline=C["border"])
        if self.level:  # cian = esta voz supera el umbral
            self.create_rectangle(1, 9, x(self.level), h - 6, width=0,
                                  fill=C["live"] if self.level >= self.value else C["meter"])
        if self.recommended is not None:
            r = x(self.recommended)
            self.create_polygon(r - 5, 0, r + 5, 0, r, 6, fill=C["live"])
        v = x(self.value)
        self.create_rectangle(v - 3, 4, v + 3, h - 1, fill=C["text"],
                              outline=C["accent_hi"] if self.focused else "", width=2)


class AppGUI:
    KEY_CHOICES = ["space", "enter", "tab", "esc", "backspace", "up", "down", "left", "right",
                   *[f"num{i}" for i in range(10)], "ctrl+c", "ctrl+v"]

    def __init__(self, root):
        self.root = root
        self.mapper = KeyMapper()
        self.events = queue.Queue()  # hilos del motor -> GUI (tkinter no es thread-safe)
        self.engine = VoiceEngine(self.on_word, self.events.put)
        try:
            data = json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if "mappings" not in data:  # config.json antiguo: solo el diccionario de reglas
            data = {"mappings": data}
        cfg = {"wait": 0, "threshold": 0, "echo": False, "ptt": False, "ptt_key": "v", **data}
        self.mappings = cfg["mappings"]
        self.engine.threshold, self.engine.echo, self.engine.ptt = cfg["threshold"], cfg["echo"], cfg["ptt"]

        root.title("Voice-to-Keystroke")
        root.minsize(880, 600)
        self.style_widgets()

        wrap = ttk.Frame(root, padding=22)
        wrap.pack(fill="both", expand=True)

        head = ttk.Frame(wrap)
        head.pack(fill="x")
        ttk.Label(head, text="Voice-to-Keystroke", style="Title.TLabel").pack(side="left")
        self.pill = ttk.Label(head, style="Pill.TLabel")
        self.pill.pack(side="right")
        ttk.Label(wrap, text="Di una palabra clave y se pulsa la tecla asignada.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 18))

        body = ttk.Frame(wrap)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 28))
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")

        # --- Reglas ---
        form = ttk.Frame(left)
        form.pack(fill="x")
        form.columnconfigure((0, 1), weight=1)
        ttk.Label(form, text="PALABRA", style="Caption.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(form, text="TECLA", style="Caption.TLabel").grid(row=0, column=1, sticky="w", padx=8, pady=(0, 4))
        self.word = ttk.Entry(form)
        self.word.grid(row=1, column=0, sticky="ew")
        self.key = ttk.Combobox(form, values=self.KEY_CHOICES)
        self.key.grid(row=1, column=1, sticky="ew", padx=8)
        ttk.Button(form, text="+ Agregar", command=self.add).grid(row=1, column=2, sticky="ns")
        self.word.bind("<Return>", lambda e: self.add())
        self.key.bind("<Return>", lambda e: self.add())

        self.tree = ttk.Treeview(left, columns=("word", "key"), show="headings", height=9)
        self.tree.heading("word", text="PALABRA", anchor="w")
        self.tree.heading("key", text="TECLA", anchor="w")
        self.tree.tag_configure("odd", background=C["row"])
        self.tree.pack(fill="both", expand=True, pady=(18, 8))
        self.tree.bind("<Delete>", lambda e: self.delete())
        ttk.Button(left, text="Eliminar seleccionada", command=self.delete).pack(anchor="e")

        # --- Ajustes ---
        ttk.Label(right, text="MODO", style="Caption.TLabel").pack(anchor="w", pady=(0, 4))
        modes = ttk.Frame(right)
        modes.pack(fill="x")
        modes.columnconfigure((0, 1), weight=1, uniform="mode")
        self.mode = tk.BooleanVar(value=cfg["ptt"])  # solo un modo activo: son radiobuttons
        for col, (text, value) in enumerate((("Manos libres", False), ("Pulsar para hablar", True))):
            ttk.Radiobutton(modes, text=text, variable=self.mode, value=value, style="Toolbutton",
                            command=self.set_mode).grid(row=0, column=col, sticky="ew", padx=(6 * col, 0))
        keyrow = ttk.Frame(right)
        keyrow.pack(fill="x", pady=(8, 0))
        ttk.Label(keyrow, text="Tecla para hablar", style="Muted.TLabel").pack(side="left")
        self.ptt_entry = ttk.Entry(keyrow, width=8, justify="center")
        self.ptt_entry.pack(side="right")
        self.ptt_entry.insert(0, cfg["ptt_key"])
        self.ptt_entry.bind("<Return>", lambda e: self.set_ptt_key())
        self.ptt_entry.bind("<FocusOut>", lambda e: self.set_ptt_key())

        tune = ttk.Frame(right)
        tune.pack(fill="x", pady=(22, 0))
        tune.columnconfigure(1, weight=1)
        ttk.Label(tune, text="VELOCIDAD", style="Caption.TLabel").grid(row=0, column=0)
        self.scale = ttk.Scale(tune, from_=0, to=MAX_WAIT, command=self.set_wait)
        self.scale.grid(row=0, column=1, sticky="ew", padx=12)
        self.scale.bind("<ButtonRelease-1>", lambda e: self.save())
        ttk.Label(tune, text="PRECISIÓN", style="Caption.TLabel").grid(row=0, column=2)
        self.wait_text = tk.StringVar()
        ttk.Label(tune, textvariable=self.wait_text, style="Muted.TLabel").grid(row=1, column=0, columnspan=3, pady=(6, 0))
        self.scale.set(cfg["wait"])
        self.set_wait(cfg["wait"])

        ttk.Label(right, text="UMBRAL DE VOLUMEN", style="Caption.TLabel").pack(anchor="w", pady=(22, 4))
        self.meter = LevelSlider(right, self.set_threshold)
        self.meter.value = cfg["threshold"]
        self.meter.pack(fill="x")
        self.meter.bind("<ButtonRelease-1>", lambda e: self.save(), add="+")
        self.meter.bind("<KeyRelease>", lambda e: self.save(), add="+")
        row = ttk.Frame(right)
        row.pack(fill="x", pady=(6, 0))
        self.threshold_text = tk.StringVar()
        ttk.Label(row, textvariable=self.threshold_text, style="Muted.TLabel").pack(side="left")
        ttk.Button(row, text="Usar recomendado", command=self.use_recommended).pack(side="right")

        self.echo_var = tk.BooleanVar(value=cfg["echo"])
        ttk.Checkbutton(right, text="Ignorar lo que suena en las bocinas", variable=self.echo_var,
                        command=self.set_echo).pack(anchor="w", pady=(22, 0))
        ttk.Label(right, text="Compara con el audio de los altavoces. Añade 300 ms de espera.",
                  style="Muted.TLabel").pack(anchor="w", padx=(26, 0))

        self.toggle_btn = ttk.Button(wrap, style="Accent.TButton", command=self.toggle)
        self.toggle_btn.pack(fill="x", pady=(22, 0))
        self.status = tk.StringVar(value="Detenido")
        ttk.Label(wrap, textvariable=self.status, style="Muted.TLabel").pack(anchor="w", pady=(10, 0))

        # Escucha global del teclado (también con otra ventana activa) para pulsar para hablar.
        self.ptt_key = self.ptt_target = None
        self.keys = keyboard.Listener(on_press=lambda k, injected=False: self.on_key(k, injected, True),
                                      on_release=lambda k, injected=False: self.on_key(k, injected, False))
        self.set_ptt_key()
        self.keys.start()

        self.refresh()
        self.apply_state()
        self.poll()

    def style_widgets(self):
        s = self.style = ttk.Style(self.root)
        s.theme_use("clam")  # el tema nativo de Windows ignora los colores
        s.configure(".", background=C["idle"], foreground=C["text"], font=(FONT, 10),
                    bordercolor=C["border"], lightcolor=C["surface"], darkcolor=C["surface"],
                    troughcolor=C["surface2"], fieldbackground=C["surface"], insertcolor=C["text"],
                    selectbackground=C["accent"], selectforeground="white", arrowcolor=C["muted"],
                    focuscolor=C["accent"])
        s.configure("Title.TLabel", font=(BOLD, 17))
        s.configure("Muted.TLabel", foreground=C["muted"])
        s.configure("Caption.TLabel", foreground=C["muted"], font=(BOLD, 8))
        s.configure("Pill.TLabel", font=(BOLD, 9))
        s.configure("TButton", background=C["surface2"], lightcolor=C["surface2"], darkcolor=C["surface2"],
                    padding=(14, 6))
        s.map("TButton", background=[("active", C["border"])], lightcolor=[("active", C["border"])],
              darkcolor=[("active", C["border"])])
        s.configure("Accent.TButton", background=C["accent"], lightcolor=C["accent"], darkcolor=C["accent"],
                    bordercolor=C["accent"], foreground="white", font=(BOLD, 11), padding=(14, 10))
        s.map("Accent.TButton", background=[("active", C["accent_hi"])], lightcolor=[("active", C["accent_hi"])],
              darkcolor=[("active", C["accent_hi"])], bordercolor=[("active", C["accent_hi"])])
        s.configure("Toolbutton", background=C["surface2"], foreground=C["muted"], lightcolor=C["surface2"],
                    darkcolor=C["surface2"], padding=(10, 7), anchor="center")
        s.map("Toolbutton", background=[("selected", C["accent"]), ("active", C["border"])],
              lightcolor=[("selected", C["accent"])], darkcolor=[("selected", C["accent"])],
              bordercolor=[("selected", C["accent"])], foreground=[("selected", "white")])
        s.configure("TCheckbutton", indicatorbackground=C["surface2"], indicatorforeground="white",
                    upperbordercolor=C["muted"], lowerbordercolor=C["muted"], indicatorsize=16,
                    indicatormargin=(0, 0, 10, 0))
        s.map("TCheckbutton", background=[], indicatorbackground=[("selected", C["accent"])])
        s.configure("TEntry", padding=7)
        s.configure("TCombobox", padding=6, background=C["surface2"])
        for w in ("TEntry", "TCombobox"):
            s.map(w, bordercolor=[("focus", C["accent"])], lightcolor=[("focus", C["accent"])])
        s.configure("Treeview", background=C["surface"], rowheight=30, borderwidth=0)
        s.map("Treeview", background=[("selected", C["accent"])], foreground=[("selected", "white")])
        s.configure("Treeview.Heading", background=C["surface2"], foreground=C["muted"], font=(BOLD, 8),
                    relief="flat", padding=(8, 6))
        s.map("Treeview.Heading", background=[("active", C["border"])])
        s.configure("Horizontal.TScale", background=C["accent"], bordercolor=C["border"],
                    lightcolor=C["surface2"], darkcolor=C["surface2"], gripcount=0)
        for opt, val in (("background", C["surface"]), ("foreground", C["text"]),
                         ("selectBackground", C["accent"]), ("font", (FONT, 10))):
            self.root.option_add(f"*TCombobox*Listbox.{opt}", val)  # lista desplegable del combobox

    def apply_state(self):
        """Colorea la ventana según esté captando audio o no."""
        running, on = self.engine.running, self.engine.capturing
        self.shown = (running, on)
        bg = C["listen"] if on else C["idle"]
        self.style.configure(".", background=bg)
        self.style.configure("Pill.TLabel", foreground=C["live"] if on else C["accent_hi"] if running else C["muted"])
        self.pill.config(text="●  ESCUCHANDO" if on else
                         f"●  MANTÉN {self.ptt_key.upper()} PARA HABLAR" if running else "●  DETENIDO")
        self.toggle_btn.config(text="■   Stop Listening" if running else "▶   Start Listening")
        self.meter.config(bg=bg)
        self.root.configure(bg=bg)
        try:  # barra de título del mismo color (Windows 11: DWMWA_BORDER/CAPTION/TEXT_COLOR)
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            for attr, color in ((34, bg), (35, bg), (36, C["text"])):
                ref = ctypes.c_int(int(color[5:7] + color[3:5] + color[1:3], 16))  # COLORREF 0x00BBGGRR
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ref), 4)
        except AttributeError:  # fuera de Windows
            pass

    def set_wait(self, value):
        self.wait = round(float(value))
        self.engine.wait = math.inf if self.wait == MAX_WAIT else self.wait
        self.wait_text.set("Al instante: pulsa en cuanto oye la palabra" if self.wait == 0 else
                           "Espera al final de la frase: máxima precisión" if self.wait == MAX_WAIT else
                           f"Confirma la palabra durante {self.wait * 100} ms antes de pulsar")

    def set_threshold(self, value):
        self.engine.threshold = value

    def use_recommended(self):
        if self.engine.recommended is None:
            return self.status.set("Inicia la escucha unos segundos para medir el ruido de fondo.")
        self.meter.set(self.engine.recommended)
        self.save()

    def set_mode(self):
        self.engine.ptt, self.engine.held = self.mode.get(), False
        self.save()
        self.apply_state()

    def set_ptt_key(self):
        name = self.ptt_entry.get().strip().lower()
        if name == self.ptt_key:
            return
        try:
            keys = self.mapper.parse(name)
            if len(keys) != 1:
                raise ValueError("Usa una sola tecla, sin combinaciones.")
        except ValueError as e:
            self.ptt_entry.delete(0, "end")
            self.ptt_entry.insert(0, self.ptt_key or "v")
            return messagebox.showerror("Tecla para hablar inválida", str(e))
        k = keys[0]
        first = self.ptt_key is None
        self.ptt_key = name
        self.ptt_target = self.keys.canonical(KeyCode.from_char(k) if isinstance(k, str) else k)
        if not first:
            self.save()
            self.apply_state()

    def on_key(self, key, injected, down):  # hilo de pynput
        if not injected and self.engine.ptt and self.keys.canonical(key) == self.ptt_target:
            self.engine.held = down

    def set_echo(self):
        self.engine.echo = self.echo_var.get()
        self.save()
        if self.engine.running:  # arranca/para el hilo que escucha las bocinas
            self.engine.start(self.mappings)

    def on_word(self, word):  # se ejecuta en el hilo del motor
        combo = self.mappings.get(word)
        if combo:
            try:
                self.mapper.press(combo)
                self.events.put(f"«{word}» → {combo}")
            except Exception as e:
                self.events.put(f"Error pulsando {combo}: {e}")

    def poll(self):
        while not self.events.empty():
            self.status.set(self.events.get())
        rec = self.engine.recommended
        self.meter.level = self.engine.level if self.engine.running else 0
        self.meter.recommended = rec
        self.meter.draw()
        self.threshold_text.set(f"Umbral: {self.engine.threshold} %\n" +
                                (f"Recomendado ▼: {rec} %" if rec is not None else "Recomendado: se mide al escuchar"))
        if (self.engine.running, self.engine.capturing) != self.shown:
            self.apply_state()
        self.root.after(100, self.poll)

    def add(self):
        word, combo = self.word.get().strip().lower(), self.key.get().strip().lower()
        if not word or " " in word:
            return messagebox.showerror("Palabra inválida", "Escribe una sola palabra.")
        try:
            self.mapper.parse(combo)
        except ValueError as e:
            return messagebox.showerror("Tecla inválida", str(e))
        self.mappings[word] = combo
        self.word.delete(0, "end")
        self.changed()

    def delete(self):
        for word in self.tree.selection():
            self.mappings.pop(word, None)
        self.changed()

    def save(self):
        cfg = {"wait": self.wait, "threshold": self.engine.threshold, "echo": self.engine.echo,
               "ptt": self.engine.ptt, "ptt_key": self.ptt_key, "mappings": self.mappings}
        try:
            CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("No se pudo guardar config.json", str(e))

    def changed(self):
        self.save()
        self.refresh()
        if self.engine.running:  # la gramática de vosk depende de las palabras: reiniciar
            self.engine.start(self.mappings)

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for i, (word, combo) in enumerate(sorted(self.mappings.items())):
            self.tree.insert("", "end", iid=word, values=(word, combo), tags=("odd",) if i % 2 else ())

    def toggle(self):
        if self.engine.running:
            self.engine.stop()
            self.status.set("Detenido")
        else:
            self.engine.start(self.mappings)
        self.apply_state()


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # nítido en pantallas con escalado
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    AppGUI(root)
    root.mainloop()
