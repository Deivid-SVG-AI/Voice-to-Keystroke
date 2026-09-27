"""
Voice-to-Keystroke: di una palabra clave y se pulsa la tecla asignada.

Instalación (Windows):
    python -m venv .venv
    .venv\\Scripts\\activate
    pip install vosk sounddevice pynput

    Se usa sounddevice en lugar de pyaudio: trae PortAudio incluido y tiene wheels
    para cualquier versión de Python (pyaudio no las publica para 3.14).
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
    Slider Velocidad/Precisión: cuánto tiempo debe mantenerse reconocida una palabra
    antes de pulsar la tecla (0 = al instante, máximo = al terminar la frase).
    Si la ventana destino se ejecuta como administrador, esta app también debe hacerlo.
"""
import ctypes
import json
import math
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import sounddevice as sd
import vosk
from pynput.keyboard import Controller, Key, KeyCode

CONFIG = Path(__file__).with_name("config.json")
RATE = 16000  # los modelos de vosk están entrenados a 16 kHz
CHUNK = RATE // 10  # 100 ms de audio por lectura
MAX_WAIT = 10  # posición "precisión máxima" del slider: solo el resultado final de vosk
vosk.SetLogLevel(-1)

C = {  # paleta azul oscuro
    "idle": "#0a1222", "listen": "#0b2a57", "surface": "#111d33", "surface2": "#182844",
    "row": "#142340", "border": "#26395c", "text": "#e4ecfa", "muted": "#7f93b5",
    "accent": "#3b82f6", "accent_hi": "#60a5fa", "live": "#38d9f5",
}
FONT = "Segoe UI"
BOLD = "Segoe UI Semibold"


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


class VoiceEngine:
    """Escucha el micrófono en un hilo aparte y llama on_word(palabra) por cada palabra clave."""

    def __init__(self, on_word, on_status):
        self.on_word, self.on_status = on_word, on_status
        self.model = None
        # Lecturas de 100 ms que una palabra debe seguir reconocida antes de disparar
        # (0 = al instante; math.inf = solo con el resultado final, tras el silencio).
        self.wait = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._stop.set()

    @property
    def running(self):
        return not self._stop.is_set()

    def start(self, words):
        self.stop()
        # Un Event por hilo: si el anterior sigue cargando el modelo, muere solo sin revivir.
        self._stop = threading.Event()
        threading.Thread(target=self._run, args=(sorted(words), self._stop), daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self, words, stop):
        say = lambda msg: stop.is_set() or self.on_status(msg)
        while not stop.is_set():
            try:
                with self._lock:
                    if self.model is None:
                        say("Cargando modelo (la 1ª vez se descarga, ~40 MB)...")
                        self.model = vosk.Model(lang="es")
                missing = [w for w in words if self.model.vosk_model_find_word(w) < 0]
                # Gramática cerrada = solo se buscan nuestras palabras: mucho más rápido y preciso.
                rec = vosk.KaldiRecognizer(self.model, RATE, json.dumps(words + ["[unk]"], ensure_ascii=False))
                with sd.RawInputStream(samplerate=RATE, blocksize=CHUNK, dtype="int16", channels=1) as stream:
                    say(f"Escuchando... (fuera del vocabulario, se ignoran: {', '.join(missing)})"
                        if missing else "Escuchando...")
                    fired, pending, age = 0, [], 0
                    while not stop.is_set():
                        data, _ = stream.read(CHUNK)
                        final = rec.AcceptWaveform(bytes(data))
                        res = json.loads(rec.Result() if final else rec.PartialResult())
                        heard = [w for w in res.get("text" if final else "partial", "").split() if w != "[unk]"]
                        new = heard[fired:]  # palabras de la frase actual aún sin disparar
                        age = age + 1 if new == pending else 0
                        pending = new
                        # ponytail: si vosk corrige una palabra ya disparada, esa tecla ya se pulsó;
                        # subir self.wait (precisión) lo evita a cambio de latencia.
                        if new and (final or age >= self.wait):
                            for w in new:
                                self.on_word(w)
                            fired += len(new)
                        if final:
                            fired, pending, age = 0, [], 0
            except Exception as e:  # micrófono desconectado/ocupado, etc.: avisar y reintentar
                say(f"Error: {e} — reintentando en 2 s")
                stop.wait(2)


class AppGUI:
    KEY_CHOICES = ["space", "enter", "tab", "esc", "backspace", "up", "down", "left", "right",
                   *[f"num{i}" for i in range(10)], "ctrl+c", "ctrl+v"]

    def __init__(self, root):
        self.root = root
        self.mapper = KeyMapper()
        self.events = queue.Queue()  # hilo del motor -> GUI (tkinter no es thread-safe)
        self.engine = VoiceEngine(self.on_word, self.events.put)
        try:
            data = json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if "mappings" not in data:  # config.json antiguo: solo el diccionario de reglas
            data = {"mappings": data}
        self.mappings = data["mappings"]

        root.title("Voice-to-Keystroke")
        root.minsize(480, 560)
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

        form = ttk.Frame(wrap)
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

        self.tree = ttk.Treeview(wrap, columns=("word", "key"), show="headings", height=7)
        self.tree.heading("word", text="PALABRA", anchor="w")
        self.tree.heading("key", text="TECLA", anchor="w")
        self.tree.tag_configure("odd", background=C["row"])
        self.tree.pack(fill="both", expand=True, pady=(18, 8))
        self.tree.bind("<Delete>", lambda e: self.delete())
        ttk.Button(wrap, text="Eliminar seleccionada", command=self.delete).pack(anchor="e")

        tune = ttk.Frame(wrap)
        tune.pack(fill="x", pady=(18, 0))
        tune.columnconfigure(1, weight=1)
        ttk.Label(tune, text="VELOCIDAD", style="Caption.TLabel").grid(row=0, column=0)
        self.scale = ttk.Scale(tune, from_=0, to=MAX_WAIT, command=self.set_wait)
        self.scale.grid(row=0, column=1, sticky="ew", padx=12)
        self.scale.bind("<ButtonRelease-1>", lambda e: self.save())
        ttk.Label(tune, text="PRECISIÓN", style="Caption.TLabel").grid(row=0, column=2)
        self.wait_text = tk.StringVar()
        ttk.Label(tune, textvariable=self.wait_text, style="Muted.TLabel").grid(row=1, column=0, columnspan=3, pady=(6, 0))
        self.scale.set(data.get("wait", 0))
        self.set_wait(data.get("wait", 0))

        self.toggle_btn = ttk.Button(wrap, style="Accent.TButton", command=self.toggle)
        self.toggle_btn.pack(fill="x", pady=(20, 0))
        self.status = tk.StringVar(value="Detenido")
        ttk.Label(wrap, textvariable=self.status, style="Muted.TLabel").pack(anchor="w", pady=(10, 0))

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
        """Colorea la ventana según esté escuchando o no."""
        on = self.engine.running
        bg = C["listen"] if on else C["idle"]
        self.style.configure(".", background=bg)
        self.style.configure("Pill.TLabel", foreground=C["live"] if on else C["muted"])
        self.pill.config(text="●  ESCUCHANDO" if on else "●  DETENIDO")
        self.toggle_btn.config(text="■   Stop Listening" if on else "▶   Start Listening")
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
        try:
            CONFIG.write_text(json.dumps({"wait": self.wait, "mappings": self.mappings},
                                         ensure_ascii=False, indent=2), encoding="utf-8")
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
