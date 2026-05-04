from __future__ import annotations

import argparse
import math
import os
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import sounddevice as sd
import webrtcvad
from deep_translator import GoogleTranslator
from faster_whisper import WhisperModel
from faster_whisper.utils import download_model
import tkinter as tk
from tkinter import Tk, StringVar, messagebox, ttk
from tkinter.scrolledtext import ScrolledText


APP_NAME = "Live Translator"
DEFAULT_MODEL = "base.en"
MODEL_PRESETS = {
    "Fast (tiny.en)": "tiny.en",
    "Balanced (base.en)": "base.en",
    "Accurate (small.en)": "small.en",
}
SAMPLE_RATE = 16_000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2
MAX_HISTORY_CHARS = 30_000
MODEL_CACHE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "LiveTranslator" / "models"
PALETTE = {
    "bg": "#f4efe8",
    "panel": "#fffaf3",
    "panel_soft": "#f7f0e5",
    "panel_alt": "#fdf6ea",
    "border": "#dccdb8",
    "editor": "#fffdf9",
    "ink": "#183244",
    "muted": "#5e6d7d",
    "muted_soft": "#8b97a4",
    "accent": "#0f8b8d",
    "accent_deep": "#0b6f72",
    "accent_soft": "#d8f0ec",
    "sun": "#f2b35c",
    "coral": "#e27a5f",
    "coral_soft": "#f7ded4",
    "danger": "#be5b45",
    "shadow": "#eadfce",
}
STATE_COLORS = {
    "ready": "#7a8c9d",
    "loading": "#4f7cac",
    "listening": PALETTE["accent"],
    "transcribing": PALETTE["sun"],
    "translating": PALETTE["coral"],
    "stopped": "#8c7c68",
    "error": PALETTE["danger"],
}


@dataclass(frozen=True)
class SegmentResult:
    timestamp: str
    english: str
    spanish: str
    audio_ms: int
    transcribe_ms: int
    translate_ms: int


@dataclass(frozen=True)
class InputDevice:
    index: int
    label: str


def runtime_root() -> Path:
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def bundled_model_path(model_name: str) -> Path | None:
    candidate = runtime_root() / "models" / model_name
    return candidate if candidate.exists() else None


def normalize_text(text: str) -> str:
    return " ".join(text.split()).strip()


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def blend_hex(start: str, end: str, factor: float) -> str:
    factor = clamp(factor)
    start_rgb = tuple(int(start[index : index + 2], 16) for index in (1, 3, 5))
    end_rgb = tuple(int(end[index : index + 2], 16) for index in (1, 3, 5))
    mixed = tuple(int(round(a + (b - a) * factor)) for a, b in zip(start_rgb, end_rgb))
    return "#{:02x}{:02x}{:02x}".format(*mixed)


def append_history(
    widget: ScrolledText,
    chunks: list[tuple[str, str | tuple[str, ...] | None]],
) -> None:
    widget.configure(state="normal")
    for text, tags in chunks:
        if tags is None:
            widget.insert("end", text)
        elif isinstance(tags, str):
            widget.insert("end", text, (tags,))
        else:
            widget.insert("end", text, tags)
    if float(widget.index("end-1c").split(".")[0]) > 600:
        widget.delete("1.0", "160.0")
    if int(widget.count("1.0", "end", "chars")[0]) > MAX_HISTORY_CHARS:
        widget.delete("1.0", "120.0")
    widget.see("end")
    widget.configure(state="disabled")


def list_input_devices() -> list[InputDevice]:
    devices = sd.query_devices()
    host_apis = sd.query_hostapis()
    input_devices: list[InputDevice] = []
    for index, device in enumerate(devices):
        if int(device["max_input_channels"]) < 1:
            continue
        host_name = host_apis[int(device["hostapi"])]["name"]
        label = f"{index}: {device['name']} [{host_name}]"
        input_devices.append(InputDevice(index=index, label=label))
    return input_devices


def default_input_device() -> int | None:
    default_devices = sd.default.device
    if not default_devices:
        return None
    input_index = default_devices[0]
    if input_index is None or input_index < 0:
        return None
    return int(input_index)


