"""Qwen3-TTS Studio — 生成核心（**不含任何界面代码**）。

职责边界：
    · ``config.py``      —— 路径与「用户想强制什么」
    · ``hardware.py``    —— 探测本机 + 自动决出档位（Profile）
    · **本文件**         —— 照 Profile 干活：模型调度 + 四个功能的生成 + 错误话术
    · ``web/server.py``  —— 只调用本文件的模块级函数，不重复实现生成逻辑

模型调度是本文件与单模型项目最大的不同：Qwen3-TTS 有 5 套权重
（design 1.7B / clone 1.7B·0.6B / official 1.7B·0.6B），
显存装不下两套，因此切换功能时必须**先卸载再加载**。
:class:`ModelManager` 把这套逻辑收敛成 ``ensure(kind, size)`` 一个入口。

音频长度单位为秒；所有生成入口统一返回 ``(waveform, sample_rate, status, level)``，
``status`` 是给用户看的一句话，``level`` 取 ``ok`` / ``warn`` / ``error``。
"""

from __future__ import annotations

import gc
import os
import random
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np
import soundfile as sf
import torch

import config as cfg
import postproc
import voices as voices_mod

# ---------------------------------------------------------------------------
# 全局锁
# ---------------------------------------------------------------------------
# 模型加载/卸载与生成都碰 GPU，必须串行；否则两个请求同时进来会
# 一个在卸模型、一个在生成，直接崩。
_GPU_LOCK = threading.RLock()

# transformers 的 from_pretrained 只在首次加载模型时打一次补丁
_PATCHED = False


def _patch_transformers() -> None:
    """强制本地加载 + sdpa 注意力。

    为什么必须打这个补丁（原项目踩过的坑）：
      1. 0.6B 权重的 config 里写了 flash_attention_2，但本机没装 flash-attn，
         加载时直接抛异常；换成 sdpa 走 PyTorch 原生实现即可。
      2. 离线环境下必须显式 local_files_only，否则 transformers 会尝试联网
         解析 revision，超时后报一个与真实原因无关的错。
    """
    global _PATCHED
    if _PATCHED:
        return
    try:
        from transformers import PretrainedConfig, PreTrainedModel

        _orig_model = PreTrainedModel.from_pretrained
        _orig_config = PretrainedConfig.from_pretrained

        def _patched(cls, *args, **kwargs):
            kwargs["local_files_only"] = True
            kwargs["attn_implementation"] = "sdpa"
            return _orig_model.__func__(cls, *args, **kwargs)

        def _patched_cfg(cls, *args, **kwargs):
            kwargs["local_files_only"] = True
            return _orig_config.__func__(cls, *args, **kwargs)

        PreTrainedModel.from_pretrained = classmethod(_patched)
        PretrainedConfig.from_pretrained = classmethod(_patched_cfg)
    except Exception:  # noqa: BLE001  打不上补丁也不该让整个程序起不来
        pass
    _PATCHED = True


def _patch_torch_load() -> None:
    """允许 torch.load 反序列化音色基因文件（.pt）。

    PyTorch 2.6 起 torch.load 默认 weights_only=True，而音色基因里存的是
    自定义 dataclass ``VoiceClonePromptItem``，不注册就会报 UnpicklingError。
    """
    try:
        from qwen_tts.inference.qwen3_tts_model import VoiceClonePromptItem

        torch.serialization.add_safe_globals([VoiceClonePromptItem])
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 模型调度
# ---------------------------------------------------------------------------
class ModelManager:
    """管理 5 套权重的加载 / 复用 / 卸载。

    缓存策略：只保留**一套**当前模型。同 kind+size 重复请求直接复用；
    不同则先彻底卸载旧的（含 empty_cache）再加载新的。
    这是 6 GiB 显存能跑 1.7B 的前提 —— 两套并存必然 OOM。
    """

    def __init__(self) -> None:
        self._model = None
        self._key: Optional[tuple[str, str]] = None
        self._loading: Optional[tuple[str, str]] = None

    # -- 查询 ---------------------------------------------------------------
    @property
    def loaded_key(self) -> Optional[tuple[str, str]]:
        return self._key

    def status(self) -> dict:
        return {
            "loaded": None if self._key is None else {"kind": self._key[0], "size": self._key[1]},
            "loading": None if self._loading is None else {"kind": self._loading[0], "size": self._loading[1]},
            "vram_allocated_gib": _vram_gib(),
        }

    # -- 卸载 ---------------------------------------------------------------
    def _unload_locked(self) -> None:
        if self._model is None:
            return
        del self._model
        self._model = None
        self._key = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()

    def unload(self) -> None:
        with _GPU_LOCK:
            self._unload_locked()

    # -- 加载 ---------------------------------------------------------------
    def ensure(self, kind: str, size: str):
        """确保 (kind, size) 对应的模型就绪并返回它。"""
        key = (kind, size)
        with _GPU_LOCK:
            if self._model is not None and self._key == key:
                return self._model

            path = cfg.model_path(kind, size)
            sub = cfg.MODEL_DIRS.get(key)
            if sub is None:
                raise RuntimeError(f"未知的功能/规格组合：{kind} / {size}")
            if path is None:
                raise RuntimeError(
                    f"找不到权重目录 models/{sub}/。请确认模型已随包分发到位。"
                )

            self._unload_locked()
            self._loading = key
            try:
                _patch_transformers()
                _patch_torch_load()
                from faster_qwen3_tts import FasterQwen3TTS

                prof = cfg.profile()
                print(f"[模型] 加载 {sub} → {prof.device} / {prof.dtype} …", flush=True)
                t0 = time.time()
                model = FasterQwen3TTS.from_pretrained(
                    str(path).replace("\\", "/"),
                    device=prof.device,
                    dtype=prof.torch_dtype,
                )
                self._model = model
                self._key = key
                print(f"[模型] 就绪，用时 {time.time() - t0:.1f}s", flush=True)
                return model
            finally:
                self._loading = None


_MGR = ModelManager()


def load_model(kind: str, size: str):
    """供外部（server / script）使用的加载入口。"""
    return _MGR.ensure(kind, size)


def unload_model() -> None:
    _MGR.unload()


def model_status() -> dict:
    return _MGR.status()


def _vram_gib() -> float:
    try:
        if torch.cuda.is_available():
            return round(torch.cuda.memory_allocated() / 1024**3, 2)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


# ---------------------------------------------------------------------------
# 错误识别与话术
# ---------------------------------------------------------------------------
_OOM_PATTERNS = (
    "out of memory", "cuda out of memory", "cublas", "cuda error",
    "insufficient memory", "alloc", "nvml",
)


def _is_oom(e: BaseException) -> bool:
    s = f"{type(e).__name__}: {e}".lower()
    return any(p in s for p in _OOM_PATTERNS)


def _friendly_error(e: BaseException) -> str:
    """把底层异常翻译成人能看懂、且能指导下一步动作的话。"""
    s = str(e)
    if _is_oom(e):
        return (
            "显存不足。已尝试自动降级；若仍失败，请：\n"
            "· 把规格切到 0.6B；\n"
            "· 缩短单次文本（长文请用剧本工坊分段）；\n"
            "· 关闭其他占用显卡的程序后重试。"
        )
    if "找不到权重" in s or "No such file" in s or "not found" in s.lower():
        return f"权重或文件缺失：{s}"
    if "flash" in s.lower() and "attn" in s.lower():
        return "注意力实现不兼容（可能未打 sdpa 补丁）：" + s
    return s


# ---------------------------------------------------------------------------
# 进度回调
# ---------------------------------------------------------------------------
def _notify(
    cb, done: int, total: int, stage: str = "gen", meta: dict | None = None
) -> None:
    """把「第几段 / 共几段」报给调用方。

    ``stage``：``load`` = 模型还没就绪，``gen`` = 正在逐段生成。
    ``meta`` 是可选的结构化上下文（角色、源行号、当前片段等）。旧回调
    仍然只接收 ``(done, total, stage)`` 三个参数；新回调可以声明第四个参数。

    进度是**锦上添花**：前端少一帧更新不痛不痒，但回调里抛异常会把
    整次生成带崩（而且是在好不容易跑完某一段之后）。所以这里一律吞掉。
    回调也不允许阻塞 —— 它是被生成线程同步调的，慢一点就直接加在 RTF 上。
    """
    if not cb:
        return
    try:
        if meta is None:
            cb(int(done), int(total), stage)
        else:
            try:
                cb(int(done), int(total), stage, dict(meta))
            except TypeError:
                # 兼容只接受三参数的旧回调。不能靠签名探测：装饰器 / 偏函数
                # 的签名经常不可靠，实际调用一次最稳。
                cb(int(done), int(total), stage)
    except Exception:  # noqa: BLE001
        pass


