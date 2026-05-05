import { env, pipeline } from "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.8.1";

env.allowLocalModels = false;
env.useBrowserCache = true;

const MODEL_ID = "Xenova/opus-mt-en-es";

let translatorPromise = null;
let activeDevice = "wasm";

function postStatus(stage, detail = "") {
  self.postMessage({ type: "status", stage, detail });
}

async function getTranslator() {
  if (!translatorPromise) {
    activeDevice = "gpu" in self.navigator ? "webgpu" : "wasm";
    postStatus(
      "loading",
      activeDevice === "webgpu"
        ? "Loading translator on GPU..."
        : "Loading translator..."
    );

    translatorPromise = pipeline("translation", MODEL_ID, {
      device: activeDevice,
      progress_callback: (progress) => {
        if (progress && progress.status === "progress" && progress.progress != null) {
          const percent = Math.max(0, Math.min(100, Math.round(progress.progress)));
          postStatus("loading", `Loading translator... ${percent}%`);
        }
      },
    }).then((instance) => {
      self.postMessage({ type: "ready", device: activeDevice });
      return instance;
    });
  }

  return translatorPromise;
}

async function handleTranslation(message) {
  const translator = await getTranslator();
  const output = await translator(message.text, { max_new_tokens: 140 });
  const translation = Array.isArray(output)
    ? output[0]?.translation_text
    : output?.translation_text;

  self.postMessage({
    type: "translation",
    id: message.id,
    kind: message.kind,
    source: message.text,
    text: translation || "",
  });
}

self.onmessage = async (event) => {
  const message = event.data;

  try {
    if (message.type === "warmup") {
      await getTranslator();
      return;
    }

    if (message.type === "translate" && message.text) {
      await handleTranslation(message);
    }
  } catch (error) {
    self.postMessage({
      type: "error",
      detail: error instanceof Error ? error.message : String(error),
    });
  }
};
