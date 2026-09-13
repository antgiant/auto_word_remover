#!/usr/bin/env python
"""Quick health check for the Voice_to_Text install."""
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent

os.environ.setdefault("HF_HOME", str(HERE / "hf_cache"))
os.environ.setdefault("TORCH_HOME", str(HERE / "torch_cache"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import warnings
warnings.filterwarnings("ignore")

ok = True


def check(label, fn):
    global ok
    try:
        print(f"  {label:<34} {fn()}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  {label:<34} FAIL: {exc!r}")


print("Voice_to_Text doctor\n" + "-" * 50)

import torch
check("torch", lambda: torch.__version__)
check("CUDA available", lambda: torch.cuda.is_available())
check("GPU", lambda: torch.cuda.get_device_name(0))
check("GPU free / total VRAM", lambda: "{:.1f} / {:.1f} GiB".format(
    *(x / 1024**3 for x in torch.cuda.mem_get_info())))

import ctranslate2
check("ctranslate2", lambda: ctranslate2.__version__)
check("ctranslate2 sees CUDA", lambda: ctranslate2.get_cuda_device_count() > 0)

import faster_whisper, whisperx  # noqa: F401
check("faster_whisper", lambda: faster_whisper.__version__)
check("whisperx import", lambda: "ok")

import pyannote.audio
check("pyannote.audio", lambda: pyannote.audio.__version__)

models = HERE / "models"
for name in ["models--mobiuslabsgmbh--faster-whisper-large-v3-turbo",
             "models--Systran--faster-whisper-large-v2",
             "models--Systran--faster-whisper-large-v3"]:
    check(name.split("--")[-1] + " weights", lambda p=models / name: "present" if p.is_dir() else "MISSING")
check("wav2vec2 LARGE aligner",
      lambda: "present" if (models / "wav2vec2_fairseq_large_lv60k_asr_ls960.pth").is_file() else "MISSING")

hub = Path(os.environ["HF_HOME"]) / "hub"
for name in ["models--tensorlake--segmentation-3.0",
             "models--pyannote--wespeaker-voxceleb-resnet34-LM"]:
    check(name.split("--")[-1] + " (diarization)",
          lambda p=hub / name: "present" if p.is_dir() else "not cached yet (downloads on first --diarize)")

print("-" * 50)
print("ALL GOOD" if ok else "PROBLEMS FOUND - see FAIL lines above")
raise SystemExit(0 if ok else 1)
