"""Qwen3-TTS Studio — 输出后处理（去长静音 / 音量归一化 / 淡入淡出）。

能力来源：omnivoice-studio 的 ``OmniVoice/omnivoice/utils/audio.py``
（``remove_silence`` / ``fade_and_pad_audio``）与 ``models/omnivoice.py``
里的 ``_post_process_audio``。搬过来时有两处**刻意的偏离**，都写在下面。

为什么值得搬：Qwen3-TTS 的原始输出有三个稳定存在的小毛病 ——
    · 句尾经常拖一截几百毫秒到两秒的空白（尤其是长句、带标点的收尾），
      一条 6 秒的音频里有 1.5 秒是空气，连播时显得节奏很散；
    · 不同音色 / 不同模式出来的响度不齐，剧本工坊多角色对话里尤其明显 ——
      御姐声音大、少年声音小，听着像两个人在两个房间里说话；
    · 开头第一个采样点直接就是波形，播放器起播时常有一下轻微「咔」。

两条偏离：

1. **不依赖 pydub。** omni 那边用 ``split_on_silence`` 逐 10 ms 建
   ``AudioSegment``，一条 30 秒音频要建上千个对象、再逐个 ``+=`` 拼接，
   单次耗时百毫秒级；这里改成 numpy 一次算完全部帧能量再按区间切，
   同一条音频约 1–3 ms。行为对齐（同样是 10 ms 帧、-50 dBFS 门限）。

2. **归一化的目标不同。** omni 是「按参考音频的 RMS 等比缩放」——
   保住了原音色的强弱关系，但**不同音色之间依然不齐**：各自的参考音频
   电平本来就不同，跟着参考走等于把差异原样带下去。本工具一次会话里
   要在多个音色 / 多个模式之间来回对比（这是它与单模型项目最大的区别），
   「齐」比「忠于参考电平」重要得多，所以改成**统一目标响度**。

设计约束：
    · 只用 numpy，不引入新依赖，也不需要 ffmpeg；
    · 任何一步都可以单独关掉；全部关掉时 ``apply()`` 原样返回输入；
    · 处理完**不会削波**（峰值被压到 ``CEILING`` 以内）；
    · 返回一份统计，让界面能说清「这次到底动了什么」。
"""

from __future__ import annotations

import numpy as np

# ---------------------------------------------------------------------------
# 参数默认值 —— 与 omnivoice-studio 的默认行为对齐，只有归一化目标是新定的
# ---------------------------------------------------------------------------
FRAME_MS = 10           # 帧长（毫秒）。10 ms 是语音活动检测的常规粒度
SILENCE_DB = -50.0      # 静音门限（dBFS）。与 omni 的 silence_thresh 一致

MID_SIL_MS = 500        # 句中停顿超过这个长度才压
EDGE_SIL_MS = 100       # 首尾各保留这么长的静音
KEEP_SIL_MS = 500       # 长停顿压到这么长（omni 的 keep_silence=mid_sil）

TARGET_DB = -20.0       # 归一化目标：有声部分的 RMS 中位数
CEILING = 0.97          # 峰值上限（约 -0.26 dBFS），留一点余量不削波
MAX_GAIN_DB = 18.0      # 最多放大这么多，避免把底噪一起抬起来
MIN_GAIN_DB = -20.0     # 最多衰减这么多

FADE_SEC = 0.1          # 淡入 / 淡出时长
PAD_SEC = 0.1           # 首尾留白

# payload 里的字段名，服务端与前端共用这一份
KEYS = ("pp_silence", "pp_normalize", "pp_fade")


def defaults() -> dict:
    """给 /api/bootstrap 用：界面据此画默认勾选状态。"""
    return {
        "silence": True,
        "normalize": True,
        "fade": True,
        "mid_ms": MID_SIL_MS,
        "edge_ms": EDGE_SIL_MS,
        "target_db": TARGET_DB,
    }


