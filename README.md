# Live Translator

Windows desktop app that listens to English on a microphone, transcribes it locally with Whisper, and translates each phrase into Spanish with low delay.

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
