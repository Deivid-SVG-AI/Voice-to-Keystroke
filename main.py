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
    Si la ventana destino se ejecuta como administrador, esta app también debe hacerlo.
"""
import json
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
vosk.SetLogLevel(-1)


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
                with sd.RawInputStream(samplerate=RATE, blocksize=RATE // 10, dtype="int16", channels=1) as stream:
                    say(f"Escuchando... (fuera del vocabulario, se ignoran: {', '.join(missing)})"
                        if missing else "Escuchando...")
                    fired = 0  # palabras de la frase actual ya disparadas
                    while not stop.is_set():
                        data, _ = stream.read(RATE // 10)
                        final = rec.AcceptWaveform(bytes(data))
                        res = json.loads(rec.Result() if final else rec.PartialResult())
                        heard = [w for w in res.get("text" if final else "partial", "").split() if w != "[unk]"]
                        # ponytail: disparamos con resultados parciales (~100 ms) en vez de esperar el
                        # silencio final; si vosk corrige un parcial, esa tecla ya se pulsó.
                        for w in heard[fired:]:
                            self.on_word(w)
                        fired = 0 if final else max(fired, len(heard))
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
            self.mappings = json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.mappings = {}

        root.title("Voice-to-Keystroke")
        form = ttk.Frame(root, padding=8)
        form.pack(fill="x")
        ttk.Label(form, text="Palabra").grid(row=0, column=0, sticky="w")
        ttk.Label(form, text="Tecla").grid(row=0, column=1, sticky="w")
        self.word = ttk.Entry(form, width=20)
        self.word.grid(row=1, column=0, padx=(0, 6))
        self.key = ttk.Combobox(form, values=self.KEY_CHOICES, width=18)
        self.key.grid(row=1, column=1, padx=(0, 6))
        ttk.Button(form, text="Agregar", command=self.add).grid(row=1, column=2)
        self.word.bind("<Return>", lambda e: self.add())
        self.key.bind("<Return>", lambda e: self.add())

        self.tree = ttk.Treeview(root, columns=("word", "key"), show="headings", height=10)
        self.tree.heading("word", text="Palabra")
        self.tree.heading("key", text="Tecla")
        self.tree.pack(fill="both", expand=True, padx=8)
        self.tree.bind("<Delete>", lambda e: self.delete())

        bar = ttk.Frame(root, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="Eliminar seleccionada", command=self.delete).pack(side="left")
        self.toggle_btn = ttk.Button(bar, text="Start Listening", command=self.toggle)
        self.toggle_btn.pack(side="right")
        self.status = tk.StringVar(value="Detenido")
        ttk.Label(root, textvariable=self.status, padding=(8, 0, 8, 8)).pack(fill="x")

        self.refresh()
        self.poll()

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

    def changed(self):
        try:
            CONFIG.write_text(json.dumps(self.mappings, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("No se pudo guardar config.json", str(e))
        self.refresh()
        if self.engine.running:  # la gramática de vosk depende de las palabras: reiniciar
            self.engine.start(self.mappings)

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for word, combo in sorted(self.mappings.items()):
            self.tree.insert("", "end", iid=word, values=(word, combo))

    def toggle(self):
        if self.engine.running:
            self.engine.stop()
            self.toggle_btn.config(text="Start Listening")
            self.status.set("Detenido")
        else:
            self.engine.start(self.mappings)
            self.toggle_btn.config(text="Stop Listening")


if __name__ == "__main__":
    root = tk.Tk()
    AppGUI(root)
    root.mainloop()
