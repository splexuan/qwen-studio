"""提交前给代码打一个版本标记：把当前的短 commit 和日期写进 app/buildinfo.py。

为什么需要它：整合包和 GitHub 的 zip 里都**没有 .git**，运行时问 git 只会得到
「不是仓库」。手动覆盖更新之后，「我这套代码现在是哪一版」唯一能看的地方，
就是 app/buildinfo.py 里这一行 —— 它会跟着代码一起被覆盖走。

用法（在项目根目录）：
    runtime\\python\\python.exe tools\\stamp_version.py           # 看当前标记
    runtime\\python\\python.exe tools\\stamp_version.py --write   # 按 git 重新标记
    runtime\\python\\python.exe tools\\stamp_version.py --pack    # 打包时调用

不在 git 仓库里也能用：短 commit 取不到就退回目录里最新的 mtime，
所以「解压出来的整合包」重新盖一次章也不会坏。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "app" / "buildinfo.py"

FALLBACK = "未标记（在项目根目录跑一次 tools\\stamp_version.py --write）"


def git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, timeout=10)
        return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def current() -> str:
    sha = git("rev-parse", "--short", "HEAD")
    if not sha:
        # 没有 .git：退化成「这个文件最近一次被改动的时间」
        try:
            t = time.strftime("%Y-%m-%d %H:%M", time.localtime(TARGET.stat().st_mtime))
            return f"{t}（非 git 工作区）"
        except OSError:
            return FALLBACK
    when = git("log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M")
    # 「有未提交改动」只看**别的**文件：标记文件此刻必然是改状态（不然没必要来标记），
    # 把它算进去的话，按「提交 → 打标记」的惯例跑出来的标记永远带着这四个字。
    dirty = [ln for ln in git("status", "--porcelain").splitlines()
             if ln and not ln.endswith("app/buildinfo.py")]
    return f"{sha} · {when}" + ("（有未提交改动）" if dirty else "")


def write(text: str, pack: bool = False) -> None:
    """按行替换两个常量，其他内容原样保留。"""
    out = []
    for ln in TARGET.read_text(encoding="utf-8").splitlines():
        if ln.startswith("CODE_VERSION ="):
            out.append(f'CODE_VERSION = "{text}"')
        elif ln.startswith("PACK_STAMP ="):
            out.append(f'PACK_STAMP = "{text}"' if pack else 'PACK_STAMP = ""')
        else:
            out.append(ln)
    TARGET.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--write":
        v = current()
        write(v)
        print(f"已标记：{v}")
        return 0
    if mode == "--pack":
        v = current()
        write(v, pack=True)
        print(f"已标记：{v}（含打包戳）")
        return 0
    print("当前标记：")
    for ln in TARGET.read_text(encoding="utf-8").splitlines():
        if ln.startswith(("CODE_VERSION", "PACK_STAMP")):
            print("  " + ln)
    print("\n重新标记：tools\\stamp_version.py --write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())