def ensure_project_model(model_name: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    download_model(model_name, output_dir=str(output_dir))
    return output_dir


class SpeechSegmenter:
    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        frame_ms: int = FRAME_MS,
        aggressiveness: int = 3,
        start_trigger_frames: int = 3,
        end_trigger_frames: int = 14,
        min_speech_frames: int = 9,
        pre_roll_frames: int = 8,
        max_segment_seconds: int = 12,
        minimum_rms: float = 520.0,
        start_energy_ratio: float = 2.4,
        continue_energy_ratio: float = 1.7,
        min_voiced_ratio: float = 0.34,
        min_peak_rms: float = 860.0,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.start_trigger_frames = start_trigger_frames
        self.end_trigger_frames = end_trigger_frames
        self.min_speech_frames = min_speech_frames
        self.max_segment_frames = int(max_segment_seconds * 1000 / frame_ms)
        self.minimum_rms = minimum_rms
        self.start_energy_ratio = start_energy_ratio
        self.continue_energy_ratio = continue_energy_ratio
        self.min_voiced_ratio = min_voiced_ratio
        self.min_peak_rms = min_peak_rms
        self.vad = webrtcvad.Vad(aggressiveness)
        self.pre_roll: deque[tuple[bytes, float, bool]] = deque(maxlen=pre_roll_frames)
        self.current_frames: list[bytes] = []
        self.speaking = False
        self.voiced_run = 0
        self.voiced_frames = 0
        self.silence_run = 0
        self.frame_count = 0
        self.energy_sum = 0.0
        self.peak_rms = 0.0
        self.noise_floor_rms = 180.0

    def push(self, frame: bytes) -> tuple[bytes, int] | None:
        rms = self._frame_rms(frame)
        start_threshold = max(self.minimum_rms, self.noise_floor_rms * self.start_energy_ratio)
        continue_threshold = max(self.minimum_rms * 0.8, self.noise_floor_rms * self.continue_energy_ratio)
        vad_speech = self.vad.is_speech(frame, self.sample_rate)
        energy_threshold = continue_threshold if self.speaking else start_threshold
        is_speech = vad_speech and rms >= energy_threshold

        if not self.speaking:
            self.pre_roll.append((frame, rms, is_speech))
            self.voiced_run = self.voiced_run + 1 if is_speech else 0
            if not is_speech:
                self._update_noise_floor(rms)
            if self.voiced_run >= self.start_trigger_frames:
                self.speaking = True
                self.current_frames = [chunk for chunk, _rms, _flag in self.pre_roll]
                self.frame_count = len(self.current_frames)
                self.voiced_frames = sum(1 for _chunk, _rms, flag in self.pre_roll if flag)
                self.energy_sum = sum(chunk_rms for _chunk, chunk_rms, _flag in self.pre_roll)
                self.peak_rms = max((chunk_rms for _chunk, chunk_rms, _flag in self.pre_roll), default=0.0)
                self.silence_run = 0
                self.pre_roll.clear()
            return None

        self.current_frames.append(frame)
        self.frame_count += 1
        self.energy_sum += rms
        self.peak_rms = max(self.peak_rms, rms)
        if is_speech:
            self.voiced_frames += 1
            self.silence_run = 0
        else:
            self.silence_run += 1

        if self.silence_run >= self.end_trigger_frames or self.frame_count >= self.max_segment_frames:
            segment = self._finish()
            if segment is None:
                return None
            return segment

        return None

    def flush(self) -> tuple[bytes, int] | None:
        return self._finish()

    def _finish(self) -> tuple[bytes, int] | None:
        if not self.speaking:
            self._reset()
            return None

        segment = b"".join(self.current_frames)
        frame_count = self.frame_count
        voiced_frames = self.voiced_frames
        average_rms = self.energy_sum / max(frame_count, 1)
        voiced_ratio = voiced_frames / max(frame_count, 1)
        peak_rms = self.peak_rms
        self._reset()

        if voiced_frames < self.min_speech_frames:
            return None
        if voiced_ratio < self.min_voiced_ratio:
            return None
        if peak_rms < self.min_peak_rms:
            return None
        if average_rms < max(self.minimum_rms * 0.72, self.noise_floor_rms * 1.2):
            return None

        return segment, frame_count * self.frame_ms

    def _reset(self) -> None:
        self.current_frames = []
        self.speaking = False
        self.voiced_run = 0
        self.voiced_frames = 0
        self.silence_run = 0
        self.frame_count = 0
        self.energy_sum = 0.0
        self.peak_rms = 0.0

    def _frame_rms(self, frame: bytes) -> float:
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        if samples.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(samples * samples)))

    def _update_noise_floor(self, rms: float) -> None:
        ceiling = max(self.minimum_rms * 2.3, self.noise_floor_rms * 3.0)
        if rms > ceiling:
            return
        self.noise_floor_rms = clamp(
            (self.noise_floor_rms * 0.94) + (rms * 0.06),
            80.0,
            2400.0,
        )


