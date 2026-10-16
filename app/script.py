"""Qwen3-TTS Studio — 剧本解析与校验（**纯函数，不碰模型**）。

剧本每行一个格式：

    [角色名] 要说的台词
    [角色名][情感指令] 要说的台词

* ``[角色名]`` 必填。剧本工坊里它必须是音色库中已有的音色名；
  官方精品里它必须是内置的 9 个说话人之一。
* ``[情感指令]`` 可选，是**自由文本** —— Qwen3-TTS 的 instruct 不查词表，
  你写「压着火，语速偏快」和写「愤怒」都会被原样送进模型，
  这一点与 OmniVoice 的固定标签机制不同。

把解析和生成分开的理由：界面上要做**实时预览**（哪几行能识别、哪几行会被跳过、
各角色各多少句），如果解析藏在生成函数里，预览就得加载模型才能显示 —— 太贵。
"""

from __future__ import annotations

import re
from typing import Iterable

# [角色] 或 [角色][指令] 前缀
LINE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:\[([^\]]+)\])?\s*(.+?)\s*$")

# 行首没有 [角色] 时，看看是不是用户写了「角色：台词」这种常见习惯，给出针对性提示
COLON_RE = re.compile(r"^\s*([^\[\]:：]{1,12})\s*[:：]\s*(.+?)\s*$")


def parse_line(raw: str, index: int = 0) -> dict:
    """解析单行。识别失败时 ``ok=False``，并给出 ``issue``。"""
    line = (raw or "").rstrip("\n")
    m = LINE_RE.match(line)
    if m:
        role, instruct, content = m.group(1).strip(), (m.group(2) or "").strip(), m.group(3).strip()
        if not content:
            return {"index": index, "raw": line, "role": role, "instruct": instruct,
                    "content": "", "ok": False, "issue": "只有角色标记，没有台词。"}
        return {"index": index, "raw": line, "role": role, "instruct": instruct,
                "content": content, "ok": True, "issue": ""}

    c = COLON_RE.match(line)
    if c:
        return {"index": index, "raw": line, "role": c.group(1).strip(), "instruct": "",
                "content": c.group(2).strip(), "ok": False,
                "issue": "看起来用了「角色：台词」写法，但本工具要求方括号，"
                         f"请改成「[{c.group(1).strip()}] {c.group(2).strip()[:16]}…」。"}

    return {"index": index, "raw": line, "role": None, "instruct": "",
            "content": line.strip(), "ok": False,
            "issue": "缺少 [角色名] 前缀，本行会被跳过。"}


def parse(text: str) -> list[dict]:
    """把整段剧本解析成结构化行（空行忽略）。"""
    out: list[dict] = []
    for i, ln in enumerate((text or "").splitlines(), 1):
        if not ln.strip():
            continue
        out.append(parse_line(ln, i))
    return out


def validate(lines: list[dict], known_roles: Iterable[str] | None = None) -> list[str]:
    """返回问题列表（面向用户的中文描述）。"""
    issues: list[str] = []
    known = set(known_roles) if known_roles is not None else None
    for ln in lines:
        if not ln["ok"]:
            if ln.get("issue"):
                issues.append(f"第 {ln['index']} 行：{ln['issue']}")
            continue
        if known is not None and ln["role"] not in known:
            issues.append(f"第 {ln['index']} 行：音色 [{ln['role']}] 不在库里。")
    return issues


def stats(lines: list[dict]) -> dict:
    """统计信息，给界面上的预览卡片用。"""
    ok = [ln for ln in lines if ln["ok"]]
    by_role: dict[str, int] = {}
    for ln in ok:
        by_role[ln["role"]] = by_role.get(ln["role"], 0) + 1
    return {
        "total_lines": len(lines),
        "ok_lines": len(ok),
        "bad_lines": len(lines) - len(ok),
        "roles": by_role,
        "role_count": len(by_role),
        "instruct_lines": sum(1 for ln in ok if ln["instruct"]),
        "chars": sum(len(ln["content"]) for ln in ok),
    }


def preview(text: str, known_roles: Iterable[str] | None = None) -> dict:
    """界面实时预览：解析 + 校验 + 统计，一次给全。"""
    lines = parse(text)
    return {
        "lines": lines,
        "issues": validate(lines, known_roles),
        "stats": stats(lines),
    }


def render(lines: list[dict]) -> str:
    """把结构化行还原成规范化的剧本文本（供「一键整理」按钮用）。"""
    out = []
    for ln in lines:
        if not ln["ok"]:
            continue
        head = f"[{ln['role']}]"
        if ln["instruct"]:
            head += f"[{ln['instruct']}]"
        out.append(f"{head} {ln['content']}")
    return "\n".join(out)


def all_roles(lines: list[dict]) -> list[str]:
    """按首次出现顺序列出角色。"""
    seen: list[str] = []
    for ln in lines:
        if ln["ok"] and ln["role"] not in seen:
            seen.append(ln["role"])
    return seen
