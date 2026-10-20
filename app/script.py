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

为了支持逐句编辑与合成，解析结果还会带上：

* ``id`` / ``line_id``：基于源文件行号的稳定标识，例如 ``L12``；
* ``scene``：当前场景。``=== 第一场 ===``、``--- 第一场 ---``、
  ``场景：客厅``、``INT. 客厅``、``第一场 客厅`` 都会开启一个新场景；
* ``disabled``：行首 ``#`` 表示禁用。禁用行仍会出现在预览里，
  但不参与校验、统计、角色表或合成；
* ``kind="comment"``：行首 ``//`` 表示注释，只用于阅读，不参与合成；
* ``kind="action"``：行首 ``>`` 或整行圆括号表示动作/舞台说明，不参与合成；
* ``chars`` / ``est_secs``：字符数与按默认语速估算的秒数。

空行仍然忽略；场景标题、注释、动作和禁用行不会破坏
「每行一个 ``[角色]`` 台词」的核心解析。
"""

from __future__ import annotations

import re
from typing import Iterable

# [角色] 或 [角色][指令] 前缀
LINE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:\[([^\]]+)\])?\s*(.+?)\s*$")

# 行首没有 [角色] 时，看看是不是用户写了「角色：台词」这种常见习惯，给出针对性提示
COLON_RE = re.compile(r"^\s*([^\[\]:：]{1,12})\s*[:：]\s*(.+?)\s*$")

# 禁用行：允许缩进，标记后可以继续写标准台词
DISABLED_RE = re.compile(r"^\s*#")

# 注释与动作行：保留在结构预览里，但不参与校验、统计和合成。
COMMENT_RE = re.compile(r"^\s*//\s*(.*)$")
ACTION_RE = re.compile(r"^\s*(?:>\s*(.*)|[（(]\s*(.+?)\s*[）)])\s*$")

# 场景标题：=== / --- 包起来的行
SCENE_MARK_RE = re.compile(
    r"^\s*(?:={3,}|-{3,})\s*(.*?)\s*(?:={3,}|-{3,})?\s*$"
)

# 场景标题：场景：客厅 / SCENE 1 / INT. 客厅 / 第一场 客厅
SCENE_TEXT_RE = re.compile(
    r"^\s*(?:(?:场景|場面|SCENE)\s*(?:[一二三四五六七八九十百0-9]+)?\s*[：:]?\s*"
    r"|第[一二三四五六七八九十百0-9]+[场幕]\s*[：:、 ]?\s*"
    r"|(?:INT|EXT)\.\s*)(.*?)\s*$",
    re.IGNORECASE,
)
SCENE_BRACKET_RE = re.compile(
    r"^\s*[【\[]\s*(?:场景|場面|SCENE)\s*[：:]?\s*(.*?)\s*[】\]]\s*$",
    re.IGNORECASE,
)

# 没有历史记录时的兜底语速。server 会用 history.chars_per_sec() 覆盖它。
DEFAULT_CHARS_PER_SEC = 4.8


def estimate_secs(chars: int, chars_per_sec: float = DEFAULT_CHARS_PER_SEC) -> float:
    """按语速估算朗读时长；只用于预览，不是精确时间轴。"""
    try:
        cps = float(chars_per_sec)
    except (TypeError, ValueError):
        cps = DEFAULT_CHARS_PER_SEC
    if cps <= 0:
        cps = DEFAULT_CHARS_PER_SEC
    return round(max(0, int(chars or 0)) / cps, 2)


def _line_id(index: int) -> str:
    """源行号 → 稳定 id。源行号不会随空行过滤而漂移。"""
    return f"L{int(index)}"


def _record(
    index: int,
    raw: str,
    *,
    kind: str = "line",
    role: str | None = None,
    instruct: str = "",
    content: str = "",
    ok: bool = False,
    issue: str = "",
    disabled: bool = False,
    scene: str = "",
    scene_title: str = "",
    is_scene: bool = False,
    ignored: bool = False,
    disabled_role: bool = False,
    disabled_body: str = "",
    chars_per_sec: float = DEFAULT_CHARS_PER_SEC,
) -> dict:
    """生成统一行结构；所有新增字段都在这里集中维护。"""
    chars = len(content or "")
    secs = estimate_secs(chars, chars_per_sec)
    return {
        "index": int(index),
        "id": _line_id(index),
        "line_id": _line_id(index),
        "source_line": int(index),
        "kind": kind,
        "raw": raw,
        "role": role,
        "instruct": instruct,
        # emotion 是 instruct 的别名：前端做角色/情绪标签时更容易读懂。
        "emotion": instruct,
        "content": content,
        "chars": chars,
        "est_secs": secs,
        "estimated_secs": secs,
        "ok": bool(ok),
        "disabled": bool(disabled),
        "issue": issue,
        "scene": scene,
        "scene_title": scene_title,
        "is_scene": bool(is_scene),
        "ignored": bool(ignored),
        # 禁用行需要同时记住「# 后面原本是什么」。没有这两个字段时，
        # 一键整理只能输出 ``# [角色] 台词``，注释和动作会被降级成台词。
        "disabled_role": bool(disabled_role),
        "disabled_body": disabled_body,
    }


def _scene_title(line: str) -> tuple[bool, str]:
    """判断一行是不是场景标题，并返回标题文本。"""
    m = SCENE_BRACKET_RE.match(line)
    if m:
        return True, (m.group(1) or "").strip()
    m = SCENE_MARK_RE.match(line)
    if m:
        return True, (m.group(1) or "").strip()
    m = SCENE_TEXT_RE.match(line)
    if m:
        return True, (m.group(1) or line).strip()
    return False, ""


def _parse_body(line: str, index: int, chars_per_sec: float) -> dict:
    """解析去掉禁用标记后的正文。"""
    m = LINE_RE.match(line)
    if m:
        role, instruct, content = (
            m.group(1).strip(),
            (m.group(2) or "").strip(),
            m.group(3).strip(),
        )
        if not content:
            return _record(
                index, line, role=role, instruct=instruct, content="",
                ok=False, issue="只有角色标记，没有台词。",
                chars_per_sec=chars_per_sec,
            )
        return _record(
            index, line, role=role, instruct=instruct, content=content,
            ok=True, issue="", chars_per_sec=chars_per_sec,
        )

    # 动作行也可能是「角色 + 动作」，例如 ``> [甲] 转身离开``。
    # 这类行只用于结构预览，不应再报「缺少 [角色名]」。
    action = ACTION_RE.match(line)
    if action:
        return _record(
            index, line, kind="action",
            content=(action.group(1) or action.group(2) or "").strip(),
            ok=False, issue="", ignored=True, chars_per_sec=chars_per_sec,
        )

    c = COLON_RE.match(line)
    if c:
        role, content = c.group(1).strip(), c.group(2).strip()
        return _record(
            index, line, role=role, content=content, ok=False,
            issue="看起来用了「角色：台词」写法，但本工具要求方括号，"
                  f"请改成「[{role}] {content[:16]}…」。",
            chars_per_sec=chars_per_sec,
        )

    return _record(
        index, line, role=None, content=line.strip(), ok=False,
        issue="缺少 [角色名] 前缀，本行会被跳过。",
        chars_per_sec=chars_per_sec,
    )


def parse_line(
    raw: str, index: int = 0, chars_per_sec: float = DEFAULT_CHARS_PER_SEC
) -> dict:
    """解析单行。识别失败时 ``ok=False``，并给出 ``issue``。

    场景行返回 ``kind="scene"``；禁用行返回 ``disabled=True`` 且 ``ok=False``。
    这两种情况都不应被当成普通格式错误。
    """
    line = (raw or "").rstrip("\n")
    is_scene, title = _scene_title(line)
    if is_scene:
        return _record(
            index, line, kind="scene", content="", ok=False, issue="",
            scene=title, scene_title=title, is_scene=True,
            chars_per_sec=chars_per_sec,
        )

    disabled = bool(DISABLED_RE.match(line))
    body = re.sub(r"^\s*#\s*", "", line) if disabled else line
    comment = COMMENT_RE.match(body)
    action = ACTION_RE.match(body)
    if comment:
        rec = _record(
            index, body, kind="comment", content=(comment.group(1) or "").strip(),
            ok=False, issue="", ignored=True, chars_per_sec=chars_per_sec,
        )
    elif action:
        rec = _record(
            index, body, kind="action",
            content=(action.group(1) or action.group(2) or "").strip(),
            ok=False, issue="", ignored=True, chars_per_sec=chars_per_sec,
        )
    else:
        rec = _parse_body(body, index, chars_per_sec)
    rec["raw"] = line
    if disabled and not rec.get("ignored"):
        # 禁用台词保留角色/正文，方便前端显示与重新启用，但不进入任何统计。
        rec.update(
            disabled=True, ok=False, issue="",
            disabled_role=bool(rec.get("role")),
            disabled_body=body,
        )
    elif disabled:
        rec.update(disabled=True, disabled_role=True, disabled_body=body)
    return rec


def parse(text: str, chars_per_sec: float = DEFAULT_CHARS_PER_SEC) -> list[dict]:
    """把整段剧本解析成结构化行（空行忽略，场景标题保留）。"""
    out: list[dict] = []
    current_scene = ""
    scene_title = ""
    scene_no = 0
    for i, ln in enumerate((text or "").splitlines(), 1):
        if not ln.strip():
            continue
        is_scene, title = _scene_title(ln)
        if is_scene:
            scene_no += 1
            scene_title = title
            current_scene = title or f"场景 {scene_no}"
            out.append(_record(
                i, ln, kind="scene", content="", ok=False, issue="",
                scene=current_scene, scene_title=scene_title, is_scene=True,
                chars_per_sec=chars_per_sec,
            ))
            continue
        rec = parse_line(ln, i, chars_per_sec)
        # 场景上下文写在每一行上，前端切换场景时不用自己再扫一遍源文本。
        rec["scene"] = current_scene
        rec["scene_title"] = scene_title
        out.append(rec)
    return out


def validate(lines: list[dict], known_roles: Iterable[str] | None = None) -> list[str]:
    """返回问题列表（面向用户的中文描述）。不参与合成的行不报错。"""
    issues: list[str] = []
    known = set(known_roles) if known_roles is not None else None
    for ln in lines:
        if ln.get("disabled") or ln.get("is_scene") or ln.get("ignored"):
            continue
        if not ln.get("ok"):
            if ln.get("issue"):
                issues.append(f"第 {ln['index']} 行：{ln['issue']}")
            continue
        if known is not None and ln.get("role") not in known:
            issues.append(f"第 {ln['index']} 行：音色 [{ln.get('role')}] 不在库里。")
    return issues


def _cast(
    lines: list[dict],
    known_roles: Iterable[str] | None,
    chars_per_sec: float,
) -> list[dict]:
    """按首次出现顺序汇总角色表。"""
    known = set(known_roles) if known_roles is not None else None
    order: list[str] = []
    rows: dict[str, dict] = {}
    for ln in lines:
        if (not ln.get("ok") or ln.get("disabled")
                or ln.get("is_scene") or ln.get("ignored")):
            continue
        role = ln.get("role")
        if not role:
            continue
        if role not in rows:
            order.append(role)
            rows[role] = {
                "role": role,
                "lines": 0,
                "line_count": 0,
                "chars": 0,
                "estimated_secs": 0.0,
                "estimated_seconds": 0.0,
                "mapped": None if known is None else role in known,
                "voice_exists": None if known is None else role in known,
                "missing_voice": None if known is None else role not in known,
                "line_ids": [],
                "scenes": [],
            }
        row = rows[role]
        row["lines"] += 1
        row["line_count"] += 1
        row["chars"] += int(ln.get("chars") or 0)
        row["line_ids"].append(ln.get("line_id") or ln.get("id"))
        scene = ln.get("scene") or ""
        if scene and scene not in row["scenes"]:
            row["scenes"].append(scene)

    for role in order:
        row = rows[role]
        secs = estimate_secs(row["chars"], chars_per_sec)
        row["estimated_secs"] = secs
        row["estimated_seconds"] = secs
    return [rows[r] for r in order]


def stats(
    lines: list[dict],
    known_roles: Iterable[str] | None = None,
    chars_per_sec: float = DEFAULT_CHARS_PER_SEC,
) -> dict:
    """统计信息，给界面上的预览卡片用。"""
    valid = [
        ln for ln in lines
        if (ln.get("ok") and not ln.get("disabled")
            and not ln.get("is_scene") and not ln.get("ignored"))
    ]
    disabled = [ln for ln in lines if ln.get("disabled")]
    scene_lines = [ln for ln in lines if ln.get("is_scene")]
    comments = [ln for ln in lines if ln.get("kind") == "comment"]
    actions = [ln for ln in lines if ln.get("kind") == "action"]
    invalid = [
        ln for ln in lines
        if (not ln.get("ok") and not ln.get("disabled")
            and not ln.get("is_scene") and not ln.get("ignored"))
    ]
    by_role: dict[str, int] = {}
    role_chars: dict[str, int] = {}
    role_secs: dict[str, float] = {}
    for ln in valid:
        role = ln.get("role")
        if not role:
            continue
        by_role[role] = by_role.get(role, 0) + 1
        role_chars[role] = role_chars.get(role, 0) + int(ln.get("chars") or 0)
    for role, chars in role_chars.items():
        role_secs[role] = estimate_secs(chars, chars_per_sec)

    cast = _cast(lines, known_roles, chars_per_sec)
    missing = [row["role"] for row in cast if row.get("voice_exists") is False]
    total_chars = sum(int(ln.get("chars") or 0) for ln in valid)
    total_secs = estimate_secs(total_chars, chars_per_sec)
    return {
        # total_lines 包含场景标题和禁用行；新增细分字段，旧字段语义不变。
        "total_lines": len(lines),
        "ok_lines": len(valid),
        "bad_lines": len(invalid),
        "disabled_lines": len(disabled),
        "scene_lines": len(scene_lines),
        "comment_lines": len(comments),
        "action_lines": len(actions),
        "scene_count": len({ln.get("scene") for ln in valid if ln.get("scene")}),
        "roles": by_role,
        "role_count": len(by_role),
        "instruct_lines": sum(1 for ln in valid if ln.get("instruct")),
        "chars": total_chars,
        "estimated_secs": total_secs,
        "estimated_seconds": total_secs,
        "chars_per_sec": round(float(chars_per_sec), 2),
        "role_chars": role_chars,
        "role_secs": role_secs,
        "cast": cast,
        "missing_voices": missing,
        "missing_roles": missing,
        "missing_voice_count": len(missing),
    }


def scenes(lines: list[dict]) -> list[dict]:
    """返回场景标题及每个场景包含的台词行 id。"""
    out: list[dict] = []
    current: dict | None = None
    for ln in lines:
        if ln.get("is_scene"):
            current = {
                "id": ln.get("line_id") or ln.get("id"),
                "index": ln.get("index"),
                "source_line": ln.get("source_line"),
                "title": ln.get("scene_title") or ln.get("scene") or "",
                "name": ln.get("scene") or "",
                "line_ids": [],
            }
            out.append(current)
        elif current is not None and ln.get("ok") and not ln.get("disabled"):
            current["line_ids"].append(ln.get("line_id") or ln.get("id"))
    return out


def preview(
    text: str,
    known_roles: Iterable[str] | None = None,
    chars_per_sec: float = DEFAULT_CHARS_PER_SEC,
) -> dict:
    """界面实时预览：解析 + 校验 + 统计，一次给全。"""
    lines = parse(text, chars_per_sec)
    st = stats(lines, known_roles, chars_per_sec)
    return {
        "lines": lines,
        "issues": validate(lines, known_roles),
        "stats": st,
        "cast": st["cast"],
        "scenes": scenes(lines),
        # roles 既保留旧字段，也让前端不必再从 stats.roles 的字典里取 key。
        "roles": all_roles(lines),
    }


def render(lines: list[dict]) -> str:
    """把结构化行还原成规范化的剧本文本（供「一键整理」按钮用）。"""
    out: list[str] = []
    for ln in lines:
        if ln.get("is_scene"):
            out.append(ln.get("raw") or "")
            continue
        if ln.get("disabled"):
            # 禁用不是「删掉」。普通台词规范成 ``# [角色] 台词``；
            # 被禁用的注释 / 动作则保持原意，不能改写成一句台词。
            body = ln.get("disabled_body") or ""
            if ln.get("kind") in ("comment", "action"):
                out.append("# " + body)
            elif ln.get("disabled_role") and ln.get("role"):
                head = f"[{ln['role']}]"
                if ln.get("instruct"):
                    head += f"[{ln['instruct']}]"
                out.append(f"# {head} {ln.get('content', '')}")
            elif body:
                out.append("# " + body)
            continue
        if ln.get("kind") in ("comment", "action"):
            # 注释和动作是合法的阅读结构，不是解析失败。整理时必须原样保留。
            out.append(ln.get("raw") or "")
            continue
        if ln.get("ignored") or not ln.get("ok"):
            continue
        head = f"[{ln['role']}]"
        if ln.get("instruct"):
            head += f"[{ln['instruct']}]"
        out.append(f"{head} {ln['content']}")
    return "\n".join(out)


def all_roles(lines: list[dict]) -> list[str]:
    """按首次出现顺序列出角色。禁用行和场景行不计入。"""
    seen: list[str] = []
    for ln in lines:
        if (ln.get("ok") and not ln.get("disabled")
                and not ln.get("is_scene") and not ln.get("ignored")
                and ln.get("role") not in seen):
            seen.append(ln["role"])
    return seen


def find_line(lines: list[dict], selector: int | str) -> dict | None:
    """按源行号、``L12`` 或 ``line_id`` 找一行。"""
    if selector is None:
        return None
    text = str(selector).strip()
    for ln in lines:
        if text in {
            str(ln.get("index")),
            str(ln.get("source_line")),
            str(ln.get("id")),
            str(ln.get("line_id")),
        }:
            return ln
    return None