def options(p: dict | None) -> dict:
    """从生成 payload 抽出后处理选项。

    缺省全部开启 —— 与 omnivoice-studio 的默认一致（那边三个开关默认都勾）。
    前端显式传 ``false`` 才关；传了 ``postprocess: false`` 则整组关掉
    （给脚本化调用留一个「我全都要原始音频」的短路径）。
    """
    p = p or {}
    if p.get("postprocess") is False:
        return {"silence": False, "normalize": False, "fade": False,
                "mid_ms": MID_SIL_MS, "edge_ms": EDGE_SIL_MS, "target_db": TARGET_DB}
    return {
        "silence": bool(p.get("pp_silence", True)),
        "normalize": bool(p.get("pp_normalize", True)),
        "fade": bool(p.get("pp_fade", True)),
        "mid_ms": _int_in(p.get("pp_mid_ms"), MID_SIL_MS, 100, 3000),
        "edge_ms": _int_in(p.get("pp_edge_ms"), EDGE_SIL_MS, 0, 1000),
        "target_db": _float_in(p.get("pp_target_db"), TARGET_DB, -40.0, -6.0),
    }


def _int_in(v, default: int, lo: int, hi: int) -> int:
    """把外部传来的数字夹进合理区间。

    夹而不是报错：这几个值只影响听感，不该让一单生成因为一个越界滑块失败。
    """
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _float_in(v, default: float, lo: float, hi: float) -> float:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    if n != n:  # NaN
        return default
    return max(lo, min(hi, n))


# ---------------------------------------------------------------------------
# 基础测量
# ---------------------------------------------------------------------------
def frame_db(wav: np.ndarray, sr: int, frame_ms: int = FRAME_MS) -> tuple[np.ndarray, int]:
    """逐帧算 RMS 电平（dBFS），返回 (每帧 dB, 每帧采样数)。

    用 RMS 而不是峰值：峰值对单个爆点太敏感，一段安静语音里有个轻微齿音
    就会被判成「有声」，静音检测会漏掉真正的停顿。
    """
    hop = max(1, int(sr * frame_ms / 1000))
    n = len(wav) // hop
    if n <= 0:
        return np.zeros(0, dtype=np.float64), hop
    frames = np.asarray(wav[: n * hop], dtype=np.float64).reshape(n, hop)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    # 1e-9 的下限避免 log10(0)；对应 -180 dBFS，远低于任何真实底噪
    return 20.0 * np.log10(np.maximum(rms, 1e-9)), hop


def _edge_fade(x: np.ndarray, sr: int, ms: float = 3.0) -> np.ndarray:
    """给**切口**补极短的淡入淡出（默认 3 ms）。

    拼接点在静音段里，电平已经低于 -50 dBFS，理论上切不出声；但原音频
    在停顿里往往有一层极低的底噪，从「有底噪」直接跳到「纯零」会在频谱上
    留一个台阶，放大音量后能听成一下轻微的「嗒」。3 ms 足够抹平，又短到
    不会吃掉任何字头。
    """
    k = int(sr * ms / 1000)
    if k <= 0 or len(x) < k * 2:
        return x
    ramp = np.linspace(0.0, 1.0, k, dtype=np.float32)
    x = x.copy()
    x[:k] *= ramp
    x[-k:] *= ramp[::-1]
    return x


# ---------------------------------------------------------------------------
# 1. 去长静音
# ---------------------------------------------------------------------------
def remove_long_silences(
    wav: np.ndarray,
    sr: int,
    mid_ms: int = MID_SIL_MS,
    edge_ms: int = EDGE_SIL_MS,
    keep_ms: int = KEEP_SIL_MS,
    thresh_db: float = SILENCE_DB,
) -> tuple[np.ndarray, float]:
    """压掉句中长停顿、裁掉首尾多余静音。

    · 句中超过 ``mid_ms`` 的停顿压成 ``keep_ms``（不是全删 —— 全删会让两句话
      粘在一起，听感上像掉了标点；保留半秒是最像「正常换气」的长度）
    · 首尾各只留 ``edge_ms``

    返回 ``(新波形, 剪掉多少秒)``。
    """
    if len(wav) == 0:
        return wav, 0.0

    db, hop = frame_db(wav, sr)
    if len(db) == 0:
        return wav, 0.0
    voiced = db > thresh_db
    if not voiced.any():
        # 整条都是静音：不要返回空数组（下游 save_wav 会写坏文件），
        # 但也不该假装处理成功 —— 剪掉 0 秒，让调用方看到「没动」
        return wav, 0.0

    before = len(wav)

    # --- 首尾 ---
    first = int(np.argmax(voiced))
    last = len(voiced) - 1 - int(np.argmax(voiced[::-1]))
    pad = int(sr * edge_ms / 1000)
    start = max(0, first * hop - pad)
    end = min(len(wav), (last + 1) * hop + pad)
    core = np.asarray(wav[start:end], dtype=np.float32)

    # --- 句中 ---
    # 在裁过边的片段上重新按同样的帧网格判断（hop 不变，索引直接复用）
    n = len(core) // hop
    if n <= 0:
        return core, round((before - len(core)) / sr, 3)
    frames = np.asarray(core[: n * hop], dtype=np.float64).reshape(n, hop)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    vd = 20.0 * np.log10(np.maximum(rms, 1e-9)) > thresh_db

    mid_ms = max(0, int(mid_ms))
    keep_n = int(sr * keep_ms / 1000)
    parts: list[np.ndarray] = []
    prev_end = 0
    i = 0
    while i < n:
        if vd[i]:
            i += 1
            continue
        j = i
        while j < n and not vd[j]:
            j += 1
        run_ms = (j - i) * FRAME_MS
        if mid_ms > 0 and run_ms >= mid_ms:
            cut_from = i * hop
            cut_to = min(len(core), j * hop)
            gap = cut_to - cut_from
            if gap > keep_n:
                # 保留下来的静音居中放：切口前后各一半，听感上停顿位置不变
                head = keep_n // 2
                tail = keep_n - head
                seg = core[prev_end: cut_from + head]
                if len(seg):
                    parts.append(_edge_fade(seg, sr))
                prev_end = max(prev_end, cut_to - tail)
        i = j

    tail_seg = core[prev_end:]
    if len(tail_seg):
        parts.append(_edge_fade(tail_seg, sr))

    out = np.concatenate(parts) if parts else core
    return out, round((before - len(out)) / sr, 3)


