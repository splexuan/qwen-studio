"""Qwen3-TTS Studio — 参考音频转写（**可选能力**，默认 auto 自动探测）。

为什么是「可选」：
    识别模型（SenseVoice Small，int8 约 0.24 GB）与 sherpa-onnx 运行时都不属于
    Qwen3-TTS 本体，是**额外**带上的。项目整体离线分发，缺了没法临时联网补，
    所以这里只负责**如实报告**能不能用，由界面决定禁用还是可点。

    现状（本机，2026-10-15）：sherpa-onnx 1.13.8 已随包放进 runtime/，
    SenseVoice 权重已放进 models/sensevoice-small/，因此**可用**。

想关掉：设环境变量 ``QWENSTUDIO_ASR=0``。想强制开：``QWENSTUDIO_ASR=1``。

为什么选 SenseVoice 而不是 Whisper（沿用了参照项目的结论）：
    实测两者转出的字完全一致，但 SenseVoice 自带中文标点、快约 40 倍、
    权重只有 0.24 GB（Whisper large-v3-turbo 是 1.6 GB）。

**定位**：它是「帮你少打字」的工具，不是「填进去就不用管」的保证 ——
识别结果可能有错别字，而完整 ICL 模式下参考文本写错会直接污染输出，
所以界面上转写完会提示用户核对一遍。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import numpy as np

import config as cfg

SENSEVOICE_SUBDIR = "sensevoice-small"

# 常见的权重目录命名，逐个试
_DIR_HINTS = (
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
    "sensevoice-small",
    "SenseVoiceSmall",
)

_recognizer = None
_load_error: Optional[str] = None


def sensevoice_dir() -> Optional[Path]:
    """定位 SenseVoice 权重目录；找不到返回 None。"""
    cands = [cfg.MODELS_DIR / SENSEVOICE_SUBDIR]
    cands += [cfg.MODELS_DIR / h for h in _DIR_HINTS]
    env = os.environ.get("QWENSTUDIO_SENSEVOICE_DIR")
    if env:
        cands.insert(0, Path(env))
    for p in cands:
        if p.exists() and any(p.glob("*.onnx")):
            return p
    return None


def available() -> tuple[bool, str]:
    """返回 (是否可用, 说明)。界面据此决定按钮是可点还是禁用（并把说明挂成 tooltip）。

    三段式检查，**失败原因要具体到「缺哪一样」** ——
    「不可用」三个字对排障没有任何帮助。
    """
    if cfg.ASR_ENABLED is False:
        return False, "自动转写被强制关闭（QWENSTUDIO_ASR=0）。"

    try:
        import sherpa_onnx  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, (
            "未安装 sherpa-onnx 运行时，无法自动转写。"
            f"（{type(e).__name__}：离线分发时需把该包放进 runtime/python/Lib/site-packages/）"
        )

    d = sensevoice_dir()
    if d is None:
        return False, f"未找到 SenseVoice 权重目录（期望 models/{SENSEVOICE_SUBDIR}/，需含 *.onnx 与 tokens.txt）。"
    return True, f"可用（{d.name}）"


def _get_recognizer():
    """懒加载识别器（约 0.24 GB，纯 CPU）。"""
    global _recognizer, _load_error
    if _recognizer is not None:
        return _recognizer
    if _load_error:
        raise RuntimeError(_load_error)

    ok, why = available()
    if not ok:
        _load_error = why
        raise RuntimeError(why)

    import sherpa_onnx

    d = sensevoice_dir()
    assert d is not None
    onnx = next(iter(d.glob("*.onnx")))
    tokens = next(iter(d.glob("tokens*.txt")), None)
    if tokens is None:
        _load_error = f"{d} 下找不到 tokens.txt"
        raise RuntimeError(_load_error)

    try:
        _recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(onnx),
            tokens=str(tokens),
            num_threads=2,
            use_itn=True,          # 输出带标点与数字规范化的文本
            language="auto",
        )
    except Exception as e:  # noqa: BLE001
        _load_error = f"SenseVoice 初始化失败：{e}"
        raise RuntimeError(_load_error) from e
    return _recognizer


def transcribe(waveform: np.ndarray, sr: int) -> str:
    """把一段音频转成文本。失败会抛异常，由调用方决定如何提示。"""
    rec = _get_recognizer()
    arr = np.asarray(waveform, dtype=np.float32)
    if arr.ndim > 1:
        arr = arr.mean(axis=1)

    # SenseVoice 期望 16 kHz 单声道
    if sr != 16000:
        try:
            import librosa

            arr = librosa.resample(arr, orig_sr=sr, target_sr=16000)
        except Exception:  # noqa: BLE001  退化成线性插值，够用
            idx = np.linspace(0, len(arr) - 1, int(len(arr) * 16000 / sr))
            arr = np.interp(idx, np.arange(len(arr)), arr).astype(np.float32)

    stream = rec.create_stream()
    stream.accept_waveform(16000, arr)
    rec.decode_stream(stream)
    return (stream.result.text or "").strip()