def _run_pieces(
    pieces: list[str],
    gen_one,
    gap_secs: float,
    on_progress=None,
    meta: list[dict] | None = None,
) -> tuple[np.ndarray, int]:
    """逐段生成再拼接，边跑边报进度。

    clone / official(单人) 共用这一套 —— 两者的差别只在「一段怎么念」，
    而「切几段、怎么拼、进度口径是什么」必须完全一致，否则长文本在
    两个页面上的段数、段间静音、进度百分比全都不一样。

    ``gen_one(piece, index) -> (wav, sr)``；``index`` 从 0 起，
    调用方用它决定「第一段要不要预热音色基因」（只有第 0 段需要）。
    ``meta`` 与 ``pieces`` 等长时，每条进度都会带上对应的结构化上下文。
    """
    total = len(pieces)
    load_meta = meta[0] if meta and len(meta) == total else None
    _notify(on_progress, 0, total, "load", load_meta)
    if total == 1:
        wav, sr = gen_one(pieces[0], 0)
        _notify(on_progress, 1, 1, "gen", load_meta)
        return wav, sr
    segs: list[tuple[np.ndarray, int]] = []
    for i, piece in enumerate(pieces):
        wav_i, sr = gen_one(piece, i)
        segs.append((wav_i, sr))
        item_meta = meta[i] if meta and len(meta) == total else None
        _notify(on_progress, i + 1, total, "gen", item_meta)
    return _join_segments(segs, gap_secs)


# ---------------------------------------------------------------------------
# 文本切分（长文本 / 长台词）
# ---------------------------------------------------------------------------
_SENT_END = "。！？!?；;\n"
_CLAUSE_END = "，,、：: "
_BREAK_END = _SENT_END + _CLAUSE_END


def split_text(text: str, max_chars: int) -> list[str]:
    """把长文本切成长度均衡、且不超过 max_chars 的片段。

    Qwen3-TTS 单次生成受 max_new_tokens=2048 限制（12Hz 下约 170 秒音频），
    过长会截断或失控，因此送入前先切。切点优先落在句末标点上，
    避免在词中间断开发音。

    旧实现从前往后贪心：遇到 max_chars 才硬切，切点回退到最近的逗号。
    这样很容易切出「92 字 + 18 字」这种尾巴极短的两段 —— 第一段太长，
    音色会逐渐漂移；第二段重新起头，反而最像参考音频。用户听起来就是
    「前面都不像，只有最后一句像」。

    这里先按 ``ceil(长度 / max_chars)`` 算出最少需要几段，再围绕每段的
    理想长度找最近的标点；标点离理想位置太远时才硬切。这样既保留句读，
    也不会把最后一段饿成十几个字。
    """
    text = (text or "").strip()
    if not text:
        return []
    max_chars = max(1, int(max_chars or 1))
    total = len(text)
    if total <= max_chars:
        return [text]

    # 段数只取「装得下」的最小值，长度再围绕 total/n 均分。
    # 例如 110 字 / 100 字上限 = 2 段，而不是 92 + 18。
    count = (total + max_chars - 1) // max_chars
    # 标点位置是「切完这个标点之后」的下标；越靠近理想切点的越优先。
    breaks = [
        (i + 1, ch in _SENT_END)
        for i, ch in enumerate(text)
        if ch in _BREAK_END
    ]

    out: list[str] = []
    start = 0
    for k in range(count - 1):
        remaining = count - k - 1
        # 当前段至少留一点；同时保证剩下的字还能塞进剩余段数。
        lower = max(start + 1, total - remaining * max_chars)
        upper = min(start + max_chars, total - remaining)
        if lower > upper:
            # 理论上不会走到这里；真遇到就把剩余部分交给下一轮，
            # 不让一个异常参数把整段生成直接打断。
            break

        # 按「剩余文字的平均长度」取理想切点，而不是一开始就算死。
        ideal = start + (total - start) // (remaining + 1)
        candidates = [
            (pos, strong)
            for pos, strong in breaks
            if lower <= pos <= upper and pos > start
        ]
        if candidates:
            # 最近的标点；距离相同的时候优先句末标点（。！？）。
            pos, _strong = min(
                candidates,
                key=lambda item: (abs(item[0] - ideal), 0 if item[1] else 1),
            )
            # 标点离理想位置太远（例如一句话有 80 字）时，宁可硬切，
            # 也不要为了句读留下一个 80/30 的偏斜分段。
            tolerance = max(4, int(max_chars * 0.2))
            end = pos if abs(pos - ideal) <= tolerance else ideal
        else:
            end = ideal

        end = max(lower, min(upper, end))
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        start = end

    if start < total:
        tail = text[start:].strip()
        if tail:
            out.append(tail)
    return [s for s in out if s]


def _join_segments(segs: list[tuple[np.ndarray, int]], gap_secs: float) -> tuple[np.ndarray, int]:
    """把多段音频拼接成一段，段间插入静音。"""
    if not segs:
        raise RuntimeError("没有可拼接的音频片段。")
    sr = segs[0][1]
    gap_secs = _gap_value(gap_secs)
    gap = np.zeros(int(sr * gap_secs), dtype=np.float32) if gap_secs > 0 else None
    parts: list[np.ndarray] = []
    for i, (wav, _sr) in enumerate(segs):
        if i and gap is not None:
            parts.append(gap)
        parts.append(np.asarray(wav, dtype=np.float32))
    return np.concatenate(parts), sr


def _gap_value(value, default: float | None = None) -> float:
    """把外部传入的段间静音收敛成合理秒数。

    非法值退回默认值；负数和过大值直接夹住。这里不报错：段间静音只影响
    听感，不值得让一整单生成因为一个越界参数失败。
    """
    if default is None:
        default = cfg.SCRIPT_GAP_SECS
    try:
        secs = float(value)
    except (TypeError, ValueError):
        secs = float(default)
    if secs != secs:  # NaN
        secs = float(default)
    return max(0.0, min(10.0, secs))


