"""长文本流式的 TTFA / RTF：决定「能不能给剧本工坊开流式」的那个数。

短文本的 RTF 被参考音频编码这类固定开销拉高，看不出真实吞吐；长文本才接近
「每秒生成多少秒音频」。所以这里用接近真实剧本长度的文本量一次。

    runtime\\python\\python.exe tools\\bench_long.py            # 默认 1.7B
    BENCH_SIZE=0.6B runtime\\python\\python.exe tools\\bench_long.py
"""

from __future__ import annotations

import os
import sys
import time
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

LONG = (
    "边生成边播测试。这句话比较长，目的是让生成结束的时候音频还在响，"
    "这样才看得出来结果卡的播放器是不是真的跟着声音在走。"
    "再来一句凑够十秒，顺便看看长文本的吞吐到底是什么水平。"
)


def main() -> None:
    size = os.environ.get("BENCH_SIZE", "1.7B")
    sub = cfg.MODEL_DIRS[("clone", size)]
    print(f"[{size}] 加载 …", flush=True)
    model = FasterQwen3TTS.from_pretrained(
        str(cfg.MODELS_DIR / sub).replace("\\", "/"),
        device="cuda", dtype=torch.bfloat16,
    )
    # 预热：CUDA graph 捕获发生在第一次调用，不进基线
    model.generate_voice_clone(text="你好。", language="Chinese",
                               ref_audio=REF_AUDIO.as_posix(), ref_text=REF_TEXT,
                               xvec_only=True, max_new_tokens=64)

    print(f"文本 {len(LONG)} 字，开始流式（chunk=8）…", flush=True)
    t0 = time.perf_counter()
    stamps: list[float] = []
    chunks: list[np.ndarray] = []
    sr = 24000
    for chunk, sr, _t in model.generate_voice_clone_streaming(
        text=LONG, language="Chinese", ref_audio=REF_AUDIO.as_posix(),
        ref_text=REF_TEXT, xvec_only=True, chunk_size=8,
    ):
        stamps.append(time.perf_counter() - t0)
        chunks.append(np.asarray(chunk, dtype=np.float32))
    total = time.perf_counter() - t0
    audio = sum(len(c) for c in chunks) / sr

    print(f"\n块数 {len(chunks)}  总耗时 {total:.2f}s  音频 {audio:.2f}s")
    print(f"TTFA {stamps[0]*1000:.0f} ms")
    print(f"RTF  {total/audio:.3f}")
    gaps = [(b - a) * 1000 for a, b in zip(stamps, stamps[1:])]
    ms = audio / max(len(chunks), 1) * 1000
    print(f"块时长 {ms:.0f} ms  块间隔 最小 {min(gaps):.0f} / 中位 "
          f"{sorted(gaps)[len(gaps)//2]:.0f} / 最大 {max(gaps):.0f} ms")
    slack = [(g - ms) for g in gaps]
    print(f"隐含缓冲增量 中位 {sorted(slack)[len(slack)//2]:+.0f} ms/块 "
          f"（负数=在吃老本，连续为负就会断）")


if __name__ == "__main__":
    main()
