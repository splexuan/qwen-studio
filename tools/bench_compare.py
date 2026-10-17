"""对比两条路径的输出到底是「内容不同」还是「内容相同但时间轴错位」。

逐采样点相关系数对错位零容忍：同一句话晚 200 ms 起始，相关就掉到 0 附近。
所以这里换两个对时间轴鲁棒的指标：
  1) ASR 转写 —— 说的是不是同一句话
  2) 能量包络互相关 —— 语句的节奏骨架是否一致，顺带算出整体偏移
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import numpy as np
import soundfile as sf

import asr

BENCH = ROOT / "tools" / "_bench"


def envelope(x: np.ndarray, sr: int, frame_ms: float = 20.0) -> np.ndarray:
    hop = max(1, int(sr * frame_ms / 1000))
    n = len(x) // hop
    if n == 0:
        return np.zeros(1, dtype=np.float64)
    frames = x[: n * hop].astype(np.float64).reshape(n, hop)
    env = np.sqrt(np.mean(np.square(frames), axis=1))
    # 归一化只去掉整体音量差，保留节奏形状
    m = env.max()
    return env / m if m > 0 else env


def best_lag(a: np.ndarray, b: np.ndarray, hop_ms: float = 20.0) -> tuple[float, float]:
    """返回 (最大相关系数, 对应偏移毫秒)。正数表示 a 比 b 晚。"""
    n = len(a) + len(b)
    size = 1 << (n - 1).bit_length()
    fa, fb = np.fft.rfft(a - a.mean(), size), np.fft.rfft(b - b.mean(), size)
    cc = np.fft.irfft(fa * np.conj(fb), size)
    cc = np.concatenate([cc[-(len(b) - 1):], cc[: len(a)]])
    denom = np.sqrt(np.sum((a - a.mean()) ** 2) * np.sum((b - b.mean()) ** 2))
    if denom <= 0:
        return 0.0, 0.0
    cc = cc / denom
    i = int(np.argmax(cc))
    lag = i - (len(b) - 1)
    return float(cc[i]), lag * hop_ms


def main() -> None:
    ok, why = asr.available()
    print(f"ASR: {ok} {why}\n")
    for tag in ("en", "zh"):
        files = {}
        for kind in ("static", "dynamic"):
            p = BENCH / f"parity_{tag}_{kind}.wav"
            if not p.exists():
                print(f"缺文件 {p}")
                continue
            wav, sr = sf.read(str(p), dtype="float32", always_2d=False)
            files[kind] = (wav, sr)

        print(f"=== [{tag}] ===")
        texts = {}
        for kind, (wav, sr) in files.items():
            texts[kind] = asr.transcribe(wav, sr) if ok else ""
            print(f"  {kind:<8} {len(wav)/sr:5.2f}s  ASR: {texts[kind]!r}")
        if ok and texts.get("static") == texts.get("dynamic"):
            print("  → 转写完全一致")
        elif ok:
            print("  → 转写不同")

        envs = {k: envelope(w, s) for k, (w, s) in files.items()}
        c, lag = best_lag(envs["static"], envs["dynamic"])
        print(f"  包络互相关 最大 {c:.3f}  整体偏移 {lag:+.0f} ms")
        print()


if __name__ == "__main__":
    main()
