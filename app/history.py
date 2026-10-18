"""Qwen3-TTS Studio — 合成记录。

写在 ``outputs/history.json``，**只存参数不存音频副本** —— 音频本体就是
``outputs/`` 里的 wav 文件（那是产物，要清得自己清）。记录最多保留
``config.HISTORY_MAX`` 条，超出后丢最旧的。

每条记录形如::

    {
      "id": "20261015_213012_4821",
      "ts": "2026-10-15 21:30:12",
      "mode": "clone", "size": "1.7B", "language": "Auto",
      "voice": "御姐", "instruct": "压低声音，语气紧张",
      "text": "正文（默认存全文，只有超过 2000 字才截）",
      "file": "clone_20261015_213012.wav",
      "duration": 6.4, "chars": 18, "status": "ok"
    }
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import config as cfg

_PATH = cfg.OUTPUT_DIR / "history.json"
_LOCK = threading.Lock()
# 正文按**全文**存，这里只是一道防爆上限。
# 早先是 200 字：列表里看不出问题（本来就只显示两行），但「鼠标经过看原文」
# 弹出来的就是这 200 字 —— 用户会认为功能没做完。2000 与单次生成的文本上限
# （limits.max_text_chars）对齐，正常一次生成必定存全。
_TEXT_MAX = 2000


def _read() -> list[dict]:
    if not _PATH.exists():
        return []
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001  损坏的记录文件不该让程序起不来
        return []


def _write(items: list[dict]) -> None:
    try:
        _PATH.write_text(
            json.dumps(items[: cfg.HISTORY_MAX], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def add(entry: dict) -> dict:
    """追加一条记录（线程安全）。"""
    rec = {
        "id": f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}",
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": entry.get("mode", ""),
        "size": entry.get("size", ""),
        "language": entry.get("language", ""),
        "voice": entry.get("voice", ""),
        "speaker": entry.get("speaker", ""),
        "instruct": entry.get("instruct", ""),
        "text": (entry.get("text") or "")[:_TEXT_MAX],
        "chars": len(entry.get("text") or ""),
        "file": entry.get("file", ""),
        "duration": entry.get("duration", 0.0),
        # 生成耗时（服务端掐的合成时间）、克隆方式、段间静音 —— 都属于"这次用了什么"，
        # 合成记录里要能查到，别只能看结果卡那一张
        "took": round(float(entry.get("took") or 0), 2),
        "xvec_only": entry.get("xvec_only"),
        "gap_secs": entry.get("gap_secs"),
        # 后处理统计（剪掉多少静音 / 实际增益 / 是不是流式跳过了）。
        # 没有它，「这条为什么比上次短 1.2 秒」事后根本查不出来 ——
        # 参数区显示的是「开没开」，这里记的是「实际动了多少」，不是一回事。
        "postprocess": entry.get("postprocess"),
        "status": entry.get("status", "ok"),
    }
    with _LOCK:
        items = _read()
        items.insert(0, rec)
        _write(items)
    return rec


def list_all(limit: int = 0) -> list[dict]:
    """倒序返回记录，并标注音频文件是否还在。"""
    with _LOCK:
        items = _read()
    out = []
    for it in items[:limit or None]:
        f = it.get("file") or ""
        it = dict(it)
        it["file_exists"] = bool(f) and (cfg.OUTPUT_DIR / f).exists()
        out.append(it)
    return out


def delete(ids: list[str]) -> int:
    """按 id 删除记录（不动 outputs/ 里的 wav）。"""
    want = {i for i in (ids or []) if i}
    if not want:
        return 0
    with _LOCK:
        items = _read()
        kept = [i for i in items if i.get("id") not in want]
        removed = len(items) - len(kept)
        _write(kept)
    return removed


def clear() -> int:
    """清空全部记录（同样不动 wav）。"""
    with _LOCK:
        n = len(_read())
        _write([])
    return n


def find(rec_id: str) -> Optional[dict]:
    for it in _read():
        if it.get("id") == rec_id:
            return it
    return None
