"""基准：加速版一致性 + 流式 TTFA/RTF。

为什么不走 core.py：core.py 是给界面用的，带 CUDA graph 预热、显存管理、
错误话术与降级重试，任何一层包装都会污染计时。基准要的是「同一份权重、
两条路径、同一个 seed」，所以这里直接调 FasterQwen3TTS。

两条路径怎么共存：FasterQwen3TTS 内部就是上游 Qwen3TTSModel 的实例
（self.model），所以 `fast.model.generate_voice_clone(...)` 走的是上游的
dynamic-cache 路径 —— 不用加载两遍权重，显存翻不了倍。

不写 outputs/、不碰 voices/。结果只落 tools/_bench/。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "vendor" / "faster-qwen3-tts-main"))

import numpy as np
import soundfile as sf
import torch

import config as cfg
from faster_qwen3_tts import FasterQwen3TTS

OUT = ROOT / "tools" / "_bench"
OUT.mkdir(exist_ok=True)

REF_AUDIO = ROOT / "vendor" / "faster-qwen3-tts-main" / "ref_audio.wav"
REF_TEXT = (
    "I'm confused why some people have super short timelines, yet at the same time "
    "are bullish on scaling up reinforcement learning atop LLMs. If we're actually "
    "close to a human-like learner, then this whole approach of training on "
    "verifiable outcomes is doomed."
)

TEXT_EN = "The quick brown fox jumps over the lazy dog near the river bank."
TEXT_ZH = "今天的天气不错，我们一起出去走走吧。这条路一直通到河边，风景很好。"


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


def compare(a: np.ndarray, b: np.ndarray, sr: int) -> dict:
    """两条路径的输出差多少。长度不同就先截到最短再比。"""
    n = min(len(a), len(b))
    x, y = a[:n].astype(np.float64), b[:n].astype(np.float64)
    if n == 0:
        return {"n": 0}
    # 相关系数对整体音量不敏感，用来判断「是不是同一段话」
    corr = float(np.corrcoef(x, y)[0, 1]) if x.std() > 0 and y.std() > 0 else 0.0
    diff = x - y
    # SNR：把差异当成噪声，看它相对信号有多小
    sig = rms(x)
    snr = 20 * np.log10(sig / rms(diff)) if rms(diff) > 0 else float("inf")
    return {
        "len_a_s": round(len(a) / sr, 3),
        "len_b_s": round(len(b) / sr, 3),
        "dur_diff_pct": round((len(a) - len(b)) / max(len(b), 1) * 100, 2),
        "corr": round(corr, 5),
        "snr_db": round(float(snr), 2),
        "rms_diff_pct": round((rms(a) - rms(b)) / max(rms(b), 1e-9) * 100, 2),
    }


def main() -> None:
    size = os.environ.get("BENCH_SIZE", "0.6B")
    sub = cfg.MODEL_DIRS[("clone", size)]
    path = cfg.MODELS_DIR / sub
    if not path.exists():
        raise SystemExit(f"权重不存在：{path}")

    report: dict = {"size": size, "ref_audio": str(REF_AUDIO)}

    print(f"[1/3] 加载 {sub} …", flush=True)
    t0 = time.time()
    model = FasterQwen3TTS.from_pretrained(
        str(path).replace("\\", "/"),
        device="cuda",
        dtype=torch.bfloat16,
    )
    report["load_seconds"] = round(time.time() - t0, 2)
    print(f"      就绪 {report['load_seconds']}s", flush=True)

    # ---- 预热：第一次调用要捕获 CUDA graph，这次耗时没有参考价值 ----
    print("[2/3] 预热（含 CUDA graph 捕获）…", flush=True)
    t0 = time.time()
    w, sr = model.generate_voice_clone(
        text="Hello.", language="English", ref_audio=REF_AUDIO.as_posix(),
        ref_text=REF_TEXT, xvec_only=True, do_sample=False, max_new_tokens=128,
    )
    report["warmup_seconds"] = round(time.time() - t0, 2)
    print(f"      预热 {report['warmup_seconds']}s  sr={sr}", flush=True)

    # ---- A. 一致性：同一份权重，静态 cache vs 上游 dynamic cache ----
    print("[3/3] 一致性对比（do_sample=False，无随机性）…", flush=True)
    cases = {}
    for tag, text, lang in (("en", TEXT_EN, "English"), ("zh", TEXT_ZH, "Chinese")):
        t0 = time.time()
        a_list, sr_a = model.generate_voice_clone(
            text=text, language=lang, ref_audio=REF_AUDIO.as_posix(),
            ref_text=REF_TEXT, xvec_only=True, do_sample=False,
        )
        t_static = time.time() - t0
        a = np.asarray(a_list[0], dtype=np.float32)

        t0 = time.time()
        # self.model 是上游 Qwen3TTSModel 实例 → 走 dynamic cache
        b_list, sr_b = model.model.generate_voice_clone(
            text=text, language=lang, ref_audio=REF_AUDIO.as_posix(),
            ref_text=REF_TEXT, xvec_only_mode=True, do_sample=False,
            non_streaming_mode=True,
        )
        t_dyn = time.time() - t0
        b = np.asarray(b_list[0], dtype=np.float32)

        sf.write(str(OUT / f"parity_{tag}_static.wav"), a, sr)
        sf.write(str(OUT / f"parity_{tag}_dynamic.wav"), b, sr)
        info = compare(a, b, sr)
        info["t_static_s"] = round(t_static, 2)
        info["t_dynamic_s"] = round(t_dyn, 2)
        info["speedup"] = round(t_dyn / t_static, 2) if t_static > 0 else None
        cases[tag] = info
        print(f"      [{tag}] {json.dumps(info, ensure_ascii=False)}", flush=True)
    report["parity"] = cases

    # ---- B. 流式：TTFA 与 RTF ----
    print("      流式基准 …", flush=True)
    streams = {}
    for cs in (4, 8, 12):
        for tag, text, lang in (("zh", TEXT_ZH, "Chinese"),):
            t0 = time.time()
            first = None
            chunks: list[np.ndarray] = []
            for audio_chunk, sr, timing in model.generate_voice_clone_streaming(
                text=text, language=lang, ref_audio=REF_AUDIO.as_posix(),
                ref_text=REF_TEXT, xvec_only=True, do_sample=True, chunk_size=cs,
            ):
                if first is None:
                    first = time.time() - t0
                chunks.append(np.asarray(audio_chunk, dtype=np.float32))
            total = time.time() - t0
            audio_s = sum(len(c) for c in chunks) / sr
            key = f"chunk{cs}_{tag}"
            streams[key] = {
                "chunk_size": cs,
                "chunks": len(chunks),
                "ttfa_ms": round((first or 0) * 1000),
                "total_s": round(total, 2),
                "audio_s": round(audio_s, 2),
                "rtf": round(total / audio_s, 3) if audio_s else None,
                "ms_per_chunk": round(audio_s / max(len(chunks), 1) * 1000),
            }
            print(f"      [{key}] {json.dumps(streams[key], ensure_ascii=False)}", flush=True)
    report["stream"] = streams

    # ---- C. 同一音色连续两次：验证内部 prompt 缓存是否真的生效 ----
    times = []
    for _ in range(2):
        t0 = time.time()
        model.generate_voice_clone(
            text=TEXT_EN, language="English", ref_audio=REF_AUDIO.as_posix(),
            ref_text=REF_TEXT, xvec_only=True, do_sample=False,
        )
        times.append(round(time.time() - t0, 2))
    report["repeat_times"] = times
    report["cache_speedup"] = round(times[0] / times[1], 2) if times[1] > 0 else None
    print(f"      重复调用 {times}  第二次快 {report['cache_speedup']}x", flush=True)

    (OUT / "bench.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print("\n=== 汇总 ===")
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
