const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

const HOST = "0.0.0.0";
const PORT = Number(process.env.PORT || 10000);
const DOCS_DIR = path.join(__dirname, "docs");

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
