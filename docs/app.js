const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

const MAX_CAPTIONS = 4;
const PREVIEW_DELAY_MS = 280;
const VOICE_WINDOW_MS = 1600;
const RESTART_DELAY_MS = 260;

const captionsEl = document.getElementById("captions");
const startButton = document.getElementById("start-button");
const statusText = document.getElementById("status-text");
const controlNote = document.getElementById("control-note");
const captionStage = document.getElementById("caption-stage");

const state = {
  running: false,
  stage: "idle",
  captions: [],
  previewText: "",
  recognition: null,
  manuallyStopping: false,
  translationBackend: "unknown",
  backendCheckPromise: null,
  serverHealthy: false,
  translatorWorker: null,
  translatorReady: false,
  translatorDevice: "",
  previewTimer: null,
  nextTranslationId: 0,
  latestPreviewId: 0,
  pendingFinals: 0,
  previewAbortController: null,
  lastPreviewSource: "",
  lastPreviewTranslation: "",
  lastAcceptedText: "",
  lastAcceptedAt: 0,
  micStream: null,
  audioContext: null,
  analyser: null,
  audioData: null,
  meterFrame: 0,
  noiseFloor: 0.01,
  speechFrames: 0,
  silenceFrames: 0,
  lastVoiceAt: 0,
};

if (!SpeechRecognition) {
  setStage(
    "idle",
    "Use Chrome or Edge. This free version relies on browser speech recognition."
  );
  controlNote.textContent =
    "Speech recognition is not available in this browser. Open the page in Chrome or Edge.";
}

startButton.addEventListener("click", async () => {
  if (state.running) {
    stopApp();
    return;
  }

  await startApp();
});

captionStage.addEventListener("dblclick", async () => {
  if (!document.fullscreenElement) {
    await document.documentElement.requestFullscreen?.();
    return;
  }

  await document.exitFullscreen?.();
});

function setStage(stage, detail = "") {
  state.stage = stage;
  document.body.dataset.state = stage;

  const labels = {
    idle: "Ready",
    loading: detail || "Loading",
    ready: "Ready",
    listening: "Listening",
    speaking: "Listening",
    translating: "Translating",
    error: "Error",
  };

  statusText.textContent = labels[stage] || detail || "Ready";

  if (detail) {
    controlNote.textContent = detail;
  } else if (state.running) {
    controlNote.textContent =
      "Double-click the screen for fullscreen. Chrome or Edge recommended.";
  } else {
    controlNote.textContent = "Free browser version. Chrome or Edge recommended.";
  }
}