# ---------------------------------------------------------------------------
# 保存
# ---------------------------------------------------------------------------
def _reserve(tag: str) -> Path:
    """在 outputs/ 里**原子地**抢一个没人占用的文件名。

    ``touch(exist_ok=False)`` 底层是 O_CREAT|O_EXCL，NTFS 上是原子的 ——
    两个线程同时抢同一个名字，只有一个能拿到，另一个拿到 FileExistsError 换下一个。
    原来是「先 while path.exists() 选名字、再 sf.write」，那是典型的查-写竞态：
    两边都可能查到同一个「不存在」的名字，后写的那份把先写的盖掉。
    现在写盘虽然基本还在 GPU 锁后面排队，但这个竞态本身不该靠「碰不到」来成立。
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{tag}_{stamp}"
    n = 0
    while True:
        path = cfg.OUTPUT_DIR / (f"{base}.wav" if n == 0 else f"{base}_{n}.wav")
        try:
            path.touch(exist_ok=False)
            return path
        except FileExistsError:
            n += 1


def save_wav(wav: np.ndarray, sr: int, tag: str) -> str:
    """写入 outputs/，返回文件名（不含目录）。"""
    path = _reserve(tag)
    # 先写 .part<pid>_<tid>.wav 再 os.replace：同卷改名是原子的，
    # 所以「另存的同时有人在读这个文件名」永远读不到半截 wav。
    tmp = path.with_name(f"{path.stem}.part{os.getpid()}_{threading.get_ident()}.wav")
    try:
        sf.write(str(tmp), np.asarray(wav, dtype=np.float32), sr)
        os.replace(tmp, path)
    except Exception:
        for p in (tmp, path):        # 别在 outputs/ 里留 0 字节的坑
            try:
                p.unlink()
            except OSError:
                pass
        raise
    return path.name


def audio_duration(path: str | Path) -> float:
    try:
        info = sf.info(str(path))
        return round(info.frames / info.samplerate, 2)
    except Exception:  # noqa: BLE001
        return 0.0


def _status_line(wav: np.ndarray, sr: int, t_cost: float, extra: str = "") -> str:
    dur = len(wav) / sr if sr else 0.0
    rtf = (t_cost / dur) if dur else 0.0
    head = f"音频 {dur:.1f}s，耗时 {t_cost:.1f}s，RTF {rtf:.2f}"
    return f"{head}\n{extra}" if extra else head


# ---------------------------------------------------------------------------
# 克隆采样稳定性
# ---------------------------------------------------------------------------
def _clone_sampling(p: dict) -> dict:
    """把克隆的采样参数收敛成一组安全的实际值。

    ``stable`` 开启时使用低温度 + 收窄的核采样；关闭时保留内核默认值。
    这里不把参数暴露成任意滑块：用户要的是「像不像」，不是调参实验。
    值仍然会写进合成记录，方便以后复现某一条。
    """
    stable = bool(p.get("stable", cfg.CLONE_STABLE_DEFAULT))
    if not stable:
        return {
            "stable": False,
            "temperature": None,
            "top_p": None,
            "seed": None,
        }
    try:
        seed = int(p.get("seed"))
    except (TypeError, ValueError):
        seed = None
    if seed is None or seed < 0:
        seed = random.randrange(1, 2**31)
    return {
        "stable": True,
        "temperature": float(cfg.CLONE_STABLE_TEMPERATURE),
        "top_p": float(cfg.CLONE_STABLE_TOP_P),
        "seed": seed,
    }


def _clone_chunk_chars(p: dict) -> int:
    """克隆的切段阈值：中等长度不切，超长才按句均衡切开。

    显式传入的 ``chunk_chars`` 仍可用于脚本/调试，但同样不会超过
    ``MAX_TEXT_CHARS``。默认值对所有克隆任务生效，不只在稳定模式生效：
    是否低随机采样与“要不要切段”是两件独立的事。
    """
    try:
        requested = int(p.get("chunk_chars") or 0)
    except (TypeError, ValueError):
        requested = 0
    if requested > 0:
        return min(cfg.MAX_TEXT_CHARS, requested)
    return min(
        cfg.MAX_TEXT_CHARS,
        max(cfg.CLONE_CHUNK_CHARS, int(cfg.profile().script_chunk_chars)),
    )


def _clone_xvec_only(p: dict, voice_default: bool | None = None) -> bool:
    """决定克隆是否只使用 speaker embedding。

    稳定模式开启时强制锁 speaker embedding。前端会同步勾选并禁用那个
    复选框，所以界面上不会出现“稳定开着、实际走完整 ICL”的矛盾。
    关闭稳定模式后，显式传入的 xvec_only 优先，其次才是音色自身设置。
    """
    if bool(p.get("stable", cfg.CLONE_STABLE_DEFAULT)):
        return bool(cfg.CLONE_STABLE_XVEC_ONLY)
    if p.get("xvec_only") is not None:
        return bool(p["xvec_only"])
    if voice_default is not None:
        return bool(voice_default)
    return bool(cfg.CLONE_XVEC_ONLY)


def _record_sampling(
    p: dict, sampling: dict, retried: int = 0, actual: dict | None = None,
) -> dict:
    """把这次**实际生效**的采样参数写回 payload。

    server 的合成记录就是 ``{**p, ...}``，前端结果卡也读同一份字段。
    如果只把参数留在局部变量里，事后看到「这条不像」却不知道当时用的
    seed / 温度，所谓「可复现」就只是句空话。
    """
    rec = {
        "stable": bool(sampling["stable"]),
        "temperature": sampling["temperature"],
        "top_p": sampling["top_p"],
        "seed": sampling["seed"],
        "drift_retries": int(retried),
    }
    if actual:
        # xvec_only / chunk_chars / gap_secs 也要记录实际值：前端可能传了
        # false，但稳定模式会强制走 speaker embedding；只记原始请求会误导。
        rec.update(actual)
    p.update(rec)
    return rec


@contextmanager
def _seeded_rng(seed: int | None):
    """在生成期间固定 torch / Python 的随机状态，结束后原样恢复。

    内核没有 ``seed`` 形参，采样直接走全局 torch RNG。这里保存并恢复
    进程级状态，既让单次任务可复现，也不污染同一服务进程里的其他任务。
    """
    if seed is None:
        yield
        return
    py_state = random.getstate()
    np_state = np.random.get_state()
    cpu_state = torch.random.get_rng_state()
    cuda_states = torch.cuda.get_rng_state_all() if torch.cuda.is_available() else None
    try:
        random.seed(seed)
        np.random.seed(seed % (2**32))
        torch.manual_seed(seed)
        if cuda_states is not None:
            torch.cuda.manual_seed_all(seed)
        yield
    finally:
        random.setstate(py_state)
        np.random.set_state(np_state)
        torch.random.set_rng_state(cpu_state)
        if cuda_states is not None:
            torch.cuda.set_rng_state_all(cuda_states)


_FEATURE_SR = 16000
_SPEAKER_SR = 24000


def _resample_for_features(wav: np.ndarray, sr: int) -> tuple[np.ndarray, int]:
    """把参考音频和生成音频统一到 16 kHz 再比较。

    参考音频常见 44.1/48 kHz，生成音频是 24 kHz。直接比频谱平坦度会
    把「有效带宽不同」误当成「音色变了」：实测同一段参考从 44.1 kHz
    降到 24 kHz，平坦度就从 0.0056 变成 0.017。统一采样率后这个变量消失。
    """
    if sr == _FEATURE_SR:
        return np.asarray(wav, dtype=np.float32), sr
    from scipy.signal import resample_poly

    g = np.gcd(int(sr), _FEATURE_SR)
    up, down = _FEATURE_SR // g, int(sr) // g
    y = resample_poly(np.asarray(wav, dtype=np.float32), up, down)
    return np.asarray(y, dtype=np.float32), _FEATURE_SR


def _audio_features(wav: np.ndarray, sr: int) -> dict | None:
    """提取用于判断「整段是否明显变嗓」的轻量声学特征。

    只用 numpy，避免为了一个检查引入 librosa 这种重依赖。特征刻意选得
    很粗：目标是抓「113 Hz / 2084 Hz 这种整段跑偏」，不是做说话人识别。
    """
    try:
        x = np.asarray(wav, dtype=np.float64).reshape(-1)
        if x.size == 0 or sr <= 0:
            return None
        x, sr = _resample_for_features(x, int(sr))
        x = np.asarray(x, dtype=np.float64).reshape(-1)
        # 掐掉首尾低能量部分，避免长静音把统计稀释
        hop = max(1, int(sr * 0.02))
        n = len(x) // hop
        if n < 5:
            return None
        frames = x[: n * hop].reshape(n, hop)
        rms = np.sqrt(np.mean(frames * frames, axis=1))
        floor = max(1e-6, float(np.percentile(rms, 10)))
        voiced = frames[rms > floor * 2.0]
        if len(voiced) < 5:
            voiced = frames
        y = voiced.reshape(-1)
        if y.size < max(256, int(sr * 0.2)):
            return None

        # 频谱质心 / 平坦度：用 512 点 FFT，20 ms 帧、10 ms 跳
        fft = 512
        win = np.hanning(fft).astype(np.float64)
        step = max(1, fft // 2)
        starts = range(0, max(0, len(y) - fft + 1), step)
        cent, flat = [], []
        freqs = np.fft.rfftfreq(fft, 1.0 / sr)
        for i in starts:
            spec = np.abs(np.fft.rfft(y[i:i + fft] * win))
            total = float(np.sum(spec))
            if total <= 1e-12:
                continue
            cent.append(float(np.sum(freqs * spec) / total))
            p = spec / total
            flat.append(float(np.exp(np.mean(np.log(np.maximum(p, 1e-12))))
                              / np.mean(p)))
        if not cent:
            return None

        # 基频：自相关找一个 70–400 Hz 的主周期。只保留有声帧，
        # 再取中位数；个别八度错误不会改变整段的结论。
        f0 = []
        lo = max(2, int(sr / 400))
        hi = min(len(y) - 1, int(sr / 70))
        for i in starts:
            seg = y[i:i + fft]
            if len(seg) < fft:
                continue
            seg = seg - float(np.mean(seg))
            corr = np.correlate(seg, seg, mode="full")[len(seg) - 1:]
            if corr[0] <= 1e-12:
                continue
            lag = lo + int(np.argmax(corr[lo:hi + 1]))
            if corr[lag] / corr[0] >= 0.3:
                f0.append(sr / lag)
        return {
            "centroid": float(np.median(cent)),
            "flatness": float(np.median(flat)),
            "f0": float(np.median(f0)) if f0 else 0.0,
        }
    except Exception:  # noqa: BLE001  检查失败不应该让生成失败
        return None


def _drift_score(ref: dict | None, got: dict | None) -> float:
    """返回相对参考音频的最大偏离倍数，1.0 表示完全相同。

    频谱平坦度是噪声/气声的代理。统一到 16 kHz 后，实测参考约 0.067，
    正常新样本约 0.105–0.124，旧坏段约 0.278；三项中任意一项超限就重试。
    """
    if not ref or not got:
        return 0.0

    def ratio(a: float, b: float) -> float:
        if a <= 1e-9 or b <= 1e-9:
            return 1.0
        return max(a / b, b / a)

    return max(
        ratio(ref["centroid"], got["centroid"]) / cfg.CLONE_DRIFT_CENTROID_RATIO,
        ratio(ref["flatness"], got["flatness"]) / cfg.CLONE_DRIFT_FLATNESS_RATIO,
        ratio(ref["f0"], got["f0"]) / cfg.CLONE_DRIFT_F0_RATIO,
    )


def _drifted(ref: dict | None, got: dict | None) -> bool:
    """判断一段音频是否明显偏离参考音频。"""
    return _drift_score(ref, got) > 1.0


def _resample_audio(
    wav: np.ndarray, sr: int, target_sr: int,
) -> np.ndarray:
    """把单声道音频重采样到目标采样率。"""
    x = np.asarray(wav, dtype=np.float32).reshape(-1)
    sr = int(sr)
    if sr <= 0:
        raise ValueError("音频采样率无效。")
    if sr == int(target_sr):
        return np.ascontiguousarray(x, dtype=np.float32)
    from scipy.signal import resample_poly

    g = np.gcd(sr, int(target_sr))
    up, down = int(target_sr) // g, sr // g
    y = resample_poly(x, up, down)
    return np.ascontiguousarray(y, dtype=np.float32)


def _normalize_embedding(embedding) -> np.ndarray:
    """把 speaker embedding 转成单位向量，避免长度影响 cosine。"""
    emb = np.asarray(embedding, dtype=np.float32).reshape(-1)
    if emb.size == 0:
        raise ValueError("speaker embedding 为空。")
    norm = float(np.linalg.norm(emb))
    if not np.isfinite(norm) or norm <= 1e-9:
        raise ValueError("speaker embedding 范数为零。")
    return np.ascontiguousarray(emb / norm, dtype=np.float32)


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    """两个已归一化向量的 cosine；形状不一致时返回 0。"""
    aa = np.asarray(a, dtype=np.float32).reshape(-1)
    bb = np.asarray(b, dtype=np.float32).reshape(-1)
    if aa.size == 0 or aa.shape != bb.shape:
        return 0.0
    return float(np.clip(np.dot(aa, bb), -1.0, 1.0))


def _speaker_windows(
    wav: np.ndarray, sr: int,
    window_secs: float | None = None,
    min_window_secs: float | None = None,
) -> list[np.ndarray]:
    """按目标窗长把音频均衡切开，避免留下过短尾巴。"""
    x = np.asarray(wav, dtype=np.float32).reshape(-1)
    sr = int(sr)
    if x.size == 0 or sr <= 0:
        return []
    window = max(1.0, float(window_secs or cfg.CLONE_SPK_WINDOW_SECS))
    min_window = max(1.0, float(min_window_secs or cfg.CLONE_SPK_MIN_WINDOW_SECS))
    duration = len(x) / sr
    if duration <= window:
        return [x]
    count = int(np.ceil(duration / window))
    if duration / count < min_window:
        count = max(1, int(duration // min_window))
    # 单段最多 600 字，正常不会到这里；留一道护栏防止异常输入拖垮检查。
    count = min(count, 64)
    bounds = np.linspace(0, len(x), count + 1, dtype=np.int64)
    return [x[int(a):int(b)] for a, b in zip(bounds[:-1], bounds[1:]) if b > a]


def _speaker_embedding(model, wav: np.ndarray, sr: int) -> np.ndarray:
    """用内核自己的 speaker encoder 提取并归一化音色向量。"""
    x = _resample_audio(wav, sr, _SPEAKER_SR)
    inner = getattr(model, "model", model)
    inner = getattr(inner, "model", inner)
    extract = getattr(inner, "extract_speaker_embedding", None)
    if extract is None:
        raise RuntimeError("当前内核不支持 extract_speaker_embedding。")
    emb = extract(np.ascontiguousarray(x, dtype=np.float32), _SPEAKER_SR)
    if hasattr(emb, "detach"):
        emb = emb.detach().float().cpu().numpy()
    return _normalize_embedding(emb)


def _speaker_profile(
    model, wav: np.ndarray, sr: int, ref_embedding: np.ndarray,
) -> dict | None:
    """返回整段 cosine、窗口最低 cosine 和整段向量。"""
    try:
        full = _speaker_embedding(model, wav, sr)
        windows = _speaker_windows(wav, sr)
        if len(windows) == 1:
            window_embs = [full]
        else:
            window_embs = [_speaker_embedding(model, w, sr) for w in windows]
        scores = [_cosine(ref_embedding, emb) for emb in window_embs]
        if not scores:
            return None
        return {
            "embedding": full,
            "cosine": _cosine(ref_embedding, full),
            "min": float(min(scores)),
        }
    except Exception:  # noqa: BLE001  评分失败只退回频谱检查，不让生成失败
        return None


def _speaker_candidate_key(
    profile: dict | None, previous: np.ndarray | None,
) -> tuple:
    """候选排序键：先满足硬下限，再优化“参考相似度 / 段间连贯度”的较差项。"""
    if not profile:
        return (False, -1.0, -1.0, -1.0, -1.0)
    ref_min = float(profile.get("min") or 0.0)
    chain = (
        _cosine(profile["embedding"], previous)
        if previous is not None else None
    )
    valid = ref_min >= cfg.CLONE_SPK_MIN_COS
    if chain is not None:
        valid = valid and chain >= cfg.CLONE_SPK_MIN_COS
        worst = min(ref_min, chain)
        average = (ref_min + chain) / 2.0
    else:
        worst = ref_min
        average = ref_min
    return (
        bool(valid), worst, average, ref_min,
        -1.0 if chain is None else chain,
    )


def _needs_speaker_candidate(
    profile: dict | None, previous: np.ndarray | None,
) -> bool:
    """首条低于目标，或与上一段不够连贯时，才多生成一条候选。"""
    if not profile:
        return False
    if float(profile.get("min") or 0.0) < cfg.CLONE_SPK_TARGET_COS:
        return True
    if previous is not None:
        chain = _cosine(profile["embedding"], previous)
        return chain < cfg.CLONE_SPK_CHAIN_COS
    return False


# ---------------------------------------------------------------------------
# 四个生成入口
# ---------------------------------------------------------------------------
def _gen_clone(
    text: str,
    language: str,
    ref_audio: str | Path,
    ref_text: str,
    instruct: str | None,
    size: str,
    xvec_only: bool,
    warm_prompt: Any = None,
    temperature: float | None = None,
    top_p: float | None = None,
    seed: int | None = None,
) -> tuple[np.ndarray, int]:
    model = load_model("clone", size)
    if warm_prompt is not None:
        _prime_prompt_cache(model, ref_audio, ref_text, xvec_only, warm_prompt, size)
    kwargs: dict[str, Any] = {}
    if temperature is not None:
        kwargs["temperature"] = float(temperature)
    if top_p is not None:
        kwargs["top_p"] = float(top_p)
    with _seeded_rng(seed):
        wavs, sr = model.generate_voice_clone(
            text=text,
            language=language,
            ref_audio=str(ref_audio),
            ref_text=ref_text or "",
            instruct=(instruct or None),
            xvec_only=bool(xvec_only),
            **kwargs,
        )
    return np.asarray(wavs[0], dtype=np.float32), int(sr)


def _prime_prompt_cache(
    model, ref_audio: str | Path, ref_text: str, xvec_only: bool, payload: dict,
    size: str = "",
) -> None:
    """把音色基因（.pt）预热进内核的 voice prompt 缓存。

    这是「音色固化」真正生效的地方 —— 原项目把 .pt 写出来却从没读回去过，
    于是每次生成都要重新编码一遍参考音频。这里直接命中内核缓存，
    省掉重采样 + speaker 编码。

    只处理 ``xvec_only=True``：完整 ICL 模式的缓存项还含一份「参考文本的
    tokenize 结果」，那份东西重算不过毫秒级，且构造错了反而会让生成出错，
    不如交给内核自己算。

    代价是依赖内核私有属性 ``_voice_prompt_cache``；因此全程 try/except，
    任何不匹配都静默退回「从 wav 现场编码」的正常路径，不会让生成失败。
    """
    try:
        if not xvec_only:
            return
        # 说话人向量的维度跟着权重规格走：1.7B → 2048，0.6B → 1024。
        # 基因是「用某套权重抽出来的」，拿 1.7B 的喂 0.6B，会在 prefill 拼张量时
        # 直接炸（Expected size 1024 but got size 2048）。规格对不上就别预热 ——
        # 退回现场编码，慢一点但结果正确。
        # 优先信基因里记的 model_size；早期基因没这个字段，就按向量实际长度判。
        want_dim = {"1.7B": 2048, "0.6B": 1024}.get(str(size or ""))
        items = payload.get("items") or []
        emb = items[0].get("ref_spk_embedding") if items else None
        got_dim = len(emb) if emb is not None else 0
        if want_dim and got_dim and got_dim != want_dim:
            return
        cache = getattr(model, "_voice_prompt_cache", None)
        if cache is None:
            return
        if not items:
            return
        key = (str(ref_audio), ref_text or "", True, True)  # 末位是 append_silence
        cache[key] = (
            dict(
                ref_code=[None],
                ref_spk_embedding=[items[0]["ref_spk_embedding"]],
                x_vector_only_mode=[True],
                icl_mode=[False],
            ),
            [None],
        )
    except Exception:  # noqa: BLE001  预热失败不影响正确性
        pass


def _gen_design(text: str, language: str, instruct: str) -> tuple[np.ndarray, int]:
    model = load_model("design", "1.7B")
    wavs, sr = model.generate_voice_design(
        text=text,
        language=language,
        instruct=instruct or "",
    )
    return np.asarray(wavs[0], dtype=np.float32), int(sr)


def _gen_official(
    text: str, language: str, speaker: str, instruct: str | None, size: str
) -> tuple[np.ndarray, int]:
    model = load_model("official", size)
    wavs, sr = model.generate_custom_voice(
        text=text,
        language=language,
        speaker=(speaker or "vivian").lower(),
        instruct=(instruct or None),
    )
    return np.asarray(wavs[0], dtype=np.float32), int(sr)


def _is_official_dialog(text: str) -> bool:
    """行首都是内置说话人的 ``[角色]`` 时，按多人对话处理。

    流式也要问同一个问题：多人对话是一行一换说话人、逐行生成再拼接的，
    没有办法"边生成边播"。
    """
    import script as script_mod  # 延迟导入：纯解析层，无副作用

    lines = script_mod.parse(text)
    st = script_mod.stats(lines)
    return (
        st["ok_lines"] > 0
        and st["bad_lines"] == 0
        and all((ln["role"] or "").lower() in cfg.SPEAKER_NAMES for ln in lines if ln["ok"])
    )


def gen_official_text(
    text: str, language: str, speaker: str, instruct: str | None, size: str,
    on_progress=None, gap_secs: float | None = None,
) -> tuple[np.ndarray, int, str]:
    """官方精品：单人或多人对话。

    行首带 ``[角色]``（且角色都是内置说话人之一）时按多人对话逐行生成再拼接；
    否则整体交给模型一次生成 —— 这样用户直接贴一段带方括号的普通文本
    也不会被误判成对话。

    **单人也会分段**：说话人和 instruct 都是固定的，切段只影响段间的韵律衔接，
    不会换嗓子 —— 这和声音设计正相反（那边每段都是新嗓子，绝不能切）。
    分成段之后这个模式也就不再有字数的硬上限。
    """
    gap = _gap_value(gap_secs)
    is_dialog = _is_official_dialog(text)
    prof = cfg.profile()

    if not is_dialog:
        pieces = split_text(text, prof.script_chunk_chars)
        metas = [
            {
                "role": (speaker or "vivian").lower(),
                "line": None,
                "line_id": None,
                "index": i + 1,
                "total": len(pieces),
                "text": piece[:80],
                "mode": "official",
            }
            for i, piece in enumerate(pieces)
        ]
        wav, sr = _run_pieces(
            pieces,
            lambda piece, _i: _gen_official(piece, language, speaker, instruct, size),
            gap,
            on_progress,
            metas,
        )
        return wav, sr, ""

    if size == "0.6B":
        raise RuntimeError(
            "0.6B 规格的官方精品不支持情感指令（内核会把 instruct 强制置空），"
            "多人对话会退化成无情绪的朗读。请切到 1.7B，或去掉行首的 [情感指令]。"
        )

    import script as script_mod

    lines = script_mod.parse(text)
    st = script_mod.stats(lines)

    # 先把「一行切成几段」全部算出来，才有分母报进度 ——
    # 边生成边数的话，百分比永远算不出来（分母在最后一刻才确定）。
    jobs: list[tuple[str, str, str | None, int, str]] = []
    for ln in lines:
        if (not ln.get("ok") or ln.get("disabled")
                or ln.get("is_scene") or ln.get("ignored")):
            continue
        for piece in split_text(ln["content"], prof.script_chunk_chars):
            jobs.append((
                ln["role"].lower(), piece, ln.get("instruct") or None,
                ln.get("index") or 0, ln.get("line_id") or ln.get("id") or "",
            ))

    total = len(jobs)
    first_meta = None
    if jobs:
        first_meta = {
            "role": jobs[0][0], "line": jobs[0][3], "line_id": jobs[0][4],
            "index": 1, "total": total, "text": jobs[0][1][:80], "mode": "official",
        }
    _notify(on_progress, 0, total, "load", first_meta)
    segs: list[tuple[np.ndarray, int]] = []
    for i, (role, piece, inst, line_no, line_id) in enumerate(jobs, 1):
        wav, sr = _gen_official(piece, language, role, inst, size)
        segs.append((wav, sr))
        _notify(on_progress, i, total, "gen", {
            "role": role, "line": line_no, "line_id": line_id,
            "index": i, "total": total, "text": piece[:80], "mode": "official",
        })
    wav, sr = _join_segments(segs, gap)
    return wav, sr, f"多人对话：{st['ok_lines']} 句 / {st['role_count']} 个角色"


def gen_script(
    script_text: str, language: str, size: str, on_progress=None,
    gap_secs: float | None = None,
) -> tuple[np.ndarray, int, str, list[str]]:
    """剧本工坊：用 voices/ 里已登记的音色逐句合成。

    返回 (wav, sr, extra_note, warnings)。
    """
    import script as script_mod

    lines = script_mod.parse(script_text)
    if not lines:
        raise RuntimeError("剧本内容为空。")

    known = set(voices_mod.list_voices())
    warns = script_mod.validate(lines, known)
    prof = cfg.profile()
    gap = _gap_value(gap_secs)

    # 同上：先把要念的段全部摊平，分母稳定了进度才有意义。
    jobs: list[tuple[dict, str, str | None, bool, dict]] = []
    used: list[str] = []
    for ln in lines:
        if (not ln.get("ok") or ln.get("disabled")
                or ln.get("is_scene") or ln.get("ignored")
                or ln.get("role") not in known):
            continue  # 具体原因已在 warns 里说明
        v = voices_mod.load_voice(ln["role"])
        if v is None:
            continue
        first = True
        for piece in split_text(ln["content"], prof.script_chunk_chars):
            jobs.append((
                v, piece, ln.get("instruct") or None, first,
                {
                    "role": ln["role"],
                    "line": ln.get("index"),
                    "line_id": ln.get("line_id") or ln.get("id"),
                    "scene": ln.get("scene") or "",
                    "text": piece[:80],
                    "mode": "script",
                },
            ))
            first = False
        used.append(ln["role"])

    total = len(jobs)
    first_meta = None
    if jobs:
        first_meta = {**jobs[0][4], "index": 1, "total": total}
    _notify(on_progress, 0, total, "load", first_meta)
    segs: list[tuple[np.ndarray, int]] = []
    for i, (v, piece, inst, first, meta) in enumerate(jobs, 1):
        wav, sr = _gen_clone(
            piece,
            language,
            v["audio_path"],
            v.get("ref_text", ""),
            inst,
            size,
            v.get("xvec_only", cfg.CLONE_XVEC_ONLY),
            # 基因只预热一次；同一音色后续片段直接命中内核缓存
            v.get("prompt") if first else None,
        )
        segs.append((wav, sr))
        _notify(on_progress, i, total, "gen", {**meta, "index": i, "total": total})

    if not segs:
        raise RuntimeError("没有生成任何音频。请检查每行是否都写成 `[角色名] 台词`。")

    wav, sr = _join_segments(segs, gap)
    note = (f"剧本：{len(segs)} 段 / 角色 {len(set(used))} 个"
            f" / 段间 {gap:.2f}s")
    return wav, sr, note, warns


def speaker_preview_path(key: str, size: str) -> Path:
    """试听样例的落盘位置。规格进文件名 —— 1.7B 和 0.6B 是两把嗓子。"""
    return cfg.PREVIEW_DIR / f"{key}__{size}.wav"


def preview_script_line(
    text: str,
    language: str = "Auto",
    voice: str | None = None,
    speaker: str | None = None,
    instruct: str | None = None,
    size: str | None = None,
    xvec_only: bool | None = None,
) -> tuple[np.ndarray, int]:
    """生成一句剧本台词，供单句试听接口复用。

    只走一次 ``_gen_clone`` / ``_gen_official``，不切段、不拼整篇；
    调用方必须自己持有 ``_GPU_LOCK``（与 :func:`synthesize` 的分工一致），
    这样 server 可以把落盘也放进同一段临界区。
    """
    text = (text or "").strip()
    if not text:
        raise RuntimeError("请先输入要试听的台词。")
    size = cfg.resolve_size(size)
    language = language or "Auto"
    inst = (instruct or "").strip() or None
    if voice:
        v = voices_mod.load_voice(voice)
        if v is None:
            raise RuntimeError(f"音色库里没有 [{voice}]。")
        xv = bool(v.get("xvec_only", cfg.CLONE_XVEC_ONLY)) if xvec_only is None else bool(xvec_only)
        return _gen_clone(
            text, language, v["audio_path"], v.get("ref_text", ""),
            inst, size, xv, v.get("prompt"),
        )
    if speaker:
        key = (speaker or "").lower()
        if key not in cfg.SPEAKER_NAMES:
            raise RuntimeError(f"没有这个官方说话人：{speaker}")
        return _gen_official(text, language, key, inst, size)
    raise RuntimeError("请提供 voice（音色库名字）或 speaker（官方说话人）。")


def build_speaker_preview(key: str, size: str) -> Path:
    """生成（或复用）官方说话人的试听样例。

    生成一次就长期躺在 web/_preview/ 里：试听不该每次都等模型加载（十几到几十秒），
    但也不该在启动时把 9 个说话人全跑一遍 —— 那要一两分钟，还平白占着显存。
    """
    path = speaker_preview_path(key, size)
    if path.exists():
        return path
    lang = cfg.SPEAKER_LANG.get(key)
    text = cfg.SPEAKER_PREVIEW_TEXT.get(lang) if lang else None
    if not text:
        raise RuntimeError(f"没有为说话人 [{key}] 准备试听文本，无法生成。")
    # 必须和 synthesize 抢同一把锁：试听是「点一下就来一次」，用户很容易连着点
    # 好几个说话人，而模型的 CUDA graph 与输入 buffer 是共享的 ——
    # 两路生成同时进去会互相踩，存下来的音频张冠李戴（vivian 会变成别人的嗓子）。
    with _GPU_LOCK:
        if path.exists():       # 排队等锁的这会儿，别人可能已经把它生成好了
            return path
        wav, sr, _ = gen_official_text(text, lang, key, None, size)
        sf.write(str(path), np.asarray(wav, dtype=np.float32), sr)
    return path


# ---------------------------------------------------------------------------
# 统一入口（server.py 只调这个）
# ---------------------------------------------------------------------------
def synthesize(p: dict, on_progress=None) -> tuple[np.ndarray, int, str, str, dict]:
    """按参数生成一段音频。

    返回 ``(waveform, sample_rate, status, level, pp_stats)``；``level`` ∈ ok/warn/error，
    ``pp_stats`` 是后处理的统计（关掉时 ``{"applied": False}``），供合成记录与结果卡用。
    ``on_progress(done, total, stage)`` 可选，逐段报进度（见 :func:`_notify`）。
    ``p`` 的字段：
        mode        clone | design | official | script
        text        文本
        language    语言（Auto / Chinese / …）
        size        1.7B | 0.6B | auto
        instruct    情感/风格指令（自由文本）
        voice       音色库名字（mode=clone 时二选一）
        ref_audio   参考音频绝对路径（mode=clone 时与 voice 二选一）
        ref_text    参考音频文本（xvec_only=False 时必填）
        speaker     官方说话人（mode=official）
        xvec_only   是否只用说话人向量（仅 mode=clone 有效）
        gap_secs    段间静音（仅 script / 多人对话）
        pp_*        输出后处理开关，见 app/postproc.py
    """
    mode = (p.get("mode") or "clone").strip().lower()
    text = (p.get("text") or "").strip()
    if not text:
        raise RuntimeError("请先输入要合成的文本。")
    # 长度闸只留给**不能切**的模式。clone / official / script 都在内部按句切段，
    # 多长都行（真正的天花板是 _synth_clone 里那道内存护栏）。
    # design 不能切：声音设计的每一段都是一把新嗓子（上游没有固定 seed），
    # 切开再拼起来会变成两把不同的嗓子念同一段话 —— 比拒绝生成更糟。
    if mode == "design" and len(text) > cfg.MAX_TEXT_CHARS:
        raise RuntimeError(
            f"声音设计的文本过长（{len(text)} 字，上限 {cfg.MAX_TEXT_CHARS}）。"
            "这个模式不能分段（每段都会换成另一把嗓子），请缩短要朗读的文本，"
            "或改用语音克隆 / 官方精品。"
        )

    language = p.get("language") or "Auto"
    prof = cfg.profile()
    size = cfg.resolve_size(p.get("size"))
    instruct = (p.get("instruct") or "").strip() or None
    gap = _gap_value(
        p.get("gap_secs"),
        cfg.CLONE_GAP_SECS if mode == "clone" else cfg.SCRIPT_GAP_SECS,
    )

    # 同一时刻只允许一次生成：GPU 上并行会互相踩显存
    with _GPU_LOCK:
        t0 = time.time()
        try:
            if mode == "design":
                _notify(on_progress, 0, 1, "load")
                wav, sr = _gen_design(text, language, instruct or "")
                _notify(on_progress, 1, 1, "gen")
                extra = ""   # 模式名参数区已经写了，不必再提示一遍
            elif mode == "official":
                wav, sr, extra = gen_official_text(text, language,
                                                   p.get("speaker") or "vivian",
                                                   instruct, size, on_progress, gap)
            elif mode == "script":
                wav, sr, extra, warns = gen_script(
                    text, language, size, on_progress, gap
                )
                extra = extra + ("\n" + "\n".join(warns) if warns else "")
            elif mode == "clone":
                wav, sr, extra = _synth_clone(
                    p, language, instruct, size, on_progress, gap
                )
            else:
                raise RuntimeError(f"未知的生成模式：{mode}")
        except Exception as e:  # noqa: BLE001
            # 第一层降级：清缓存后原样重试一次（碎片导致的 OOM 常能救回来）
            if _is_oom(e):
                _MGR._unload_locked()
                try:
                    if mode == "clone":
                        wav, sr, extra = _synth_clone(
                            p, language, instruct, size, on_progress, gap
                        )
                    elif mode == "official":
                        wav, sr, extra = gen_official_text(
                            text, language, p.get("speaker") or "vivian", instruct, size,
                            on_progress, gap)
                    elif mode == "design":
                        raise
                    elif mode == "script":
                        wav, sr, extra, _ = gen_script(
                            text, language, size, on_progress, gap
                        )
                    else:
                        raise
                    level = "warn"
                    extra = (extra or "") + "\n已释放显存后重试成功。"
                except Exception as e2:  # noqa: BLE001
                    return (np.zeros(1, dtype=np.float32), 24000,
                            _friendly_error(e2), "error", {"applied": False})
            else:
                return (np.zeros(1, dtype=np.float32), 24000,
                        _friendly_error(e), "error", {"applied": False})
        else:
            level = "ok"

    # 后处理放在锁**外**：纯 CPU、几毫秒，没必要占着 GPU 锁 ——
    # 那几十毫秒里另一路生成本来可以开始加载权重了。
    pp_stats: dict = {"applied": False}
    if level != "error" and len(wav) > 1:
        wav, pp_stats = postproc.apply(wav, sr, postproc.options(p))
        note = postproc.summary(pp_stats)
        # 只报「动了什么」，不报设置 —— 设置已经在结果卡参数表里了。
        # 这一行的价值在于：事后看到「剪掉 1.2s」能解释「为什么这条比上次短」。
        if pp_stats.get("applied") and note:
            extra = "\n".join(x for x in (extra, f"后处理：{note}") if x)

    status = _status_line(wav, sr, time.time() - t0, extra)
    return wav, sr, status, level, pp_stats


def _resolve_clone_source(p: dict) -> tuple[str, str, bool, Any]:
    """从 payload 解出参考音频 / 参考文本 / 是否只用向量 / 音色基因。

    非流式与流式共用这一份判断。两条路如果各自解析，同一个音色可能在
    「直接播」和「边生成边播」下给出两把嗓子 —— 用户会以为是模型不稳定。
    """
    voice_name = (p.get("voice") or "").strip()
    warm = None
    voice_default = None
    if voice_name:
        v = voices_mod.load_voice(voice_name)
        if v is None:
            raise RuntimeError(f"音色库里没有 [{voice_name}]。")
        ref_audio, ref_text = v["audio_path"], v.get("ref_text", "")
        voice_default = v.get("xvec_only", cfg.CLONE_XVEC_ONLY)
        warm = v.get("prompt")
    else:
        ref_audio = (p.get("ref_audio") or "").strip()
        if not ref_audio:
            raise RuntimeError("请选择一个音色，或上传参考音频。")
        ref_text = (p.get("ref_text") or "").strip()
        voice_default = bool(p.get("xvec_only", cfg.CLONE_XVEC_ONLY))

    xvec_only = _clone_xvec_only(p, voice_default)

    if not xvec_only and not ref_text:
        raise RuntimeError(
            "关闭了「仅用说话人向量」时必须填写参考音频的文本，"
            "否则完整 ICL 模式无法对齐。请补上参考文本，或改回仅用向量。"
        )
    return ref_audio, ref_text, bool(xvec_only), warm


def _synth_clone(
    p: dict, language: str, instruct: str | None, size: str, on_progress=None,
    gap_secs: float | None = None,
) -> tuple[np.ndarray, int, str]:
    """语音克隆：音色库优先，其次用上传的参考音频。

    **不限字数**：按克隆专用阈值均衡切段后逐段生成再拼接，
    所以这里唯一的上限是「内存装不下」—— 由 ``cfg.MAX_AUDIO_SECONDS`` 兜住。
    """
    # 状态行的第二行。结果卡的「本次参数」已经把音色名/临时上传列出来了，
    # 这里再报一遍只是噪音 —— 这个口子只留给「参数区看不到」的增量信息。
    extra = ""
    ref_audio, ref_text, xvec_only, warm = _resolve_clone_source(p)
    sampling = _clone_sampling(p)

    text = (p.get("text") or "").strip()

    # 内存护栏（不是体验限制）：拼接时每一段都还在内存里，峰值约为成品的两倍。
    # 24 kHz float32 = 96 KB/s，四小时音频光成品就 1.4 GB —— 再往上必 OOM，
    # 而且是跑到最后拼接那一步才炸，前面几十分钟全白跑。宁可开头就拒绝。
    est_sec = len(text) / max(1.0, cfg.CHARS_PER_SEC)
    if est_sec > cfg.MAX_AUDIO_SECONDS:
        raise RuntimeError(
            f"这段文本念出来约 {est_sec / 3600:.1f} 小时，超出单次能装下的内存"
            f"（约 {cfg.MAX_AUDIO_SECONDS / 3600:.0f} 小时音频）。"
            "请拆成几次生成 —— 合并音频比重新生成一遍便宜得多。"
        )

    chunk_chars = _clone_chunk_chars(p)
    pieces = split_text(text, chunk_chars)

    # 参考音频只分析一次；每一段复用同一份基准。
    ref_feat = None
    ref_wav = ref_sr = None
    if sampling["stable"] and (
        cfg.CLONE_DRIFT_ENABLED or cfg.CLONE_SPK_SCORE_ENABLED
    ):
        try:
            ref_wav, ref_sr = sf.read(str(ref_audio), dtype="float32",
                                     always_2d=False)
            if getattr(ref_wav, "ndim", 1) > 1:
                ref_wav = ref_wav.mean(axis=1)
            ref_sr = int(ref_sr)
            if cfg.CLONE_DRIFT_ENABLED:
                ref_feat = _audio_features(ref_wav, ref_sr)
        except Exception:  # noqa: BLE001  读不到参考音频时只跳过检查
            ref_wav = ref_sr = None
            ref_feat = None

    retried = 0
    candidate_takes = 0
    speaker_extra = 0
    spk_model = None
    ref_spk = None
    previous_spk = None
    speaker_scores: list[dict] = []
    speaker_chain: list[float] = []

    def score_speaker(wav: np.ndarray, sr: int) -> dict | None:
        """延迟加载 speaker encoder；读不到参考音频时安全跳过。"""
        nonlocal spk_model, ref_spk
        if (not sampling["stable"] or not cfg.CLONE_SPK_SCORE_ENABLED
                or ref_wav is None or ref_sr is None):
            return None
        try:
            if spk_model is None:
                spk_model = load_model("clone", size)
                ref_spk = _speaker_embedding(spk_model, ref_wav, ref_sr)
            return _speaker_profile(spk_model, wav, sr, ref_spk)
        except Exception:  # noqa: BLE001  评分失败退回频谱检查
            return None

    def take(piece: str, i: int, seed: int | None) -> tuple[
        np.ndarray, int, dict | None, dict | None
    ]:
        nonlocal candidate_takes
        wav_i, sr_i = _gen_clone(
            piece, language, ref_audio, ref_text, instruct, size, xvec_only,
            warm if i == 0 and candidate_takes == 0 else None,
            sampling["temperature"], sampling["top_p"], seed,
        )
        candidate_takes += 1
        return (
            wav_i, sr_i,
            _audio_features(wav_i, sr_i) if ref_feat is not None else None,
            score_speaker(wav_i, sr_i),
        )

    def gen_one(piece: str, i: int) -> tuple[np.ndarray, int]:
        nonlocal retried, previous_spk, speaker_extra
        # 音色基因只预热一次；之后的段直接命中内核缓存
        # 所有段共用同一个主 seed。以前按段号派生 seed，等于每段都从另一条
        # 随机轨迹起音；即使 speaker embedding 相同，韵律也会更容易分叉。
        seg_seed = sampling["seed"]
        wav, sr, feat, spk = take(piece, i, seg_seed)

        # 第一层：真实 speaker encoder 择优。只有首条低于目标或与上一段
        # 不够连贯时才多跑一条，正常段仍然只生成一次。
        if (spk is not None and cfg.CLONE_SPK_CANDIDATES > 1
                and _needs_speaker_candidate(spk, previous_spk)):
            pool = [(wav, sr, feat, spk)]
            for k in range(1, int(cfg.CLONE_SPK_CANDIDATES)):
                speaker_extra += 1
                extra_seed = (
                    (seg_seed + 104729 * (k + 1)) % (2**31)
                    if seg_seed is not None else None
                )
                pool.append(take(piece, i, extra_seed))
            wav, sr, feat, spk = max(
                pool, key=lambda x: _speaker_candidate_key(x[3], previous_spk)
            )

        # 第二层：频谱/基频兜底。speaker encoder 不可用，或择优后仍明显
        # 漂移时，沿用原来的换 seed 重生成逻辑。
        retries = 0
        while (ref_feat is not None and feat is not None
               and _drifted(ref_feat, feat)
               and retries < cfg.CLONE_DRIFT_MAX_RETRIES):
            retries += 1
            retried += 1
            retry_seed = (
                (seg_seed + 104729 * (retries + 10)) % (2**31)
                if seg_seed is not None else None
            )
            wav2, sr2, feat2, spk2 = take(piece, i, retry_seed)
            if spk is not None and spk2 is not None:
                better = (
                    _speaker_candidate_key(spk2, previous_spk)
                    > _speaker_candidate_key(spk, previous_spk)
                )
            else:
                better = (
                    feat2 is not None and (
                        feat is None
                        or _drift_score(ref_feat, feat2)
                        < _drift_score(ref_feat, feat)
                    )
                )
            if better:
                wav, sr, feat, spk = wav2, sr2, feat2, spk2

        if spk is not None:
            speaker_scores.append(spk)
            if previous_spk is not None:
                speaker_chain.append(_cosine(spk["embedding"], previous_spk))
            previous_spk = spk["embedding"]
        return wav, sr

    metas = [
        {
            "role": (p.get("voice") or "").strip() or None,
            "line": None,
            "line_id": None,
            "index": i + 1,
            "total": len(pieces),
            "text": piece[:80],
            "mode": "clone",
        }
        for i, piece in enumerate(pieces)
    ]
    gap = _gap_value(gap_secs, cfg.CLONE_GAP_SECS)
    wav, sr = _run_pieces(pieces, gen_one, gap, on_progress, metas)
    _record_sampling(
        p, sampling, retried,
        {
            "xvec_only": bool(xvec_only), "chunk_chars": chunk_chars,
            "gap_secs": gap, "candidate_takes": candidate_takes,
            "speaker_cosine": (
                round(min(x["cosine"] for x in speaker_scores), 4)
                if speaker_scores else None
            ),
            "speaker_cos_min": (
                round(min(x["min"] for x in speaker_scores), 4)
                if speaker_scores else None
            ),
            "speaker_chain_min": (
                round(min(speaker_chain), 4) if speaker_chain else None
            ),
        },
    )
    if len(pieces) > 1:
        extra += f"（长文本自动切为 {len(pieces)} 段）"
    if sampling["stable"]:
        extra += (
            f"\n音色稳定：温度 {sampling['temperature']:.2f}，"
            f"top_p {sampling['top_p']:.2f}，seed {sampling['seed']}，"
            f"{'仅说话人向量' if xvec_only else '完整 ICL'}"
        )
        if speaker_extra:
            extra += f"；说话人相似度择优额外生成 {speaker_extra} 条候选"
        if retried:
            extra += f"；检测到 {retried} 段频谱偏移，已自动重生成"
        if speaker_scores:
            worst = min(x["min"] for x in speaker_scores)
            extra += f"\n说话人相似度最低窗口 {worst:.3f}"
    return wav, sr, extra


# ---------------------------------------------------------------------------
# 流式（边生成边播）
# ---------------------------------------------------------------------------
# 内核每 chunk_size 个 codec 步吐一块，12Hz 编码器下 chunk_size=8 ≈ 0.67 秒音频。
# 1.7B / 6.5 秒中文 / xvec_only 的实测（tools/bench_stream.py）：
#     chunk=4   TTFA 453ms   RTF 0.909
#     chunk=8   TTFA 624ms   RTF 0.827   ← 取这个
#     chunk=12  TTFA 887ms   RTF 0.769
# 块越小出声越快，但 RTF 反而越差（固定开销摊不薄）。8 是折中：0.67 秒一块，
# RTF 还留着 0.17 的余量 —— RTF 一旦 >= 1 就会「播完了下一块还没好」。
STREAM_CHUNK_SIZE = 8

# 内核输出的波形采样率。注意 12Hz 是 codec 帧率（每秒 12 个 token），不是采样率，
# 别把名字里的 12Hz 当成音频采样率用。
STREAM_SAMPLE_RATE = 24000


def status_line(wav: np.ndarray, sr: int, t_cost: float, extra: str = "") -> str:
    """流式收尾时给 server 用。

    普通任务的状态行由 ``synthesize()`` 内部算，流式的音频是边播边攒的、
    等 server 拼完才知道时长，所以把同一份措辞单独暴露出来 ——
    两条路必须给出同一个口径的 RTF，否则合成记录里两种任务没法比。
    """
    return _status_line(wav, sr, t_cost, extra)


def stream_ready(p: dict) -> tuple[bool, str]:
    """这一单能不能流式。不能时返回一句人话，前端据此退回普通任务。"""
    mode = (p.get("mode") or "clone").strip().lower()
    text = (p.get("text") or "").strip()
    if not text:
        return False, "请先输入要合成的文本。"
    if mode == "script":
        return False, "剧本工坊要逐句换角色、反复重建 prompt，流式会追不上播放。"
    if mode == "design":
        return False, "声音设计生成的是「一把新嗓子」，没有边听边等的需求。"
    if mode == "official":
        if _is_official_dialog(text):
            return False, "多人对话要逐行换说话人，无法流式。"
        return True, ""
    if mode == "clone":
        return True, ""
    return False, f"未知的生成模式：{mode}"


def synthesize_stream(p: dict):
    """流式合成：边生成边把音频块交出去。

    产出 ``(kind, payload)``，``kind`` 取值：
        meta   首个 —— ``{"sr", "chunk_size", "pieces", "mode"}``
        pcm    ``np.ndarray`` float32，可多次
        gap    段间静音 —— ``np.ndarray``
        done   末个 —— ``{"took", "pieces", "status"}``
        error  出错 —— ``{"message"}``

    只负责「生成」，落盘与合成记录交给 server —— 与 ``synthesize()`` 的分工一致。

    **整段持 _GPU_LOCK**：generator 在 yield 期间锁不释放，否则另一路生成会挤进
    同一张 CUDA graph，两路互相踩；出来的音频会张冠李戴。
    """
    ok, why = stream_ready(p)
    if not ok:
        yield ("error", {"message": why})
        return

    mode = (p.get("mode") or "clone").strip().lower()
    text = (p.get("text") or "").strip()
    language = p.get("language") or "Auto"
    size = cfg.resolve_size(p.get("size"))
    instruct = (p.get("instruct") or "").strip() or None
    chunk_size = max(1, int(p.get("chunk_size") or STREAM_CHUNK_SIZE))
    prof = cfg.profile()
    gap = _gap_value(
        p.get("gap_secs"),
        cfg.CLONE_GAP_SECS if mode == "clone" else cfg.SCRIPT_GAP_SECS,
    )
    gate = np.zeros(int(STREAM_SAMPLE_RATE * gap), dtype=np.float32)

    with _GPU_LOCK:
        t0 = time.time()
        try:
            if mode == "clone":
                ref_audio, ref_text, xvec_only, warm = _resolve_clone_source(p)
                sampling = _clone_sampling(p)
                chunk_chars = _clone_chunk_chars(p)
                # 流式不做逐段漂移检测（音频已经边生成边播出去了，
                # 再换 take 会让用户听到的内容和最终文件不一致）。
                _record_sampling(
                    p, sampling, 0,
                    {"xvec_only": bool(xvec_only), "chunk_chars": chunk_chars,
                     "gap_secs": gap},
                )
                model = load_model("clone", size)
                if warm is not None:
                    _prime_prompt_cache(model, ref_audio, ref_text, xvec_only, warm, size)
                pieces = split_text(text, chunk_chars)
                yield ("meta", {"sr": STREAM_SAMPLE_RATE, "chunk_size": chunk_size,
                                "pieces": len(pieces), "mode": mode})
                for i, piece in enumerate(pieces):
                    if i:
                        yield ("gap", gate)
                    # 与普通生成一致：所有段共用主 seed，只有漂移重试才换 seed。
                    seg_seed = sampling["seed"]
                    kwargs: dict[str, Any] = {}
                    if sampling["temperature"] is not None:
                        kwargs["temperature"] = sampling["temperature"]
                    if sampling["top_p"] is not None:
                        kwargs["top_p"] = sampling["top_p"]
                    with _seeded_rng(seg_seed):
                        stream = model.generate_voice_clone_streaming(
                            text=piece, language=language,
                            ref_audio=str(ref_audio), ref_text=ref_text or "",
                            xvec_only=xvec_only, instruct=instruct,
                            chunk_size=chunk_size,
                            **kwargs,
                        )
                        for chunk, _sr, _timing in stream:
                            yield ("pcm", np.asarray(chunk, dtype=np.float32))
                    # 基因只预热一次；后续段直接命内核缓存
                    warm = None
            else:  # official 单人（多人对话已在 stream_ready 挡掉）
                model = load_model("official", size)
                speaker = (p.get("speaker") or "vivian").lower()
                pieces = split_text(text, prof.script_chunk_chars)
                yield ("meta", {"sr": STREAM_SAMPLE_RATE, "chunk_size": chunk_size,
                                "pieces": len(pieces), "mode": mode})
                for i, piece in enumerate(pieces):
                    if i:
                        yield ("gap", gate)
                    stream = model.generate_custom_voice_streaming(
                        text=piece, speaker=speaker, language=language,
                        instruct=instruct, chunk_size=chunk_size,
                    )
                    for chunk, _sr, _timing in stream:
                        yield ("pcm", np.asarray(chunk, dtype=np.float32))
        except Exception as e:  # noqa: BLE001
            yield ("error", {"message": _friendly_error(e)})
            return

        yield ("done", {"took": round(time.time() - t0, 2),
                        "pieces": len(pieces), "status": "",
                        "params": {
                            k: p.get(k) for k in
                            ("stable", "temperature", "top_p", "seed",
                             "drift_retries", "candidate_takes",
                             "speaker_cosine", "speaker_cos_min",
                             "speaker_chain_min", "xvec_only", "chunk_chars",
                             "gap_secs")
                            if p.get(k) is not None
                        },
                        # 流式**不做**后处理：音频块在生成的同时就已经播出去了，
                        # 收尾再剪静音/归一化只会让「下载到的」和「刚听到的」不一样。
                        # 想听后处理后的版本，用普通生成（参数不用改）。
                        "postprocess": False})


def warmup(kind: str = "clone", size: str = "") -> float:
    """把「加载权重 + 捕获 CUDA graph」两段都跑掉，返回耗时（秒）。

    只 ``load_model`` 是不够的：CUDA graph 是在**第一次 generate** 时才捕获的，
    那一段十几秒——不预热的话，用户点的第一单依旧要等这么久，正是
    「启动即加载」想省掉的那段。

    预热用的文本压到最短（max_new_tokens=32，不到 3 秒音频），只要走完一次
    完整路径就能让图和缓存就位。副作用是顺手把「音色库第一个音色」的 prompt
    带进内核缓存，对此后的生成只有好处。
    """
    t0 = time.time()
    size = cfg.resolve_size(size or None)
    model = load_model(kind, size)
    try:
        with _GPU_LOCK:
            if kind == "clone":
                names = voices_mod.list_voices()
                if not names:
                    return time.time() - t0   # 没音色就没法预热，权重加载已经够本
                v = voices_mod.load_voice(names[0]) or {}
                audio = v.get("audio_path")
                if not audio:
                    return time.time() - t0
                model.generate_voice_clone(
                    text="你好。", language="Chinese", ref_audio=str(audio),
                    ref_text=v.get("ref_text") or "",
                    xvec_only=bool(v.get("xvec_only", cfg.CLONE_XVEC_ONLY)),
                    max_new_tokens=32,
                )
            elif kind == "official":
                model.generate_custom_voice(
                    text="你好。", speaker="vivian", language="Chinese",
                    max_new_tokens=32,
                )
            else:  # design 没有稳定的最小输入，权重加载到这儿就算完
                pass
    except Exception:  # noqa: BLE001  预热失败不该让启动失败，生成时照常重试
        pass
    return time.time() - t0
