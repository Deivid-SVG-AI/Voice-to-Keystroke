"""python test_main.py — voz sintetizada (SAPI, voz es-ES) en lugar del micrófono."""
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

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

wav = Path(tempfile.gettempdir(), "v2k_test.wav")
subprocess.run(["powershell", "-NoProfile", "-Command", f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoiceByHints('NotSet', 'NotSet', 0, [Globalization.CultureInfo]'es-ES')
$s.SetOutputToWaveFile('{wav}', (New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo 16000, 'Sixteen', 'Mono'))
$s.Speak('saltar. hoy hace buen tiempo en la ciudad. disparar. saltar saltar.')
$s.Dispose()"""], check=True)
audio = wave.open(str(wav)).readframes(10**9) + b"\0" * main.RATE * 2  # + 1 s de silencio


class FakeMic:  # sustituye a sd.RawInputStream; al acabar el audio para el motor
    def __init__(self, **kw): self.pos = 0
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self, frames):
        chunk, self.pos = audio[self.pos:self.pos + frames * 2], self.pos + frames * 2
        if not chunk:
            engine.stop()
            done.set()
        return chunk or b"\0" * frames * 2, False


main.sd.RawInputStream = FakeMic
heard, done = [], threading.Event()
engine = main.VoiceEngine(heard.append, print)
engine.start(["saltar", "disparar"])
assert done.wait(120), "timeout"
print("oído:", heard)
assert heard == ["saltar", "disparar", "saltar", "saltar"], heard
print("OK")
