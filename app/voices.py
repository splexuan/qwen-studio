"""Qwen3-TTS Studio — 音色库。

一个「音色」由两部分定义：

* **参考音频** ``<名字>.wav`` —— 必需，音色的本体；
* **参考文本** ``<名字>.txt`` —— 该音频实际说的字。仅在完整 ICL 模式
  （``xvec_only=False``）下必需；仅用说话人向量时可以不填。

另有可选的两份附属文件：

* ``<名字>.json`` —— 元数据（创建时间、模式、来源）。没有也能用，
  缺失时按默认值补上；
* ``<名字>.pt`` —— **音色基因**。保存时抽取一次参考音频的声学表征，
  下次生成直接预热进内核的 prompt 缓存，省掉重采样与说话人编码。

原项目（Qwen3TTS_Pro）把 ``.pt`` 写出来却从未读回 —— 「音色固化·基因永久存储」
这个卖点实际上没生效，每次生成都从 wav 重编码。本模块补上了这一步：
``build_prompt()`` 生成基因，``load_voice()`` 返回的 ``prompt`` 字段会被
``core._prime_prompt_cache()`` 消费。

命令行（排障与迁移用）::

    runtime\\python.exe app\\voices.py list
    runtime\\python.exe app\\voices.py import "C:\\Game\\Qwen3TTS（千问）\\Qwen3TTS_Pro\\personas"
    runtime\\python.exe app\\voices.py warm 韩立
    runtime\\python.exe app\\voices.py doctor
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np
import soundfile as sf

import config as cfg

# Windows 文件名非法字符 + 控制字符
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))}

WAV_EXT = ".wav"
TXT_EXT = ".txt"
META_EXT = ".json"
BIO_EXT = ".pt"


class VoiceExistsError(RuntimeError):
    """同名音色已存在，且调用方没有要求覆盖。

    单独分一个类型是给 web 层认的：它要把这种情况回报成 ``code=name_exists``，
    界面才好把「已存在」升级成「要不要覆盖」的二次确认 —— 光有一句
    错误文本，前端只能靠匹配字符串来认，改个字就废了。
    """

# 参考音频的健康区间（秒）。低于 2s 信息太少，高于 20s 收益递减且拖慢编码。
REF_MIN_SECS = 2.0
REF_GOOD_MAX_SECS = 12.0


# ---------------------------------------------------------------------------
# 名字处理
# ---------------------------------------------------------------------------
def sanitize_name(raw: str) -> str:
    """把用户输入变成安全的音色名。"""
    s = _BAD_CHARS.sub("_", (raw or "").strip())
    s = s.strip(" .")
    if not s:
        return ""
    if s.upper() in RESERVED:
        s = f"_{s}"
    return s[:48]


def path_of(name: str, ext: str) -> Path:
    return cfg.VOICES_DIR / f"{name}{ext}"


# ---------------------------------------------------------------------------
# 音频读写
# ---------------------------------------------------------------------------
def read_audio(src: str | Path) -> tuple[np.ndarray, int]:
    """读成单声道 float32。"""
    data, sr = sf.read(str(src), dtype="float32", always_2d=False)
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim > 1:
        arr = arr.mean(axis=1)
    return arr, int(sr)


def write_audio(arr: np.ndarray, sr: int, dst: str | Path) -> None:
    sf.write(str(dst), np.asarray(arr, dtype=np.float32), int(sr))


def clamp_audio(arr: np.ndarray) -> np.ndarray:
    """削掉超过 ±1 的样本，避免写文件时爆音。"""
    peak = float(np.max(np.abs(arr))) if arr.size else 0.0
    if peak > 1.0:
        arr = arr / peak
    return arr


def trim(arr: np.ndarray, sr: int, start: float, end: float) -> np.ndarray:
    """按秒裁剪，自动纠正顺序与越界。"""
    total = len(arr) / sr
    a = max(0.0, min(float(start), total))
    b = max(0.0, min(float(end), total))
    if b <= a:
        a, b = min(a, b), max(a, b)
    if b - a < 0.3:  # 裁太短没有意义，退回原音频
        return arr
    return arr[int(a * sr): int(b * sr)]


def duration_of_file(path: str | Path) -> float:
    """读任意音频文件的时长（秒）；读不出来返回 0。"""
    try:
        info = sf.info(str(path))
        return round(info.frames / info.samplerate, 2)
    except Exception:  # noqa: BLE001
        return 0.0


def duration_of(name: str) -> float:
    return duration_of_file(path_of(name, WAV_EXT))


def _ffmpeg_exe() -> Optional[str]:
    """随包 ffmpeg 的位置。config 已把它加进 PATH，先问 PATH，再退回 vendor/bin。"""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    for cand in (cfg.VENDOR_BIN / "ffmpeg.exe", cfg.VENDOR_BIN / "ffmpeg"):
        if cand.exists():
            return str(cand)
    return None


def ensure_decodable(path: str | Path) -> Path:
    """保证返回一个 soundfile 读得出来的文件；读不出来就用随包 ffmpeg 转一道。

    为什么需要它：libsndfile 认 wav / flac / ogg / mp3，**不认 m4a / aac / wma**，
    而手机录的音几乎都是 m4a。界面上那个上传框从第一天就写着支持 m4a，
    用户拖进来却只会看到「这个文件读不出音频」—— 可它明明是一段有效音频。
    ffmpeg 就躺在 vendor/bin 里，转一道就行，转出来的文件放在源文件旁边。

    转码成功后**会删掉原文件**：调用方交进来的都是临时文件
    （``web/_uploads/`` 里的上传件、链接下载的中转件），留着只是多一份重复音频。
    """
    p = Path(path)
    try:
        sf.info(str(p))
        return p
    except Exception:  # noqa: BLE001
        pass  # 交给 ffmpeg，失败再统一报错

    exe = _ffmpeg_exe()
    if not exe:
        raise RuntimeError(
            "这个音频容器 libsndfile 读不了，需要 vendor/bin/ffmpeg.exe 转码，但没找到它。"
        )
    dst = p.with_name(f"{p.stem}.conv.wav")
    cmd = [exe, "-y", "-v", "error", "-i", str(p),
           "-vn", "-ac", "1", "-c:a", "pcm_s16le", str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=300)
    except Exception as e:  # noqa: BLE001
        dst.unlink(missing_ok=True)
        raise RuntimeError(f"用 ffmpeg 转码失败：{e}") from e
    if r.returncode != 0 or not dst.exists():
        dst.unlink(missing_ok=True)
        tail = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(
            "这个文件读不出音频 —— 不是有效的音频文件，或者它的编码连 ffmpeg 也解不开。"
            + (f"（ffmpeg: {tail[-1][:120]}）" if tail else "")
        )
    p.unlink(missing_ok=True)
    return dst


# ---------------------------------------------------------------------------
# 元数据
# ---------------------------------------------------------------------------
def _read_meta(name: str) -> dict:
    p = path_of(name, META_EXT)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _write_meta(name: str, meta: dict) -> None:
    try:
        path_of(name, META_EXT).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


def _read_ref_text(name: str) -> str:
    """参考文本放在 .txt 里 —— 与 Qwen3TTS_Pro 的 personas 布局一致，便于迁移。"""
    p = path_of(name, TXT_EXT)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return ""


def _write_ref_text(name: str, text: str) -> None:
    path_of(name, TXT_EXT).write_text((text or "").strip(), encoding="utf-8")


# ---------------------------------------------------------------------------
# 读 / 列
# ---------------------------------------------------------------------------
def list_voices() -> list[str]:
    """按名字列出音色库（以 .wav 存在为准）。"""
    if not cfg.VOICES_DIR.exists():
        return []
    names = [p.stem for p in cfg.VOICES_DIR.glob(f"*{WAV_EXT}")]
    return sorted(names)


def load_voice(name: str) -> Optional[dict]:
    """读取一个音色的全部信息；不存在返回 None。

    返回 dict 的 ``prompt`` 字段是音色基因（.pt）的内容，读不到则为 None ——
    ``core`` 会据此决定是否预热内核缓存，读不到只是慢一点，不影响正确性。
    """
    name = (name or "").strip()
    wav = path_of(name, WAV_EXT)
    if not name or not wav.exists():
        return None
    meta = _read_meta(name)
    return {
        "name": name,
        "audio_path": wav,
        "ref_text": _read_ref_text(name),
        "xvec_only": bool(meta.get("xvec_only", cfg.CLONE_XVEC_ONLY)),
        "source": meta.get("source", ""),
        "created": meta.get("created", ""),
        "duration": duration_of(name),
        "has_prompt": path_of(name, BIO_EXT).exists(),
        "prompt": _load_prompt(name),
    }


def _load_prompt(name: str) -> Optional[dict]:
    """读音色基因文件。任何异常都当作「没有」处理。"""
    p = path_of(name, BIO_EXT)
    if not p.exists():
        return None
    try:
        import torch

        try:
            from qwen_tts.inference.qwen3_tts_model import VoiceClonePromptItem

            torch.serialization.add_safe_globals([VoiceClonePromptItem])
        except Exception:  # noqa: BLE001
            pass
        return torch.load(str(p), map_location="cpu", weights_only=False)
    except Exception:  # noqa: BLE001
        return None


def rows() -> list[dict]:
    """给界面用的列表：每个音色的摘要信息。

    带上 ``ref_text`` 原文（而不只是字数）是刻意的：界面用它来
    ① 在卡片上直接显示参考文本 ② 打开「参考文本」弹窗时预填充。
    早先只给 ``chars``，弹窗就只能空着让人重新打一遍。
    参考文本一般几十字，就算十几个音色也就几 KB，随 bootstrap 一起给没有负担。
    """
    out = []
    for n in list_voices():
        dur = duration_of(n)
        ref = _read_ref_text(n)
        wav = path_of(n, WAV_EXT)
        out.append({
            "name": n,
            "duration": dur,
            "chars": len(ref),
            "ref_text": ref,
            "has_prompt": path_of(n, BIO_EXT).exists(),
            # 音频文件的修改时间（秒）。前端拿它当 URL 的版本号：
            # 同一个音色名换了音频，URL 一变浏览器就会重新取，不会拿着旧的放。
            # 只靠 Cache-Control 不够 —— 老客户端、直接贴链接、反向代理都可能自己缓存。
            "mtime": int(wav.stat().st_mtime) if wav.exists() else 0,
            "length_hint": (
                "偏短" if 0 < dur < REF_MIN_SECS
                else "偏长" if dur > REF_GOOD_MAX_SECS
                else "合适"
            ),
            "xvec_only": bool(_read_meta(n).get("xvec_only", cfg.CLONE_XVEC_ONLY)),
        })
    return out


# ---------------------------------------------------------------------------
# 写：新增 / 更新 / 改名 / 删除 / 裁剪
# ---------------------------------------------------------------------------
def save_voice(
    raw_name: str,
    audio: str | Path | tuple[np.ndarray, int],
    ref_text: str = "",
    xvec_only: Optional[bool] = None,
    overwrite: bool = False,
    source: str = "upload",
) -> dict:
    """新增或覆盖一个音色。返回 ``{"name": ..., "replaced": bool}``。

    ``audio`` 可以是文件路径，也可以是 ``(waveform, sr)``。

    ``overwrite=False`` 时**同名直接报错**，不再自动改成「芳芳_2」：
    悄悄改名会让用户以为自己存了两个不同的东西，实际是同一个名字的两份拷贝，
    之后用错一份还很难看出来。要覆盖就明确传 ``overwrite=True``。
    """
    name = sanitize_name(raw_name)
    if not name:
        raise RuntimeError("音色名不能为空，或包含非法字符。")

    exists = path_of(name, WAV_EXT).exists()
    if exists and not overwrite:
        raise VoiceExistsError(f"音色 [{name}] 已存在。")

    if isinstance(audio, tuple):
        arr, sr = audio
        arr = np.asarray(arr, dtype=np.float32)
        if arr.ndim > 1:
            arr = arr.mean(axis=1)
    else:
        arr, sr = read_audio(audio)

    if arr.size < int(sr * 0.3):
        raise RuntimeError("参考音频太短（不足 0.3 秒），无法作为音色。")

    arr = clamp_audio(arr)
    if not exists:  # 覆盖时保留原创建时间
        _write_meta(name, {
            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": source,
            "xvec_only": cfg.CLONE_XVEC_ONLY if xvec_only is None else bool(xvec_only),
        })
    else:
        meta = _read_meta(name)
        meta.setdefault("created", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        meta["source"] = source
        if xvec_only is not None:
            meta["xvec_only"] = bool(xvec_only)
        _write_meta(name, meta)

    write_audio(arr, sr, path_of(name, WAV_EXT))
    _write_ref_text(name, ref_text)

    # 音频变了，旧的基因文件就失效了 —— 直接删掉，避免用到过期表征
    bio = path_of(name, BIO_EXT)
    if bio.exists():
        bio.unlink(missing_ok=True)

    return {"name": name, "replaced": exists}


def update_ref_text(name: str, text: str) -> None:
    if not path_of(name, WAV_EXT).exists():
        raise RuntimeError(f"音色 [{name}] 不存在。")
    _write_ref_text(name, text)


def set_xvec_only(name: str, xvec_only: bool) -> None:
    if not path_of(name, WAV_EXT).exists():
        raise RuntimeError(f"音色 [{name}] 不存在。")
    meta = _read_meta(name)
    meta["xvec_only"] = bool(xvec_only)
    _write_meta(name, meta)


def trim_voice(name: str, start: float, end: float) -> dict:
    """就地裁剪参考音频（界面上拖选后保存）。"""
    wav = path_of(name, WAV_EXT)
    if not wav.exists():
        raise RuntimeError(f"音色 [{name}] 不存在。")
    arr, sr = read_audio(wav)
    before = round(len(arr) / sr, 2)
    out = clamp_audio(trim(arr, sr, start, end))
    write_audio(out, sr, wav)
    path_of(name, BIO_EXT).unlink(missing_ok=True)  # 基因随之失效
    return {"name": name, "before": before, "after": round(len(out) / sr, 2)}


def rename_voice(old: str, raw_new: str) -> str:
    old = (old or "").strip()
    if not path_of(old, WAV_EXT).exists():
        raise RuntimeError(f"音色 [{old}] 不存在。")
    new = sanitize_name(raw_new)
    if not new:
        raise RuntimeError("新名字不能为空，或包含非法字符。")
    if new == old:
        return new
    if path_of(new, WAV_EXT).exists():
        raise RuntimeError(f"已存在名为 [{new}] 的音色。")

    for ext in (WAV_EXT, TXT_EXT, META_EXT, BIO_EXT):
        src, dst = path_of(old, ext), path_of(new, ext)
        if src.exists():
            shutil.move(str(src), str(dst))

    meta = _read_meta(new)
    meta["renamed_from"] = old
    meta["renamed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _write_meta(new, meta)
    return new


def delete_voices(names: list[str]) -> list[str]:
    """批量删除。返回实际删掉的名字。"""
    done = []
    for n in names or []:
        n = (n or "").strip()
        if not n:
            continue
        hit = False
        for ext in (WAV_EXT, TXT_EXT, META_EXT, BIO_EXT):
            p = path_of(n, ext)
            if p.exists():
                p.unlink(missing_ok=True)
                hit = True
        if hit:
            done.append(n)
    return done


# ---------------------------------------------------------------------------
# 音色基因（.pt）
# ---------------------------------------------------------------------------
def build_prompt(name: str, size: Optional[str] = None) -> str:
    """为音色抽取并保存基因文件。需要加载模型，因此是个较重的操作。

    ``x_vector_only_mode=False`` 同时抽出 codec token 与说话人向量，
    这样两种模式（仅向量 / 完整 ICL）都能复用同一份基因。
    """
    v = load_voice(name)
    if v is None:
        raise RuntimeError(f"音色 [{name}] 不存在。")

    import core  # 延迟导入：避免 core ↔ voices 的循环依赖

    # 先做规格收敛：界面传的默认值是 "auto"，而 MODEL_DIRS 只有
    # ("clone","1.7B") 这样的实键，直接传下去会抛「未知的功能/规格组合」。
    size = cfg.resolve_size(size)
    # 和生成共用同一把 GPU 锁：保存音色会顺手排队抽基因，连着存几个音色就是
    # 几个任务同时进来，而模型状态是全局共享的 —— 不串行会互相踩。
    # 加载也必须一起包进来，否则刚拿到 model 就被另一个线程卸掉换规格了。
    with core._GPU_LOCK:
        model = core.load_model("clone", size)

        arr, sr = read_audio(v["audio_path"])
        arr = clamp_audio(arr)

        ref_text = (v.get("ref_text") or "").strip()
        # 抽取模式必须跟着参考文本走，不能写死：
        # 内核在 ``x_vector_only_mode=False`` 且 ``ref_text`` 为空时会直接抛
        # ``ValueError: ref_text is required when x_vector_only_mode is False``。
        # 而「只上传了音频、没填参考文本」是完全合法的用法（默认的仅向量模式
        # 根本不需要参考文本），所以这种音色以前点「生成音色基因」必失败。
        #
        # 有参考文本 → 抽完整 ICL（codec token + 说话人向量），两种模式共用一份；
        # 没有       → 只抽说话人向量。不损失能力：没有参考文本时完整 ICL 本来
        #              就用不了（core.synthesize 会明确拦住）。
        items = model.model.create_voice_clone_prompt(
            ref_audio=(arr, sr),
            ref_text=ref_text,
            x_vector_only_mode=not ref_text,
        )
        payload = {
            "version": 1,
            "model_size": size,
            "ref_text": ref_text,
            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "items": [
                {
                    "ref_code": it.ref_code,
                    "ref_spk_embedding": it.ref_spk_embedding,
                    "x_vector_only_mode": bool(it.x_vector_only_mode),
                    "icl_mode": bool(it.icl_mode),
                    "ref_text": it.ref_text,
                }
                for it in items
            ],
        }
        import torch

        torch.save(payload, str(path_of(name, BIO_EXT)))
    return name


# ---------------------------------------------------------------------------
# 迁移：从 Qwen3TTS_Pro 的 personas/ 导入
# ---------------------------------------------------------------------------
def import_dir(src: str | Path, overwrite: bool = False) -> list[str]:
    """把一个「同布局」的目录（.wav/.txt/.pt）整体导入音色库。

    刻意只认这三种文件：personas/ 里就是这么放的，可以原样搬过来。
    """
    src_dir = Path(src)
    if not src_dir.is_dir():
        raise RuntimeError(f"目录不存在：{src_dir}")
    done = []
    for wav in sorted(src_dir.glob(f"*{WAV_EXT}")):
        name = sanitize_name(wav.stem)
        if not name:
            continue
        txt = src_dir / f"{wav.stem}{TXT_EXT}"
        ref_text = txt.read_text(encoding="utf-8").strip() if txt.exists() else ""
        info = save_voice(name, wav, ref_text, overwrite=overwrite, source=f"import:{src_dir.name}")
        # 基因文件直接搬，避免重复加载模型
        bio = src_dir / f"{wav.stem}{BIO_EXT}"
        if bio.exists():
            try:
                shutil.copy2(str(bio), str(path_of(info["name"], BIO_EXT)))
            except Exception:  # noqa: BLE001
                pass
        done.append(info["name"])
    return done


def doctor() -> list[str]:
    """自检：列出音色库里不自洽的项。"""
    problems: list[str] = []
    for n in list_voices():
        dur = duration_of(n)
        if dur < REF_MIN_SECS:
            problems.append(f"[{n}] 参考音频仅 {dur}s，低于建议下限 {REF_MIN_SECS}s。")
        elif dur > REF_GOOD_MAX_SECS:
            problems.append(f"[{n}] 参考音频 {dur}s 偏长，编码慢且收益递减，建议裁到 3-10s。")
        if not _read_ref_text(n):
            problems.append(f"[{n}] 没有参考文本（仅影响完整 ICL 模式）。")
    return problems


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass

    args = list(argv if argv is not None else sys.argv[1:])
    cmd = args[0] if args else "list"

    if cmd == "list":
        for r in rows():
            flag = "有基因" if r["has_prompt"] else "无基因"
            print(f"  {r['name']:<16} {r['duration']:>6.2f}s  {r['length_hint']:<4} {flag}")
        print(f"共 {len(rows())} 个音色 → {cfg.VOICES_DIR}")
    elif cmd == "import":
        if len(args) < 2:
            print("用法: voices.py import <目录>")
            return 2
        names = import_dir(args[1], overwrite="--overwrite" in args)
        print("已导入: " + "、".join(names))
    elif cmd == "warm":
        if len(args) < 2:
            print("用法: voices.py warm <音色名>")
            return 2
        build_prompt(args[1], args[2] if len(args) > 2 else None)
        print(f"已生成基因: {args[1]}{BIO_EXT}")
    elif cmd == "doctor":
        probs = doctor()
        print("\n".join(probs) if probs else "音色库自检通过。")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
