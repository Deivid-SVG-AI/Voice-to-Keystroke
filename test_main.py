"""python test_main.py — voz sintetizada (SAPI, voz es-ES) en lugar del micrófono."""
import array
import math
import subprocess
import sys
import tempfile
import threading
import time
import types
import wave
from pathlib import Path

import numpy as np
from pynput.keyboard import Key, KeyCode

import main

m = main.KeyMapper()
assert m.parse("espacio") == [Key.space]
assert m.parse("Ctrl + C") == [Key.ctrl, "c"]
assert m.parse("num7") == [KeyCode.from_vk(0x67)]
for bad in ("", "numx", "banana"):
    try:
        m.parse(bad)
        raise AssertionError(bad)
    except ValueError:
        pass
V = KeyCode.from_char("v")  # pulsar para hablar: on_key solo usa estos atributos, sin Tk
gui = types.SimpleNamespace(engine=types.SimpleNamespace(ptt=True, held=False), ptt_target=V,
                            keys=types.SimpleNamespace(canonical=lambda k: k), ptt_toggle=False, ptt_down=False)
key = lambda down, injected=False: main.AppGUI.on_key(gui, V, injected, down)
key(True); assert gui.engine.held
key(False); assert not gui.engine.held  # mantener para hablar
gui.ptt_toggle = True
for down in (True, True, True, False):  # alternar, con la repetición automática de la tecla
    key(down)
assert gui.engine.held
key(True); key(False); assert not gui.engine.held
key(True, injected=True); assert not gui.engine.held  # las teclas que pulsa la propia app no cuentan
assert main.chunk_level(b"\0\0" * 1600) == 0
assert main.chunk_level(array.array("h", [32767, -32767] * 800).tobytes()) == 100

wav = Path(tempfile.gettempdir(), "v2k_test.wav")
subprocess.run(["powershell", "-NoProfile", "-Command", f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoiceByHints('NotSet', 'NotSet', 0, [Globalization.CultureInfo]'es-ES')
$s.SetOutputToWaveFile('{wav}', (New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo 16000, 'Sixteen', 'Mono'))
$s.Speak('saltar. hoy hace buen tiempo en la ciudad. disparar. saltar saltar.')
$s.Dispose()"""], check=True)
audio = wave.open(str(wav)).readframes(10**9) + b"\0" * main.RATE * 2  # + 1 s de silencio


def scaled(gain):
    """audio con cada muestra multiplicada por gain(segundo)."""
    s = array.array("h", audio)
    for i in range(len(s)):
        s[i] = int(s[i] * gain(i / main.RATE))
    return s.tobytes()


in_disparar = lambda t: 3.0 <= t < 4.6  # tramo del audio donde se dice "disparar"
quiet_disparar = scaled(lambda t: 0.03 if in_disparar(t) else 1)  # -30 dB: como el eco de unas bocinas
only_disparar = scaled(lambda t: 1 if in_disparar(t) else 0)


class FakeMic:  # sustituye a sd.RawInputStream; al acabar el audio para el motor
    def __init__(self, **kw):
        global mic
        mic, self.pos = self, 0
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self, frames):
        chunk, self.pos = mic_audio[self.pos:self.pos + frames * 2], self.pos + frames * 2
        if ptt_until is not None:
            engine.held = self.pos / (main.RATE * 2) < ptt_until
        if pace:
            time.sleep(0.1)
        if not chunk:
            engine.stop()
            done.set()
        return chunk or b"\0" * frames * 2, False


class FakeSpeakers:  # sustituye al loopback de soundcard: por las bocinas "suena" only_disparar
    name = "bocinas de prueba"
    def recorder(self, **kw): return self
    def __enter__(self):
        self.pos = 0
        return self
    def __exit__(self, *a): pass
    def record(self, frames):
        time.sleep(0.1)
        chunk, self.pos = only_disparar[self.pos:self.pos + frames * 2], self.pos + frames * 2
        return (np.frombuffer(chunk.ljust(frames * 2, b"\0"), "<i2") / 32768).reshape(-1, 1)


speakers = FakeSpeakers()
sys.modules["soundcard"] = types.SimpleNamespace(default_speaker=lambda: speakers,
                                                 get_microphone=lambda *a, **kw: speakers)
main.sd.RawInputStream = FakeMic
heard, statuses = [], []
engine = main.VoiceEngine(lambda w: heard.append((w, mic.pos / (main.RATE * 2))), statuses.append)


def run(a=audio, wait=0, threshold=0, ptt=None, echo=False):
    global mic_audio, ptt_until, pace, done
    mic_audio, ptt_until, pace, done = a, ptt, echo, threading.Event()
    heard.clear()
    engine.wait, engine.threshold, engine.ptt, engine.echo, engine.held = wait, threshold, ptt is not None, echo, False
    engine.start(["saltar", "disparar"])
    assert done.wait(120), "timeout"
    return [w for w, _ in heard]


ALL = ["saltar", "disparar", "saltar", "saltar"]
first_fire = {}
for wait in (0, 3, math.inf):  # velocidad máxima, intermedio, precisión máxima
    assert run(wait=wait) == ALL, (wait, heard)
    first_fire[wait] = heard[0][1]
    print(f"wait={wait}: primer 'saltar' pulsado a {first_fire[wait]:.1f} s del audio")
assert first_fire[0] < first_fire[3] < first_fire[math.inf], first_fire

for wait in (0, math.inf):  # umbral: el "disparar" atenuado se reconoce, pero no llega al 45 %
    assert run(quiet_disparar, wait=wait) == ALL, heard
    assert run(quiet_disparar, wait=wait, threshold=45) == ["saltar"] * 3, (wait, heard)
    assert any("disparar» ignorada: volumen" in s for s in statuses)
print("umbral de volumen: OK")

assert run(ptt=2.0) == ["saltar"], heard  # tecla pulsada solo durante los 2 primeros segundos
assert run(ptt=2.0, wait=math.inf) == ["saltar"], heard  # dispara al soltar la tecla
print("pulsar para hablar: OK")

assert run(echo=True) == ["saltar"] * 3, heard  # "disparar" también sonó en las bocinas
assert any("disparar» ignorada: sonó en las bocinas" in s for s in statuses)
print("filtro de bocinas: OK")
print("OK")
