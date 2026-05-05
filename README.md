# Live Translator

Windows desktop app that listens to English on a microphone, transcribes it locally with Whisper, and translates each phrase into Spanish with low delay.

This repo now also includes a free browser version in [docs/index.html](./docs/index.html) that can be hosted on GitHub Pages.
It can also be deployed on Render as a standard web service.

## What it does

- Uses live microphone input.
- Chunks speech quickly after short pauses for fast reactions.
- Runs English speech recognition locally on your PC.
- Translates each finished phrase to Spanish.
- Builds into `dist\LiveTranslator\LiveTranslator.exe`.

## Notes

- Translation uses the internet.
- The build script bundles the `base.en` Whisper model so the packaged app starts faster.
- If you want even faster response, choose `Fast (tiny.en)` in the app.
- If you want more accuracy and can accept more delay, choose `Accurate (small.en)`.

## Build

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\build_exe.ps1
```

## Run from source

```powershell
.\.venv\Scripts\python app.py
```

## Quick verification

```powershell
.\.venv\Scripts\python app.py --self-test
dist\LiveTranslator\LiveTranslator.exe --self-test
```

## Free Web Version

The `docs` folder contains a browser-based classroom caption screen:

- Large Spanish-only captions
- Small Start button
- Smooth live animation while listening and translating
- Browser microphone noise suppression and transcript filtering
- No paid backend required

When hosted on Render through the included Node server, the web app uses server-side translation for faster responses. When hosted as plain static files, it falls back to in-browser translation.

### Local preview

```powershell
cd docs
py -m http.server 8000
```

Then open `http://localhost:8000`.

### Free deploy

Use GitHub Pages from the `main` branch and `/docs` folder.

- GitHub repo setting: `Settings -> Pages`
- Source: `Deploy from a branch`
- Branch: `main`
- Folder: `/docs`

There is no build command and no start command for GitHub Pages because it serves the static HTML, CSS, and JavaScript files directly.

### Browser notes

- Chrome or Edge is recommended for microphone speech recognition.
- The web version is fully client-side hosted, but it still depends on browser support for live speech recognition.
- The first translation can take longer while the browser caches the in-browser translation model.

## Render Web Service

This repo now includes a tiny Node server so Render can deploy it as a web service instead of trying to execute the old desktop app.

### Recommended Render settings

- Service type: `Web Service`
- Runtime: `Node`
- Build command: `npm install`
- Start command: `npm start`

The included [render.yaml](./render.yaml) and [package.json](./package.json) match those settings.
