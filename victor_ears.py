"""Victor v7 — oídos: VAD real (Silero) en vez de umbral de amplitud.

v6 disparaba grabación solo por RMS — el ruido de motor/tráfico de AC es de banda
ancha y alta amplitud, así que producía falsos positivos y grababa ruido en vez de
voz. Silero VAD analiza la ESTRUCTURA espectral de voz humana, no solo el volumen:
el ruido de motor (banda ancha, sin formantes) queda por debajo del umbral aunque
su RMS sea alto.

Diseño en dos capas:
  1. Prefiltro RMS barato (como v6) — evita correr la red neuronal en silencio total.
  2. Silero confirma "es voz humana" antes de considerar que arrancó una grabación.

SpeechGate es una máquina de estados pura (sin threads, sin sockets) para poder
testearla offline con audio sintetizado por Piper + ruido — ver tools/teach_victor.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

_SAMPLE_RATE = 16000
_VAD_FRAME = 512  # 32 ms @ 16kHz — tamaño de frame nativo de Silero VAD v5
_CONTEXT_SIZE = 64  # muestras de "cola" del frame anterior que el grafo v5 exige


class SileroVad:
    """Wrapper ONNX de Silero VAD v5. Stateful: llamar en orden, `reset()` entre clips.

    BUG histórico (encontrado 2026-07-05, ver spec v7): el wrapper oficial de Silero
    (OnnxWrapper.__call__ en el repo snakers4/silero-vad) concatena 64 muestras de
    CONTEXTO — la cola del frame anterior — delante de cada frame de 512 antes de
    llamar al modelo, dejando una entrada real de 576 muestras. Sin ese contexto la
    salida es prácticamente 0 para CUALQUIER entrada (silencio, tono, ruido e incluso
    voz humana real grabada — verificado con nexus_voice_test.wav: máx. prob 0.10 en
    8s de habla clara). NEXUS's barge_in.py tiene el mismo bug (mismo patrón de
    llamada, sin contexto) — no se tocó aquí porque pertenece a otro proyecto, pero
    vale la pena que Julian lo revise allá también.
    """

    def __init__(self, model_path: Path | str):
        import onnxruntime as ort
        # Sin esto, ORT reparte cada inferencia (frame de 32ms) entre todos los
        # cores lógicos y deja su pool de hilos "spin-waiting" entre llamadas —
        # con el mic siempre activo eso satura la CPU de forma sostenida (~250-300%
        # medido) y le roba ciclos a Wine/Proton mientras arranca Content Manager.
        # El propio proyecto silero-vad fija 1 hilo en sus ejemplos por esta razón:
        # el modelo es demasiado chico para beneficiarse de paralelismo.
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(model_path), sess_options=opts, providers=["CPUExecutionProvider"],
        )
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(_CONTEXT_SIZE, dtype=np.float32)

    def speech_prob(self, frame: np.ndarray) -> float:
        """frame: float32 mono a 16kHz, longitud exactamente _VAD_FRAME (512 muestras)."""
        if frame.shape[0] != _VAD_FRAME:
            raise ValueError(f"Silero VAD espera frames de {_VAD_FRAME} muestras, llegó {frame.shape[0]}")
        sr = np.array(_SAMPLE_RATE, dtype=np.int64)
        x = np.concatenate([self._context, frame]).astype(np.float32)
        out, self._state = self._session.run(
            None, {"input": x[np.newaxis, :], "state": self._state, "sr": sr},
        )
        self._context = x[-_CONTEXT_SIZE:]
        return float(out[0, 0])


def int16_to_float32(pcm: np.ndarray) -> np.ndarray:
    return (pcm.astype(np.float32) / 32768.0).clip(-1.0, 1.0)


def iter_vad_frames(pcm_int16: np.ndarray):
    """Parte un buffer int16 en frames de 512 muestras (descarta el resto final)."""
    n_frames = len(pcm_int16) // _VAD_FRAME
    audio = int16_to_float32(pcm_int16)
    for i in range(n_frames):
        yield audio[i * _VAD_FRAME:(i + 1) * _VAD_FRAME]


class SpeechGate:
    """Máquina de estados: decide cuándo empezar/terminar una grabación.

    Reemplaza el gate por amplitud pura de v6. Recibe frames de 512 muestras
    (int16) uno a uno vía `push()` y devuelve el evento que ocurrió, si alguno:
    "start" | "still_recording" | "stop" | None.
    """

    def __init__(
        self,
        vad: SileroVad,
        *,
        vad_threshold: float = 0.60,
        vad_end_threshold: float = 0.35,
        confirm_frames: int = 6,     # ~192ms de voz sostenida para arrancar
        silence_frames: int = 25,    # ~800ms de silencio sostenido para parar
        rms_prefilter: float = 250.0,
        max_frames: int = 375,       # ~12s tope duro
    ):
        self.vad = vad
        self.vad_threshold = vad_threshold
        self.vad_end_threshold = vad_end_threshold
        self.confirm_frames = confirm_frames
        self.silence_frames = silence_frames
        self.rms_prefilter = rms_prefilter
        self.max_frames = max_frames
        self._recording = False
        self._voice_run = 0
        self._silence_run = 0
        self._frames_recorded = 0

    def reset(self) -> None:
        self.vad.reset()
        self._recording = False
        self._voice_run = 0
        self._silence_run = 0
        self._frames_recorded = 0

    def push(self, frame_int16: np.ndarray) -> Optional[str]:
        rms = float(np.sqrt(np.mean(frame_int16.astype(np.float32) ** 2)))

        if not self._recording:
            # Prefiltro barato: sin energía suficiente, ni molestamos al VAD.
            if rms < self.rms_prefilter:
                self._voice_run = 0
                return None
            prob = self.vad.speech_prob(int16_to_float32(frame_int16))
            if prob >= self.vad_threshold:
                self._voice_run += 1
                if self._voice_run >= self.confirm_frames:
                    self._recording = True
                    self._voice_run = 0
                    self._silence_run = 0
                    self._frames_recorded = 0
                    return "start"
            else:
                self._voice_run = 0
            return None

        # Grabando: seguimos corriendo el VAD para saber cuándo parar.
        prob = self.vad.speech_prob(int16_to_float32(frame_int16))
        self._frames_recorded += 1
        if prob < self.vad_end_threshold:
            self._silence_run += 1
        else:
            self._silence_run = 0

        if self._silence_run >= self.silence_frames or self._frames_recorded >= self.max_frames:
            self._recording = False
            self._silence_run = 0
            return "stop"
        return "still_recording"

    @property
    def is_recording(self) -> bool:
        return self._recording