function normalizeText(text) {
  return text
    .toLowerCase()
    .replace(/[^a-z\s']/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function looksLikeNoise(text) {
  const normalized = normalizeText(text);

  if (!normalized) {
    return true;
  }

  const fillers = new Set(["uh", "um", "hmm", "mm", "ah", "eh", "huh", "mhm"]);
  if (fillers.has(normalized)) {
    return true;
  }

  const words = normalized.split(" ");
  const lettersOnly = normalized.replace(/\s/g, "");
  const uniqueLetters = new Set(lettersOnly).size;
  const vowels = (lettersOnly.match(/[aeiouy]/g) || []).length;

  if (words.length === 1 && lettersOnly.length < 3) {
    return true;
  }

  if (lettersOnly.length >= 5 && uniqueLetters <= 2) {
    return true;
  }

  if (lettersOnly.length >= 4 && vowels === 0) {
    return true;
  }

  return false;
}

function recentlyHeardVoice() {
  return Date.now() - state.lastVoiceAt < VOICE_WINDOW_MS;
}

function shouldAcceptTranscript(text, confidence, isFinal) {
  const normalized = normalizeText(text);

  if (!normalized || looksLikeNoise(normalized)) {
    return false;
  }

  const wordCount = normalized.split(" ").length;
  const charCount = normalized.length;
  const lowConfidence = typeof confidence === "number" && confidence > 0 && confidence < 0.42;

  if (!recentlyHeardVoice() && charCount < 10 && wordCount < 2) {
    return false;
  }

  if (!isFinal && charCount < 5) {
    return false;
  }

  if (isFinal && lowConfidence && wordCount < 4) {
    return false;
  }

  if (
    normalized === state.lastAcceptedText &&
    Date.now() - state.lastAcceptedAt < 1800
  ) {
    return false;
  }

  return true;
}

function renderCaptions() {
  captionsEl.textContent = "";

  const lines = [];

  if (!state.captions.length && !state.previewText) {
    const placeholder = document.createElement("p");
    placeholder.className = "caption-line caption-line-placeholder";
    placeholder.textContent = state.running
      ? "Speak in English. Spanish captions will appear here."
      : "Press Start and allow microphone access. Spanish captions will appear here.";
    lines.push(placeholder);
  }

  state.captions.forEach((caption, index) => {
    const line = document.createElement("p");
    const isCurrent = index === state.captions.length - 1;
    line.className = `caption-line ${
      isCurrent ? "caption-line-current" : "caption-line-stale"
    }`;
    line.textContent = caption;
    lines.push(line);
  });

  if (state.previewText) {
    const previewLine = document.createElement("p");
    previewLine.className = "caption-line caption-line-preview";
    previewLine.textContent = state.previewText;
    lines.push(previewLine);
  }

  lines.forEach((line) => captionsEl.appendChild(line));
}

function flashCaption() {
  document.body.classList.remove("caption-flash");
  window.requestAnimationFrame(() => {
    document.body.classList.add("caption-flash");
    window.setTimeout(() => {
      document.body.classList.remove("caption-flash");
    }, 540);
  });
}

function addCaption(text) {
  state.captions.push(text);
  state.captions = state.captions.slice(-MAX_CAPTIONS);
  state.previewText = "";
  renderCaptions();
  flashCaption();
}

function setPreview(text) {
  state.previewText = text;
  renderCaptions();
}

function clearPreviewTimer() {
  if (state.previewTimer) {
    window.clearTimeout(state.previewTimer);
    state.previewTimer = null;
  }
}

function ensureTranslatorWorker() {
  if (state.translatorWorker) {
    return;
  }

  state.translatorWorker = new Worker("./translator-worker.js", { type: "module" });

  state.translatorWorker.addEventListener("message", (event) => {
    const message = event.data;

    if (message.type === "status") {
      if (!state.translatorReady) {
        setStage("loading", message.detail || "Loading translator...");
      }
      return;
    }

    if (message.type === "ready") {
      state.translatorReady = true;
      state.translatorDevice = message.device || "";
      setStage(state.running ? "listening" : "ready");
      return;
    }

    if (message.type === "translation") {
      if (message.kind === "preview") {
        if (message.id === state.latestPreviewId && message.text) {
          state.lastPreviewSource = normalizeText(message.source || "");
          state.lastPreviewTranslation = message.text;
          setPreview(message.text);
        }
        return;
      }

      state.pendingFinals = Math.max(0, state.pendingFinals - 1);

      if (message.text) {
        addCaption(message.text);
      }

      if (state.running) {
        setStage(recentlyHeardVoice() ? "speaking" : "listening");
      } else {
        setStage("ready");
      }
      return;
    }

    if (message.type === "error") {
      setStage("error", message.detail || "Translation failed.");
    }
  });
}

async function checkServerTranslation() {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), 2500);

  try {
    const response = await fetch("/api/health", {
      method: "GET",
      headers: {
        Accept: "application/json",
      },
      signal: controller.signal,
    });

    if (!response.ok) {
      return false;
    }

    const payload = await response.json();
    return Boolean(payload?.ok);
  } catch (error) {
    return false;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

async function ensureTranslationBackend() {
  if (state.translationBackend !== "unknown") {
    return state.translationBackend;
  }

  if (!state.backendCheckPromise) {
    state.backendCheckPromise = (async () => {
      const serverHealthy = await checkServerTranslation();

      state.serverHealthy = serverHealthy;
      state.translationBackend = serverHealthy ? "server" : "browser";

      if (state.translationBackend === "browser") {
        ensureTranslatorWorker();
        state.translatorWorker.postMessage({ type: "warmup" });
      } else {
        state.translatorReady = true;
      }

      return state.translationBackend;
    })();
  }

  return state.backendCheckPromise;
}

async function translateViaServer(text, signal) {
  const response = await fetch("/api/translate", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify({ text }),
    signal,
  });

  const payload = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw new Error(payload?.error || "Translation failed.");
  }

  return typeof payload?.translation === "string" ? payload.translation.trim() : "";
}

