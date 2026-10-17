"""归因实验：do_sample=False 下，同一条路径跑两次是否逐位相同。

这一步决定前面「加速版 vs 上游」的差异该归因于什么：
  · 两条路径各自都确定 → 差异是真实的路径差异（kernel/cache 不同导致的采样分叉）
  · 任一条路径两次就不同 → do_sample=False 没真正生效，之前的对比无意义
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "vendor" / "faster-qwen3-tts-main"))

import numpy as np
import torch

import config as cfg
from faster_qwen3_tts import FasterQwen3TTS

REF_AUDIO = ROOT / "vendor" / "faster-qwen3-tts-main" / "ref_audio.wav"
REF_TEXT = (
    "I'm confused why some people have super short timelines, yet at the same time "
    "are bullish on scaling up reinforcement learning atop LLMs."
)
TEXT = "今天的天气不错，我们一起出去走走吧。"


def diff(a: np.ndarray, b: np.ndarray) -> str:
    if len(a) != len(b):
        return f"长度不同 {len(a)} vs {len(b)}"
    d = np.abs(a.astype(np.float64) - b.astype(np.float64))
    nz = int(np.count_nonzero(d))
    return f"同长度 {len(a)}  不同采样点 {nz}  最大差 {d.max():.2e}"


def main() -> None:
    sub = cfg.MODEL_DIRS[("clone", "1.7B")]
    model = FasterQwen3TTS.from_pretrained(
        str((cfg.MODELS_DIR / sub)).replace("\\", "/"),
        device="cuda", dtype=torch.bfloat16,
    )
    kw = dict(text=TEXT, language="Chinese", ref_audio=REF_AUDIO.as_posix(),
              ref_text=REF_TEXT, xvec_only=True, do_sample=False)

    model.generate_voice_clone(text="你好。", language="Chinese",
                               ref_audio=REF_AUDIO.as_posix(), ref_text=REF_TEXT,
                               xvec_only=True, do_sample=False, max_new_tokens=64)

    print("=== 加速路径（static cache + CUDA graph）===")
    a1 = np.asarray(model.generate_voice_clone(**kw)[0][0], dtype=np.float32)
    a2 = np.asarray(model.generate_voice_clone(**kw)[0][0], dtype=np.float32)
    print("  run1 vs run2 :", diff(a1, a2))

    print("=== 上游路径（dynamic cache）===")
    ukw = dict(text=TEXT, language="Chinese", ref_audio=REF_AUDIO.as_posix(),
               ref_text=REF_TEXT, xvec_only_mode=True, do_sample=False,
               non_streaming_mode=True)
    b1 = np.asarray(model.model.generate_voice_clone(**ukw)[0][0], dtype=np.float32)
    b2 = np.asarray(model.model.generate_voice_clone(**ukw)[0][0], dtype=np.float32)
    print("  run1 vs run2 :", diff(b1, b2))

    print("=== 跨路径 ===")
    print("  static vs dynamic :", diff(a1, b1))
    for name, w in (("static", a1), ("dynamic", b1)):
        print(f"  {name:<8} {len(w)/24000:5.2f}s  RMS {np.sqrt(np.mean(w**2)):.4f}")


if __name__ == "__main__":
    main()