class LiveTranslatorEngine:
    def __init__(self, events: "queue.Queue[dict[str, Any]]") -> None:
        self.events = events
        self.stop_event = threading.Event()
        self.audio_queue: "queue.Queue[bytes]" = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.stream: sd.RawInputStream | None = None
        self.model_lock = threading.Lock()
        self.model_name = ""
        self.model: WhisperModel | None = None
        self.translation_client: GoogleTranslator | None = None
        self.running = False
        self.last_level_emit = 0.0

    def preload_model(self, model_name: str) -> None:
        threading.Thread(target=self._ensure_model, args=(model_name,), daemon=True).start()

    def start(self, device_index: int, model_name: str) -> None:
        if self.running:
            return
        self.stop_event.clear()
        self.audio_queue = queue.Queue()
        self.running = True
        self.worker_thread = threading.Thread(
            target=self._run_session,
            args=(device_index, model_name),
            daemon=True,
        )
        self.worker_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.stream is not None:
            try:
                self.stream.abort(ignore_errors=True)
            except Exception:
                pass
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=2)
        self.stream = None
        self.worker_thread = None
        self.running = False

    def _emit(self, kind: str, **payload: Any) -> None:
        self.events.put({"kind": kind, **payload})

    def _ensure_model(self, model_name: str) -> WhisperModel:
        with self.model_lock:
            if self.model is not None and self.model_name == model_name:
                return self.model

            source = bundled_model_path(model_name)
            model_source = str(source) if source else model_name
            self._emit(
                "status",
                headline=f"Loading {model_name}...",
                detail="Preparing local speech recognition.",
            )
            MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self.model = WhisperModel(
                model_source,
                device="cpu",
                compute_type="int8",
                cpu_threads=max(4, min(8, os.cpu_count() or 4)),
                download_root=str(MODEL_CACHE_DIR),
            )
            self.model_name = model_name
            return self.model

    def _translator(self) -> GoogleTranslator:
        if self.translation_client is None:
            self.translation_client = GoogleTranslator(source="en", target="es")
        return self.translation_client

    def _run_session(self, device_index: int, model_name: str) -> None:
        try:
            model = self._ensure_model(model_name)
            self._emit(
                "status",
                headline="Checking microphone...",
                detail="Opening the selected input device.",
            )
            sd.check_input_settings(device=device_index, channels=1, samplerate=SAMPLE_RATE, dtype="int16")

            def audio_callback(indata: Any, frames: int, time_info: Any, status: sd.CallbackFlags) -> None:
                if status:
                    self._emit("status", headline="Listening...", detail=f"Audio status: {status}")
                if self.stop_event.is_set():
                    raise sd.CallbackAbort
                frame = bytes(indata)
                if len(frame) == FRAME_BYTES:
                    now = time.perf_counter()
                    if now - self.last_level_emit >= 0.06:
                        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
                        rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
                        self._emit("level", value=clamp(rms / 9000.0))
                        self.last_level_emit = now
                    self.audio_queue.put(frame)

            self.stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=FRAME_SAMPLES,
                device=device_index,
                channels=1,
                dtype="int16",
                callback=audio_callback,
            )
            self.stream.start()
            self._emit(
                "status",
                headline="Listening...",
                detail="Speak English into the microphone. Spanish will appear after each phrase.",
            )

            segmenter = SpeechSegmenter()
            while not self.stop_event.is_set():
                try:
                    frame = self.audio_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                segment = segmenter.push(frame)
                if segment is None:
                    continue
                self._handle_segment(model, *segment)

            remaining = segmenter.flush()
            if remaining is not None:
                self._handle_segment(model, *remaining)
        except Exception as exc:
            self._emit("error", message=str(exc))
        finally:
            if self.stream is not None:
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception:
                    pass
            self.stream = None
            self.running = False
            self._emit("level", value=0.0)
            if self.stop_event.is_set():
                self._emit("status", headline="Stopped", detail="Microphone capture is idle.")

    def _handle_segment(self, model: WhisperModel, segment_bytes: bytes, audio_ms: int) -> None:
        self._emit("status", headline="Transcribing...", detail="Converting the last phrase to text.")
        audio = np.frombuffer(segment_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        started = time.perf_counter()
        segments, _ = model.transcribe(
            audio,
            language="en",
            task="transcribe",
            beam_size=1,
            best_of=1,
            temperature=0.0,
            compression_ratio_threshold=2.0,
            log_prob_threshold=-0.8,
            no_speech_threshold=0.45,
            condition_on_previous_text=False,
            vad_filter=False,
            without_timestamps=True,
        )
        segments = list(segments)
        english = normalize_text(" ".join(segment.text for segment in segments))
        transcribe_ms = int((time.perf_counter() - started) * 1000)

        if not english:
            self._emit("status", headline="Listening...", detail="Ready for the next phrase.")
            return
        if self._should_ignore_transcript(segments, english, audio_ms):
            self._emit("status", headline="Listening...", detail="Ignored likely background noise.")
            return

        self._emit("status", headline="Translating...", detail="Sending the English phrase to Spanish translation.")
        translate_started = time.perf_counter()
        try:
            spanish = normalize_text(self._translator().translate(english))
        except Exception as exc:
            spanish = "[Translation unavailable]"
            self._emit("status", headline="Listening...", detail=f"Translation error: {exc}")
        translate_ms = int((time.perf_counter() - translate_started) * 1000)

        self._emit(
            "result",
            result=SegmentResult(
                timestamp=datetime.now().strftime("%H:%M:%S"),
                english=english,
                spanish=spanish,
                audio_ms=audio_ms,
                transcribe_ms=transcribe_ms,
                translate_ms=translate_ms,
            ),
        )
        self._emit(
            "status",
            headline="Listening...",
            detail=(
                f"Last phrase: {audio_ms} ms audio | {transcribe_ms} ms transcribe | "
                f"{translate_ms} ms translate"
            ),
        )

    def _should_ignore_transcript(
        self,
        segments: list[Any],
        english: str,
        audio_ms: int,
    ) -> bool:
        alpha_chars = sum(1 for char in english if char.isalpha())
        words = [token.strip(".,!?;:()[]{}\"'") for token in english.split()]
        words = [token for token in words if token]
        avg_logprob = sum(segment.avg_logprob for segment in segments) / max(len(segments), 1)
        max_no_speech = max((segment.no_speech_prob for segment in segments), default=0.0)
        filler_words = {"uh", "um", "hmm", "hm", "mm", "mmm"}
        lowered = english.lower().strip()

        if alpha_chars < 2:
            return True
        if lowered in filler_words:
            return True
        if max_no_speech >= 0.82 and avg_logprob <= -0.7:
            return True
        if max_no_speech >= 0.72 and avg_logprob <= -0.95:
            return True
        if audio_ms < 420 and len(words) <= 1 and alpha_chars <= 4:
            return True
        return False


class LiveTranslatorApp:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title(APP_NAME)
        self.root.geometry("1280x820")
        self.root.minsize(980, 620)
        self.root.configure(bg=PALETTE["bg"])

        self.events: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.engine = LiveTranslatorEngine(self.events)
        self.devices: list[InputDevice] = []

        self.visual_state = "ready"
        self.status_var = StringVar(value="Listo")
        self.control_var = StringVar(value="Start")
        self.level_target = 0.0
        self.level_value = 0.0
        self.capture_requested = False
        self.transcript_flash = 0.0
        self.has_transcript = False

        self._build_ui()
        self.refresh_devices()
        self.root.bind("<space>", self._handle_space_toggle)
        self.root.bind("<Escape>", self._handle_escape_stop)
        self.root.bind("<Control-l>", self._handle_clear)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(120, self.poll_events)
        self.root.after(40, self.animate_ui)
        self.engine.preload_model(DEFAULT_MODEL)

    def _build_ui(self) -> None:
        self._configure_styles()

        container = tk.Frame(self.root, bg=PALETTE["bg"], padx=18, pady=18)
        container.pack(fill="both", expand=True)

        top_bar = tk.Frame(container, bg=PALETTE["bg"])
        top_bar.pack(fill="x", pady=(0, 14))

        status_wrap = tk.Frame(top_bar, bg=PALETTE["bg"])
        status_wrap.pack(side="left", fill="x", expand=True)

        self.status_chip = tk.Label(
            status_wrap,
            textvariable=self.status_var,
            bg=PALETTE["accent_soft"],
            fg=PALETTE["accent_deep"],
            font=("Bahnschrift SemiBold", 11),
            padx=12,
            pady=5,
        )
        self.status_chip.pack(anchor="w")

        self.motion_canvas = tk.Canvas(
            status_wrap,
            width=260,
            height=34,
            bg=PALETTE["bg"],
            bd=0,
            highlightthickness=0,
        )
        self.motion_canvas.pack(fill="x", expand=True, pady=(10, 0), padx=(0, 16))
        self.motion_track_id = self.motion_canvas.create_rectangle(
            0,
            12,
            260,
            22,
            fill=blend_hex(PALETTE["shadow"], PALETTE["panel"], 0.1),
            outline="",
        )
        self.motion_fill_id = self.motion_canvas.create_rectangle(0, 12, 0, 22, fill=PALETTE["accent"], outline="")
        self.motion_tip_id = self.motion_canvas.create_rectangle(0, 12, 0, 22, fill=PALETTE["panel"], outline="")
        self.motion_dot_ids = [
            self.motion_canvas.create_oval(0, 0, 0, 0, fill=PALETTE["accent"], outline="")
            for _ in range(6)
        ]

        self.control_button = ttk.Button(
            top_bar,
            textvariable=self.control_var,
            command=self.toggle_capture,
            style="Start.TButton",
            width=8,
        )
        self.control_button.pack(side="right")

        transcript_frame = tk.Frame(
            container,
            bg=PALETTE["panel"],
            highlightbackground=PALETTE["border"],
            highlightthickness=1,
        )
        transcript_frame.pack(fill="both", expand=True)

        self.transcript_strip = tk.Frame(transcript_frame, bg=blend_hex(PALETTE["panel"], PALETTE["coral"], 0.45), height=7)
        self.transcript_strip.pack(fill="x")

        transcript_shell = tk.Frame(
            transcript_frame,
            bg=PALETTE["panel"],
            padx=24,
            pady=24,
        )
        transcript_shell.pack(fill="both", expand=True)
        transcript_shell.grid_rowconfigure(0, weight=1)
        transcript_shell.grid_columnconfigure(0, weight=1)

        self.transcript_text = tk.Text(
            transcript_shell,
            wrap="word",
            state="disabled",
            bg=PALETTE["editor"],
            fg=PALETTE["ink"],
            relief="flat",
            bd=0,
            padx=34,
            pady=30,
            insertbackground=PALETTE["ink"],
            selectbackground=blend_hex(PALETTE["accent_soft"], PALETTE["coral"], 0.2),
            selectforeground=PALETTE["ink"],
        )
        self.transcript_text.grid(row=0, column=0, sticky="nsew")
        self.transcript_text.tag_configure(
            "body",
            font=("Segoe UI Semibold", 30),
            foreground="#143544",
            justify="center",
            spacing1=10,
            spacing2=8,
            spacing3=28,
        )
        self.transcript_text.tag_configure(
            "fresh",
            foreground=blend_hex("#143544", PALETTE["coral"], 0.42),
        )
        self.empty_hint = tk.Label(
            transcript_shell,
            text="Press Start and speak English.\nThe Spanish transcript will appear here in large text.",
            bg=PALETTE["editor"],
            fg=PALETTE["muted_soft"],
            font=("Segoe UI", 22),
            justify="center",
        )
        self.empty_hint.place(relx=0.5, rely=0.5, anchor="center")

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Start.TButton",
            font=("Bahnschrift SemiBold", 10),
            padding=(12, 6),
            background=PALETTE["accent"],
            foreground=PALETTE["panel"],
            borderwidth=0,
        )
        style.map(
            "Start.TButton",
            background=[
                ("disabled", blend_hex(PALETTE["border"], PALETTE["accent"], 0.28)),
                ("pressed", PALETTE["accent_deep"]),
                ("active", PALETTE["accent_deep"]),
            ],
            foreground=[("disabled", PALETTE["panel"])],
        )
        style.configure(
            "Stop.TButton",
            font=("Bahnschrift SemiBold", 10),
            padding=(12, 6),
            background=PALETTE["coral"],
            foreground=PALETTE["panel"],
            borderwidth=0,
        )
        style.map(
            "Stop.TButton",
            background=[
                ("disabled", blend_hex(PALETTE["border"], PALETTE["coral"], 0.25)),
                ("pressed", blend_hex(PALETTE["coral"], PALETTE["ink"], 0.12)),
                ("active", blend_hex(PALETTE["coral"], PALETTE["ink"], 0.12)),
            ],
            foreground=[("disabled", PALETTE["panel"])],
        )

    def _handle_space_toggle(self, _event: Any) -> None:
        self.toggle_capture()

    def _handle_escape_stop(self, _event: Any) -> None:
        if self.capture_requested:
            self.stop()

    def _handle_clear(self, _event: Any) -> None:
        self.clear_history()

    def _status_state_for(self, headline: str) -> str:
        normalized = headline.lower()
        if normalized.startswith("no microphone"):
            return "error"
        if normalized.startswith("listening"):
            return "listening"
        if normalized.startswith("transcribing"):
            return "transcribing"
        if normalized.startswith("translating"):
            return "translating"
        if normalized.startswith("loading") or normalized.startswith("checking") or normalized.startswith("starting"):
            return "loading"
        if normalized.startswith("stopped"):
            return "stopped"
        if normalized.startswith("error"):
            return "error"
        return "ready"

    def _state_text(self, state: str) -> str:
        labels = {
            "ready": "Listo",
            "loading": "Preparando",
            "listening": "Escuchando",
            "transcribing": "Captando",
            "translating": "Traduciendo",
            "stopped": "En pausa",
            "error": "Error",
        }
        return labels.get(state, "Listo")

    def _set_status(self, headline: str) -> None:
        self.visual_state = self._status_state_for(headline)
        self.status_var.set(self._state_text(self.visual_state))
        self._sync_button()

    def refresh_devices(self) -> None:
        self.devices = list_input_devices()
        if not self.devices:
            self.capture_requested = False
            self.visual_state = "error"
            self.status_var.set("Sin microfono")
            self._sync_button()
            return
        if self.visual_state in {"ready", "stopped", "error"} and not self.capture_requested:
            self.visual_state = "ready"
            self.status_var.set("Listo")

    def _pick_input_device(self) -> int | None:
        if not self.devices:
            self.refresh_devices()
        preferred = default_input_device()
        if preferred is not None:
            for device in self.devices:
                if device.index == preferred:
                    return device.index
        return self.devices[0].index if self.devices else None

    def _sync_button(self) -> None:
        if self.capture_requested:
            self.control_var.set("Stop")
            self.control_button.configure(style="Stop.TButton")
        else:
            self.control_var.set("Start")
            self.control_button.configure(style="Start.TButton")

    def toggle_capture(self) -> None:
        if self.capture_requested:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        if self.capture_requested:
            return
        device_index = self._pick_input_device()
        if device_index is None:
            self.visual_state = "error"
            self.status_var.set("Sin microfono")
            messagebox.showerror(APP_NAME, "No microphone was found. Connect one and try again.")
            return
        self.capture_requested = True
        self.visual_state = "loading"
        self.status_var.set("Preparando")
        self._sync_button()
        self.engine.start(device_index, DEFAULT_MODEL)

    def stop(self) -> None:
        if not self.capture_requested and not self.engine.running:
            return
        self.capture_requested = False
        self.engine.stop()
        self.level_target = 0.0
        self.level_value = 0.0
        self.visual_state = "stopped"
        self.status_var.set("En pausa")
        self._sync_button()

    def clear_history(self) -> None:
        self.transcript_text.configure(state="normal")
        self.transcript_text.delete("1.0", "end")
        self.transcript_text.configure(state="disabled")
        self.has_transcript = False
        self.empty_hint.place(relx=0.5, rely=0.5, anchor="center")

    def poll_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break

            kind = event["kind"]
            if kind == "status":
                self._set_status(event["headline"])
            elif kind == "error":
                self.capture_requested = False
                self.visual_state = "error"
                self.status_var.set("Error")
                self._sync_button()
                messagebox.showerror(APP_NAME, event["message"])
            elif kind == "level":
                self.level_target = float(event.get("value", 0.0))
            elif kind == "result":
                self._append_result(event["result"])

        self.root.after(120, self.poll_events)

    def _append_result(self, result: SegmentResult) -> None:
        if not self.has_transcript:
            self.clear_history()
            self.empty_hint.place_forget()
            self.has_transcript = True
        append_history(
            self.transcript_text,
            [
                (f"{result.spanish}\n\n", ("body", "fresh")),
            ],
        )
        self.transcript_flash = 1.0

    def animate_ui(self) -> None:
        now = time.perf_counter()
        self.level_target *= 0.93
        self.level_value += (self.level_target - self.level_value) * 0.24
        self.level_value = clamp(self.level_value)
        self.transcript_flash *= 0.9

        self._animate_status_chip(now)
        self._animate_motion(now)
        self._animate_transcript_accent(now)
        self.root.after(40, self.animate_ui)

    def _animate_status_chip(self, now: float) -> None:
        accent = STATE_COLORS.get(self.visual_state, PALETTE["accent"])
        pulse = (math.sin(now * 4.2) + 1.0) / 2.0
        fill_ratio = 0.16
        if self.visual_state == "listening":
            fill_ratio = 0.2 + self.level_value * 0.35
        elif self.visual_state in {"loading", "transcribing", "translating"}:
            fill_ratio = 0.24 + pulse * 0.28
        elif self.visual_state == "error":
            fill_ratio = 0.18

        self.status_chip.configure(
            bg=blend_hex(PALETTE["panel"], accent, fill_ratio),
            fg=blend_hex(PALETTE["ink"], accent, 0.55),
        )

        label = self._state_text(self.visual_state)
        if self.visual_state in {"loading", "transcribing", "translating"}:
            dots = "." * ((int(now * 3.2) % 3) + 1)
            label = f"{label}{dots}"
        elif self.visual_state == "listening" and self.capture_requested:
            dots = "." * ((int(now * 2.2) % 2) + 1)
            label = f"{label}{dots}"
        self.status_var.set(label)

    def _animate_motion(self, now: float) -> None:
        accent = STATE_COLORS.get(self.visual_state, PALETTE["accent"])
        width = max(self.motion_canvas.winfo_width(), 260)
        center_y = 17
        self.motion_canvas.coords(self.motion_track_id, 0, 12, width, 22)

        if self.visual_state == "listening":
            fill_width = max(18, int(width * max(0.08, self.level_value)))
            self.motion_canvas.coords(self.motion_fill_id, 0, 12, fill_width, 22)
            tip_width = min(28, fill_width)
            self.motion_canvas.coords(self.motion_tip_id, max(0, fill_width - tip_width), 12, fill_width, 22)
            base_radius = 4 + self.level_value * 4.5
            amplitude = 3 + self.level_value * 7.0
            spacing = width / (len(self.motion_dot_ids) + 1)
            for index, dot_id in enumerate(self.motion_dot_ids):
                phase = now * 6.2 + (index * 0.65)
                x = spacing * (index + 1)
                y = center_y + math.sin(phase) * amplitude
                radius = base_radius + math.sin(phase * 0.9) * 1.2
                color_mix = 0.28 + clamp(self.level_value * 0.55) + index * 0.04
                self.motion_canvas.coords(dot_id, x - radius, y - radius, x + radius, y + radius)
                self.motion_canvas.itemconfigure(
                    dot_id,
                    fill=blend_hex(PALETTE["panel"], accent, clamp(color_mix, 0.2, 0.9)),
                )
        elif self.visual_state in {"loading", "transcribing", "translating"}:
            sweep = (now * 240) % (width + 180)
            start = max(0, sweep - 140)
            end = min(width, sweep)
            self.motion_canvas.coords(self.motion_fill_id, start, 12, end, 22)
            self.motion_canvas.coords(self.motion_tip_id, max(0, end - 24), 12, end, 22)
            trail_spacing = 34
            for index, dot_id in enumerate(self.motion_dot_ids):
                x = (sweep - index * trail_spacing) % (width + 100) - 40
                radius = 7 - (index * 0.65)
                y = center_y + math.sin(now * 7.5 + index * 0.4) * 3.0
                self.motion_canvas.coords(dot_id, x - radius, y - radius, x + radius, y + radius)
                self.motion_canvas.itemconfigure(
                    dot_id,
                    fill=blend_hex(PALETTE["panel"], accent, clamp(0.88 - index * 0.1, 0.25, 0.9)),
                )
        else:
            self.motion_canvas.coords(self.motion_fill_id, 0, 12, 0, 22)
            self.motion_canvas.coords(self.motion_tip_id, 0, 12, 0, 22)
            center_x = width / 2
            for index, dot_id in enumerate(self.motion_dot_ids):
                radius = 2.7
                x = center_x + ((index - 2.5) * 18)
                self.motion_canvas.coords(dot_id, x - radius, center_y - radius, x + radius, center_y + radius)
                self.motion_canvas.itemconfigure(
                    dot_id,
                    fill=blend_hex(PALETTE["panel"], PALETTE["muted_soft"], 0.28),
                )

        self.motion_canvas.itemconfigure(self.motion_fill_id, fill=accent)
        self.motion_canvas.itemconfigure(
            self.motion_tip_id,
            fill=blend_hex(PALETTE["panel"], accent, 0.55),
        )

    def _animate_transcript_accent(self, now: float) -> None:
        accent = STATE_COLORS.get(self.visual_state, PALETTE["coral"])
        pulse = (math.sin(now * 3.8) + 1.0) / 2.0
        base_mix = 0.28
        if self.visual_state == "listening":
            base_mix = 0.32 + self.level_value * 0.2
        elif self.visual_state in {"loading", "transcribing", "translating"}:
            base_mix = 0.38 + pulse * 0.25
        mix = clamp(base_mix + self.transcript_flash * 0.35, 0.18, 0.85)
        self.transcript_strip.configure(bg=blend_hex(PALETTE["shadow"], accent, mix))
        body_mix = 0.0
        if self.visual_state in {"loading", "transcribing", "translating"}:
            body_mix = 0.07 + pulse * 0.08
        body_mix += self.transcript_flash * 0.18
        self.transcript_text.tag_configure(
            "body",
            foreground=blend_hex("#143544", PALETTE["coral"], clamp(body_mix, 0.0, 0.28)),
        )

    def on_close(self) -> None:
        try:
            self.engine.stop()
        finally:
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def run_self_test() -> int:
    print(f"{APP_NAME} self-test")
    print(f"Python: {sys.version.split()[0]}")
    devices = list_input_devices()
    print(f"Input devices found: {len(devices)}")

    default_device = default_input_device()
    if default_device is not None:
        sd.check_input_settings(device=default_device, channels=1, samplerate=SAMPLE_RATE, dtype="int16")
        print(f"Default input device: {default_device}")
    else:
        print("Default input device: none selected by Windows")

    translated = GoogleTranslator(source="en", target="es").translate("Live translation is ready.")
    print(f"Translation sample: {translated}")

    source = bundled_model_path(DEFAULT_MODEL)
    model_source = str(source) if source else DEFAULT_MODEL
    model = WhisperModel(
        model_source,
        device="cpu",
        compute_type="int8",
        cpu_threads=max(4, min(8, os.cpu_count() or 4)),
        download_root=str(MODEL_CACHE_DIR),
    )
    print(f"Whisper model loaded: {model_source}")
    silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
    segments, _ = model.transcribe(silence, language="en", task="transcribe", beam_size=1, without_timestamps=True)
    print(f"Sanity transcription segments: {len(list(segments))}")
    print("Self-test complete")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live English to Spanish translator.")
    parser.add_argument("--self-test", action="store_true", help="Run a non-GUI dependency check and exit.")
    parser.add_argument("--download-model", metavar="MODEL", help="Download a faster-whisper model into a folder.")
    parser.add_argument("--output-dir", metavar="PATH", help="Folder used with --download-model.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.download_model:
        if not args.output_dir:
            raise SystemExit("--output-dir is required with --download-model")
        output_dir = Path(args.output_dir).resolve()
        ensure_project_model(args.download_model, output_dir)
        print(f"Downloaded {args.download_model} to {output_dir}")
        return 0

    if args.self_test:
        return run_self_test()

    app = LiveTranslatorApp()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