async function queueTranslation(text, kind) {
  const backend = await ensureTranslationBackend();
  const id = ++state.nextTranslationId;

  if (kind === "preview") {
    state.latestPreviewId = id;
    if (state.previewAbortController) {
      state.previewAbortController.abort();
    }
    state.previewAbortController = new AbortController();
  } else {
    state.pendingFinals += 1;
    setStage("translating", "Translating to Spanish...");
  }

  if (
    kind === "final" &&
    normalizeText(text) === state.lastPreviewSource &&
    state.lastPreviewTranslation
  ) {
    state.pendingFinals = Math.max(0, state.pendingFinals - 1);
    addCaption(state.lastPreviewTranslation);
    setStage(recentlyHeardVoice() ? "speaking" : "listening");
    return;
  }

  if (backend === "server") {
    try {
      const translation = await translateViaServer(
        text,
        kind === "preview" ? state.previewAbortController?.signal : undefined
      );

      if (kind === "preview") {
        if (id === state.latestPreviewId && translation) {
          state.lastPreviewSource = normalizeText(text);
          state.lastPreviewTranslation = translation;
          setPreview(translation);
        }
        return;
      }

      state.pendingFinals = Math.max(0, state.pendingFinals - 1);
      if (translation) {
        addCaption(translation);
      }
      setStage(recentlyHeardVoice() ? "speaking" : "listening");
    } catch (error) {
      if (kind === "preview" && error instanceof Error && error.name === "AbortError") {
        return;
      }

      if (kind === "final") {
        state.pendingFinals = Math.max(0, state.pendingFinals - 1);
      }

      setStage(
        "error",
        error instanceof Error ? error.message : "Translation failed."
      );
    }
    return;
  }

  ensureTranslatorWorker();
  state.translatorWorker.postMessage({
    type: "translate",
    id,
    kind,
    text,
  });
}

function schedulePreviewTranslation(text) {
  clearPreviewTimer();

  if (!state.running || state.pendingFinals > 0) {
    return;
  }

  state.previewTimer = window.setTimeout(() => {
    if (!state.running || !shouldAcceptTranscript(text, 1, false)) {
      return;
    }

    void queueTranslation(text, "preview");
  }, PREVIEW_DELAY_MS);
}

function handleSpeechResult(event) {
  let interimText = "";

  for (let index = event.resultIndex; index < event.results.length; index += 1) {
    const result = event.results[index];
    const alternative = result[0];
    const transcript = alternative?.transcript?.trim();
    const confidence = alternative?.confidence ?? 1;

    if (!transcript) {
      continue;
    }

    if (result.isFinal) {
      if (shouldAcceptTranscript(transcript, confidence, true)) {
        state.lastAcceptedText = normalizeText(transcript);
        state.lastAcceptedAt = Date.now();
        clearPreviewTimer();
        setPreview("");
        void queueTranslation(transcript, "final");
      }
      continue;
    }

    if (shouldAcceptTranscript(transcript, confidence, false)) {
      interimText = transcript;
    }
  }

  if (interimText) {
    schedulePreviewTranslation(interimText);
  } else if (state.pendingFinals === 0) {
    setPreview("");
  }
}

function buildRecognition() {
  if (state.recognition || !SpeechRecognition) {
    return;
  }

  const recognition = new SpeechRecognition();
  recognition.lang = "en-US";
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.maxAlternatives = 1;

  recognition.addEventListener("start", () => {
    if (state.running && state.translatorReady) {
      setStage("listening");
    }
  });

  recognition.addEventListener("result", handleSpeechResult);

  recognition.addEventListener("error", (event) => {
    if (event.error === "no-speech") {
      return;
    }

    if (event.error === "aborted" && state.manuallyStopping) {
      return;
    }

    if (event.error === "not-allowed") {
      setStage("error", "Microphone access was blocked.");
      stopApp();
      return;
    }

    if (event.error === "audio-capture") {
      setStage("error", "No microphone was found.");
      stopApp();
      return;
    }

    setStage("error", `Speech recognition error: ${event.error}`);
  });

  recognition.addEventListener("end", () => {
    if (!state.running || state.manuallyStopping) {
      state.manuallyStopping = false;
      return;
    }

    window.setTimeout(() => {
      if (state.running) {
        recognition.start();
      }
    }, RESTART_DELAY_MS);
  });

  state.recognition = recognition;
}

