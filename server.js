const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

const HOST = "0.0.0.0";
const PORT = Number(process.env.PORT || 10000);
const DOCS_DIR = path.join(__dirname, "docs");
const TRANSLATION_CACHE_TTL_MS = 1000 * 60 * 60;
const MAX_BODY_BYTES = 10_000;
const translationCache = new Map();
const translateModulePromise = import("@vitalets/google-translate-api");

const CONTENT_TYPES = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".ico": "image/x-icon",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml; charset=utf-8",
  ".txt": "text/plain; charset=utf-8",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

function sendFile(response, filePath) {
  const extension = path.extname(filePath).toLowerCase();
  const contentType = CONTENT_TYPES[extension] || "application/octet-stream";

  fs.createReadStream(filePath)
    .on("open", () => {
      response.writeHead(200, {
        "Cache-Control": extension === ".html" ? "no-cache" : "public, max-age=3600",
        "Content-Type": contentType,
      });
    })
    .on("error", () => {
      sendError(response, 500, "Internal Server Error");
    })
    .pipe(response);
}

function sendError(response, statusCode, message) {
  response.writeHead(statusCode, {
    "Content-Type": "text/plain; charset=utf-8",
  });
  response.end(message);
}

function sendJson(response, statusCode, payload) {
  response.writeHead(statusCode, {
    "Cache-Control": "no-store",
    "Content-Type": "application/json; charset=utf-8",
  });
  response.end(JSON.stringify(payload));
}

function normalizeCacheKey(text) {
  return text.trim().replace(/\s+/g, " ").toLowerCase();
}

function getCachedTranslation(text) {
  const key = normalizeCacheKey(text);
  const cached = translationCache.get(key);

  if (!cached) {
    return null;
  }

  if (Date.now() - cached.createdAt > TRANSLATION_CACHE_TTL_MS) {
    translationCache.delete(key);
    return null;
  }

  return cached.text;
}

function setCachedTranslation(text, translatedText) {
  translationCache.set(normalizeCacheKey(text), {
    createdAt: Date.now(),
    text: translatedText,
  });
}

function readJsonBody(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let totalLength = 0;

    request.on("data", (chunk) => {
      totalLength += chunk.length;

      if (totalLength > MAX_BODY_BYTES) {
        reject(new Error("Request body too large."));
        request.destroy();
        return;
      }

      chunks.push(chunk);
    });

    request.on("end", () => {
      try {
        const raw = Buffer.concat(chunks).toString("utf8");
        resolve(raw ? JSON.parse(raw) : {});
      } catch (error) {
        reject(new Error("Invalid JSON body."));
      }
    });

    request.on("error", reject);
  });
}

async function translateToSpanish(text) {
  const cached = getCachedTranslation(text);
  if (cached) {
    return cached;
  }

  const { translate } = await translateModulePromise;
  const result = await translate(text, {
    from: "en",
    to: "es",
  });
  const translatedText = result?.text?.trim() || "";

  if (translatedText) {
    setCachedTranslation(text, translatedText);
  }

  return translatedText;
}

function safeResolve(requestPath) {
  const cleanPath = requestPath === "/" ? "/index.html" : requestPath;
  const decodedPath = decodeURIComponent(cleanPath);
  const normalizedPath = path.normalize(decodedPath).replace(/^(\.\.[/\\])+/, "");
  const resolvedPath = path.join(DOCS_DIR, normalizedPath);

  if (!resolvedPath.startsWith(DOCS_DIR)) {
    return null;
  }

  return resolvedPath;
}

const server = http.createServer((request, response) => {
  if (!request.url) {
    sendError(response, 400, "Bad Request");
    return;
  }

  const url = new URL(request.url, `http://${request.headers.host || "localhost"}`);

  if (request.method === "GET" && url.pathname === "/api/health") {
    sendJson(response, 200, {
      mode: "server",
      ok: true,
    });
    return;
  }

  if (request.method === "POST" && url.pathname === "/api/translate") {
    readJsonBody(request)
      .then(async (body) => {
        const text = typeof body.text === "string" ? body.text.trim() : "";

        if (!text) {
          sendJson(response, 400, { error: "Text is required." });
          return;
        }

        if (text.length > 1800) {
          sendJson(response, 400, { error: "Text is too long." });
          return;
        }

        const translation = await translateToSpanish(text);

        sendJson(response, 200, {
          ok: true,
          translation,
        });
      })
      .catch((error) => {
        const message = error instanceof Error ? error.message : "Translation failed.";
        const statusCode =
          message === "Request body too large." || message === "Invalid JSON body."
            ? 400
            : 502;

        sendJson(response, statusCode, {
          error: message,
        });
      });
    return;
  }

  const candidatePath = safeResolve(url.pathname);

  if (!candidatePath) {
    sendError(response, 403, "Forbidden");
    return;
  }

  fs.stat(candidatePath, (error, stats) => {
    if (!error && stats.isFile()) {
      sendFile(response, candidatePath);
      return;
    }

    const fallbackPath = path.join(DOCS_DIR, "index.html");
    fs.stat(fallbackPath, (fallbackError, fallbackStats) => {
      if (!fallbackError && fallbackStats.isFile()) {
        sendFile(response, fallbackPath);
        return;
      }

      sendError(response, 404, "Not Found");
    });
  });
});

server.listen(PORT, HOST, () => {
  console.log(`Live Translator web server listening on http://${HOST}:${PORT}`);
});