# ---------------------------------------------------------------------------
# 2. 音量归一化
# ---------------------------------------------------------------------------
def normalize_loudness(
    wav: np.ndarray,
    sr: int,
    target_db: float = TARGET_DB,
    ceiling: float = CEILING,
    max_gain_db: float = MAX_GAIN_DB,
    min_gain_db: float = MIN_GAIN_DB,
) -> tuple[np.ndarray, float]:
    """把**有声部分**的 RMS 中位数拉到 ``target_db``，再保证峰值不超 ``ceiling``。

    为什么要先去掉静音再统计（调用顺序上本函数排在去静音之后）：
    一条 6 秒音频里若含 1.5 秒空白，整体 RMS 会被拉低 3–4 dB，
    直接按整体算会得出一个偏大的增益，把有声音的部分推爆。

    返回 ``(新波形, 实际增益 dB)``。实际增益可能小于计算值 ——
    峰值超过上限时会整体压回来，这一步会让响度略低于目标，
    但比削波失真强得多。
    """
    if len(wav) == 0:
        return wav, 0.0

    db, _hop = frame_db(wav, sr)
    if len(db) == 0:
        return wav, 0.0
    voiced = db > SILENCE_DB
    if int(voiced.sum()) < 3:
        return wav, 0.0

    cur = float(np.median(db[voiced]))
    gain_db = float(np.clip(target_db - cur, min_gain_db, max_gain_db))
    if abs(gain_db) < 0.15:
        return wav, 0.0

    out = np.asarray(wav, dtype=np.float32) * np.float32(10.0 ** (gain_db / 20.0))

    peak = float(np.abs(out).max()) if len(out) else 0.0
    if peak > ceiling and peak > 0:
        # Qwen 的输出里常有孤立尖峰：实测真实产物波峰因子能到 20–24 dB
        # （正常语音 12–18），99.9 分位才 0.33 而峰值 0.56 —— 就是几个点特别大。
        # 遇到这种音频，**按整条回缩**等于为了几个样本把 6 dB 的响度白扔掉，
        # 多角色之间反而更不齐。所以先判断：尖峰占比很低就做软膝限幅（只压尖峰），
        # 占比高才说明是真的大声，整体缩。
        knee = ceiling * 0.85
        a = np.abs(out)
        frac = float((a > knee).mean()) if len(out) else 0.0
        if 0.0 < frac < 0.02:
            span = ceiling - knee
            limited = knee + span * np.tanh((a - knee) / span)
            out = np.sign(out) * np.where(a > knee, limited, a).astype(np.float32)
        else:
            out *= np.float32(ceiling / peak)

    # 报告**实际**生效的增益：按有声部分的 RMS 中位数前后各算一次。
    # 不能用峰值比 —— 软限幅之后峰值被削平了，比值会小于真实响度变化；
    # 也不能直接报 gain_db —— 被回压过的话那个数是骗人的。
    after_db, _ = frame_db(out, sr)
    av = after_db[after_db > SILENCE_DB]
    real = (float(np.median(av)) - cur) if len(av) else gain_db
    return out, round(float(real), 2)