function computeRms(samples) {
  let sum = 0;
  let peak = 0;

  for (let index = 0; index < samples.length; index += 1) {
    const value = samples[index];
    const abs = Math.abs(value);
    sum += value * value;
    if (abs > peak) {
      peak = abs;
    }
  }

  return {
    rms: Math.sqrt(sum / samples.length),
    peak,
  };
}

function updateMeter() {
  if (!state.analyser || !state.audioData) {
    return;
  }

  state.analyser.getFloatTimeDomainData(state.audioData);

  const { rms, peak } = computeRms(state.audioData);
  const adaptiveFloor = Math.max(0.008, state.noiseFloor);
  const speechThreshold = Math.max(adaptiveFloor * 2.25, 0.02);
  const peakThreshold = Math.max(0.065, adaptiveFloor * 4.6);
  const hasVoice = rms > speechThreshold || peak > peakThreshold;

  if (hasVoice) {
    state.speechFrames += 1;
    state.silenceFrames = 0;
    state.lastVoiceAt = Date.now();
  } else {
    state.silenceFrames += 1;
    state.speechFrames = Math.max(0, state.speechFrames - 1);
  }

  if (hasVoice) {
    state.noiseFloor = state.noiseFloor * 0.985 + Math.min(rms, speechThreshold) * 0.015;
  } else {
    state.noiseFloor = state.noiseFloor * 0.965 + rms * 0.035;
  }

  const activity = Math.max(
    0.05,
    Math.min(1, (rms - adaptiveFloor) / Math.max(0.008, speechThreshold - adaptiveFloor))
  );
  document.documentElement.style.setProperty("--activity", activity.toFixed(3));

  if (state.running && state.translatorReady && state.pendingFinals === 0) {
    setStage(recentlyHeardVoice() ? "speaking" : "listening");
  }

  state.meterFrame = window.requestAnimationFrame(updateMeter);
}

async function startAudioMeter() {
  if (state.micStream) {
    return;
  }

  state.micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    },
    video: false,
  });

  state.audioContext = new window.AudioContext();
  const source = state.audioContext.createMediaStreamSource(state.micStream);
  state.analyser = state.audioContext.createAnalyser();
  state.analyser.fftSize = 1024;
  state.analyser.smoothingTimeConstant = 0.86;
  state.audioData = new Float32Array(state.analyser.fftSize);
  source.connect(state.analyser);

  updateMeter();
}

async function startApp() {
  if (!SpeechRecognition) {
    return;
  }

  try {
    startButton.disabled = true;
    setStage("loading", "Connecting microphone...");
    await startAudioMeter();
    setStage("loading", "Connecting translator...");
    await ensureTranslationBackend();
    buildRecognition();

    state.running = true;
    state.manuallyStopping = false;
    startButton.textContent = "Stop";
    renderCaptions();
    state.recognition.start();

    if (state.translationBackend === "server") {
      controlNote.textContent = "Server translation is active for faster captions.";
    }
  } catch (error) {
    setStage(
      "error",
      error instanceof Error ? error.message : "Could not start the microphone."
    );
    stopApp();
  } finally {
    startButton.disabled = false;
  }
}

function stopAudioMeter() {
  if (state.meterFrame) {
    window.cancelAnimationFrame(state.meterFrame);
    state.meterFrame = 0;
  }

  if (state.micStream) {
    state.micStream.getTracks().forEach((track) => track.stop());
    state.micStream = null;
  }

  if (state.audioContext) {
    state.audioContext.close();
    state.audioContext = null;
  }

  state.analyser = null;
  state.audioData = null;
}

function stopApp() {
  state.running = false;
  state.pendingFinals = 0;
  state.manuallyStopping = true;
  clearPreviewTimer();
  if (state.previewAbortController) {
    state.previewAbortController.abort();
    state.previewAbortController = null;
  }
  setPreview("");

  if (state.recognition) {
    try {
      state.recognition.stop();
    } catch (error) {
      // Ignore redundant stop calls from browsers that are already ending.
    }
  }

  stopAudioMeter();
  startButton.textContent = "Start";
  startButton.disabled = false;
  state.lastPreviewSource = "";
  state.lastPreviewTranslation = "";
  setStage(state.translatorReady ? "ready" : "idle");
  renderCaptions();
}

renderCaptions();
