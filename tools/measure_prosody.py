# -*- coding: utf-8 -*-
"""量一段音频的「情绪强度」：音高起伏 / 能量动态 / 语速 / 嘶哑度。

只读，不改任何文件。用来判断两段音频「听起来是不是更激动」。

**先看噪声底再下结论。** 参数完全相同的重复生成并非逐帧一致：
实测（同一音色、同一文本、同一指令，连跑 6 条）
    时长     3.91s ± 0.09   （极差 0.24）—— 很稳
    基频     108.6 ± 4.4 Hz（极差 11.7）
    音高起伏 22.8 ± 2.5 半  （极差 7.6）—— 噪声最大的一维
    能量动态 81.6 ± 2.2 dB  （极差 6.6）
所以**只差两三个半音、只跑一两条就下结论，是没有意义的**。
要比两个条件，每个条件至少跑五六条看均值；而且要优先看方差小的维度（时长、起音、基频）。

用法：
    runtime\\python\\python.exe tools\\measure_prosody.py voices\\皇帝.wav outputs\\xxx.wav
    runtime\\python\\python.exe tools\\measure_prosody.py --all-recent 12
    runtime\\python\\python.exe tools\\measure_prosody.py --spread outputs\\a.wav outputs\\b.wav ...
        （只打印离散度统计，用于同参数重复样本）
"""
from __future__ import annotations

import sys
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

# 人的听感是对数的，所以音高一律换半音看，而不是看 Hz。
F0_MIN, F0_MAX, F0_REF = 70.0, 400.0, 100.0


def measure(path: str | Path) -> dict:
    y, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)

    # 掐掉首尾静音再算，否则语速和动态会被一段空白稀释
    yt, _ = librosa.effects.trim(y, top_db=35)
    dur = len(y) / sr
    dur_valid = max(len(yt) / sr, 1e-6)

    f0 = librosa.yin(yt, fmin=F0_MIN, fmax=F0_MAX, sr=sr, frame_length=1024)
    f0 = f0[np.isfinite(f0)]
    st = 12 * np.log2(np.clip(f0, F0_MIN, F0_MAX) / F0_REF)
    # 用 5–95 分位而不是 min/max：yin 偶尔会跳八度，极值不可信
    lo, hi = np.percentile(st, [5, 95])

    rms = librosa.feature.rms(y=yt, frame_length=1024, hop_length=256)[0]
    db = 20 * np.log10(np.maximum(rms, 1e-6))
    d10, d90 = np.percentile(db, [10, 90])

    onsets = librosa.onset.onset_detect(
        onset_envelope=librosa.onset.onset_strength(y=yt, sr=sr), sr=sr, units="time")

    # 嘶哑度：大喊 / 嘶吼 的核心特征其实是「声带挤出来的噪声」，
    # 而不是音高起伏。用两个互补的量：
    #   flatness —— 频谱平坦度，越大越像噪声（气声、破音、沙哑）
    #   hnr_db   —— 谐波与噪声的能量比，越低越粗糙
    flatness = float(np.mean(librosa.feature.spectral_flatness(y=yt)))
    harm = librosa.effects.harmonic(yt, margin=3.0)
    resid = yt[: len(harm)] - harm
    e_h, e_r = float(np.sum(harm ** 2)), float(np.sum(resid ** 2)) + 1e-12
    hnr_db = 10 * np.log10(e_h / e_r + 1e-12)

    return {
        "dur": round(dur, 2),
        "dur_valid": round(dur_valid, 2),
        "f0": round(float(np.median(f0)), 1),
        "range_st": round(float(hi - lo), 1),   # 音高起伏
        "std_st": round(float(np.std(st)), 1),
        "dyn_db": round(float(d90 - d10), 1),   # 能量动态：音量落差
        "peak_db": round(float(db.max()), 1),
        "onset_rate": round(len(onsets) / dur_valid, 2),  # 语速代理
        "flatness": round(flatness, 4),
        "hnr_db": round(hnr_db, 1),
    }


HEAD = (f"{'文件':<34}{'时长':>7}{'基频':>9}{'音高起伏':>10}"
        f"{'能量动态':>10}{'起音/秒':>9}{'嘶哑度':>9}{'谐噪比':>8}")
RULE = "-" * 95

# 列名 -> (取哪个键, 小数位, 单位)。--spread 直接照这个表报离散度。
COLUMNS = [
    ("时长", "dur", 2, "s"),
    ("基频", "f0", 1, "Hz"),
    ("音高起伏", "range_st", 1, "半"),
    ("能量动态", "dyn_db", 1, "dB"),
    ("起音/秒", "onset_rate", 2, ""),
    ("嘶哑度", "flatness", 4, ""),
    ("谐噪比", "hnr_db", 1, "dB"),
]


def show(path: Path, label: str | None = None) -> dict:
    m = measure(path)
    name = label or path.name
    if len(name) > 33:
        name = name[:32] + "…"
    print(f"{name:<34}{m['dur']:6.2f}s{m['f0']:8.1f}Hz"
          f"{m['range_st']:9.1f}半{m['dyn_db']:9.1f}dB{m['onset_rate']:8.2f}"
          f"{m['flatness']:9.4f}{m['hnr_db']:7.1f}dB")
    return m


def show_spread(paths: list[Path]) -> None:
    """同参数重复样本的离散度 —— 判断「差异是不是噪声」的依据。"""
    import statistics as S

    rows = [measure(p) for p in paths]
    if not rows:
        return
    print(f"样本数 {len(rows)}")
    print(f"{'指标':<10}{'均值':>10}{'标准差':>10}{'极差':>10}{'最小':>10}{'最大':>10}")
    print("-" * 60)
    for label, key, nd, unit in COLUMNS:
        v = [r[key] for r in rows]
        print(f"{label:<10}{S.mean(v):10.{nd}f}{S.pstdev(v):10.{nd}f}"
              f"{max(v)-min(v):10.{nd}f}{min(v):10.{nd}f}{max(v):10.{nd}f}")
    print("-" * 60)
    print("★ 两个条件要比均值，且差值要明显超过「标准差」才谈得上有效 ——")
    print("  只差一两个半音、只跑一两条就下结论，等于在看噪声。")


def recent(n: int) -> list[Path]:
    out = Path("outputs")
    files = sorted(out.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:n]


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    if args[0] == "--spread":
        show_spread([Path(a) for a in args[1:] if Path(a).exists()])
        return
    if args[0] == "--all-recent":
        n = int(args[1]) if len(args) > 1 else 10
        targets = recent(n)
    else:
        targets = [Path(a) for a in args]

    print(HEAD)
    print(RULE)
    for p in targets:
        if not p.exists():
            print(f"{p.name:<34}  文件不存在")
            continue
        show(p)
    print(RULE)
    print("音高起伏越大、能量动态越大、起音越密 → 越激动；")
    print("嘶哑度越大、谐噪比越低 → 越像喊出来的（挤嗓、破音、沙哑）。")


if __name__ == "__main__":
    main()
