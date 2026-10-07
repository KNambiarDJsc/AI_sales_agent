"""Prepare the local AI backup (no OpenAI key needed): Kokoro TTS, Moonshine STT and
the Ollama LLM. Safe to re-run; skips whatever is already in place.

    python -m pip install -r requirements-local.txt
    python scripts/setup_local_models.py            # downloads + checks everything
    python scripts/setup_local_models.py --check    # only reports what's missing

Ollama itself is a separate install (https://ollama.com/download) — this script pulls
the model into it but can't install it.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings  # noqa: E402

KOKORO_FILES = {
    "kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.0.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}


def step(ok: bool, what: str, detail: str = "") -> bool:
    print(f"[{'OK  ' if ok else 'MISS'}] {what}" + (f" — {detail}" if detail else ""))
    return ok


def kokoro(check_only: bool) -> bool:
    folder = Path(get_settings().local_models_dir) / "kokoro"
    folder.mkdir(parents=True, exist_ok=True)
    ok = True
    for name, url in KOKORO_FILES.items():
        path = folder / name
        if not path.exists() and not check_only:
            print(f"       downloading {name} ...")
            tmp = path.with_suffix(path.suffix + ".part")
            urllib.request.urlretrieve(url, tmp)
            tmp.rename(path)
        ok &= step(path.exists(), f"Kokoro {name}", str(path))
    if ok and not check_only:
        from speech.tts.kokoro_local import _load_engine

        t0 = time.monotonic()
        _load_engine()
        step(True, "Kokoro loads and speaks", f"{time.monotonic() - t0:.1f}s")
    return ok


def moonshine(check_only: bool) -> bool:
    try:
        import moonshine_onnx  # noqa: F401
    except ImportError:
        return step(False, "Moonshine STT package", "pip install useful-moonshine-onnx")
    if check_only:
        return step(True, "Moonshine STT package installed (model downloads on first use)")
    from speech.stt.moonshine_local import _load

    t0 = time.monotonic()
    _load()  # downloads the model on first run, then caches it
    return step(True, f"Moonshine moonshine/{get_settings().local_stt_model} loads", f"{time.monotonic() - t0:.1f}s")


def ollama(check_only: bool) -> bool:
    import httpx

    s = get_settings()
    exe = shutil.which("ollama")
    try:
        tags = httpx.get(f"{s.local_llm_base_url.rstrip('/')}/api/tags", timeout=5).json()
    except Exception:  # noqa: BLE001
        return step(False, "Ollama running", "install from https://ollama.com/download and start it" if not exe else "start Ollama")
    names = {m.get("name") for m in tags.get("models", [])}
    have = s.local_llm_model in names or f"{s.local_llm_model}:latest" in names
    if not have and not check_only and exe:
        print(f"       ollama pull {s.local_llm_model} ...")
        subprocess.run([exe, "pull", s.local_llm_model], check=False)
        names = {m.get("name") for m in httpx.get(f"{s.local_llm_base_url.rstrip('/')}/api/tags", timeout=5).json().get("models", [])}
        have = s.local_llm_model in names
    return step(have, f"Ollama model {s.local_llm_model}", "" if have else f"ollama pull {s.local_llm_model}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only report, download nothing")
    args = parser.parse_args()
    results = [moonshine(args.check), kokoro(args.check), ollama(args.check)]
    print("\nLocal backup:", "READY" if all(results) else "NOT READY")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