# ---------------------------------------------------------------------------
# 3. 淡入淡出 + 首尾留白
# ---------------------------------------------------------------------------
def fade_and_pad(
    wav: np.ndarray,
    sr: int,
    fade_sec: float = FADE_SEC,
    pad_sec: float = PAD_SEC,
) -> np.ndarray:
    """首尾各做一次线性渐变，两端各补一段静音。

    渐变长度会被夹到「音频长度的一半」以内 —— 否则一条 0.15 秒的短音
    会被两个 fade 吃光，输出一片寂静。
    """
    if len(wav) == 0:
        return wav

    x = np.asarray(wav, dtype=np.float32).copy()
    k = min(int(sr * fade_sec), len(x) // 2)
    if k > 0:
        ramp = np.linspace(0.0, 1.0, k, dtype=np.float32)
        x[:k] *= ramp
        x[-k:] *= ramp[::-1]

    pad = int(sr * pad_sec)
    if pad > 0:
        zeros = np.zeros(pad, dtype=np.float32)
        x = np.concatenate([zeros, x, zeros])
    return x


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def apply(wav: np.ndarray, sr: int, opts: dict | None = None) -> tuple[np.ndarray, dict]:
    """按「去静音 → 归一化 → 淡入淡出」的顺序处理，返回 ``(波形, 统计)``。

    顺序不能换：
        · 归一化必须在去静音**之后**，否则静音会把统计拖低；
        · 淡入淡出和留白必须在归一化**之后**，否则补进去的零会让 RMS 重新变小
          （更糟的是，若先补白再归一化，空白段的「静音」会被一起放大成底噪）。
    """
    o = opts or {"silence": False, "normalize": False, "fade": False}
    x = np.asarray(wav, dtype=np.float32).ravel()
    if len(x) == 0:
        return x, {"applied": False, "reason": "空音频"}

    # 上游偶发 NaN / inf（fp16 溢出后的残渣），在这里清一次，
    # 免得后面的 gain 把 NaN 铺满整条音频
    if not np.isfinite(x).all():
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)

    stats: dict = {
        "applied": False,
        "silence": bool(o.get("silence")),
        "normalize": bool(o.get("normalize")),
        "fade": bool(o.get("fade")),
        "cut_secs": 0.0,
        "gain_db": 0.0,
        "mid_ms": o.get("mid_ms", MID_SIL_MS),
        "edge_ms": o.get("edge_ms", EDGE_SIL_MS),
        "target_db": o.get("target_db", TARGET_DB),
    }

    if o.get("silence"):
        x, cut = remove_long_silences(
            x, sr,
            mid_ms=o.get("mid_ms", MID_SIL_MS),
            edge_ms=o.get("edge_ms", EDGE_SIL_MS),
        )
        stats["cut_secs"] = cut
        if len(x) == 0:
            # 全被判成静音：退回原始音频，不能交一个空文件出去
            return np.asarray(wav, dtype=np.float32).ravel(), {
                **stats, "silence": False, "reason": "整条被判为静音，已跳过裁剪"}

    if o.get("normalize"):
        x, gain = normalize_loudness(x, sr, target_db=o.get("target_db", TARGET_DB))
        stats["gain_db"] = gain

    if o.get("fade"):
        x = fade_and_pad(x, sr)

    stats["applied"] = bool(stats["silence"] or stats["normalize"] or stats["fade"])
    return x.astype(np.float32, copy=False), stats


def summary(stats: dict) -> str:
    """把统计压成一句人话，给状态行和结果卡用。"""
    if not stats or not stats.get("applied"):
        return "未做后处理"
    bits = []
    if stats.get("silence") and stats.get("cut_secs", 0) > 0.05:
        bits.append(f"剪掉 {stats['cut_secs']:.1f}s 静音")
    elif stats.get("silence"):
        bits.append("静音已够紧凑")
    if stats.get("normalize"):
        g = stats.get("gain_db", 0.0)
        bits.append(f"响度 {g:+.1f} dB" if abs(g) >= 0.15 else "响度已达标")
    if stats.get("fade"):
        bits.append("首尾渐变")
    return "，".join(bits) if bits else "未做后处理"
