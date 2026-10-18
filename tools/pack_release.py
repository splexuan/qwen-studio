"""把整个工作台打成一个「解压即用」的整合包。

它做的事只有两件：**挑对文件**、**别把主人的东西带出去**。

要带出去的（缺一个就跑不起来）：
    app/        核心逻辑
    web/        服务 + 前端（只要 server.py / api_docs.py / static/）
    vendor/     ffmpeg 与加速推理内核（都在 config.py 里按相对路径注入）
    runtime/    便携 Python（含 torch，8 G）
    models/     5 套权重（18 G，可用 --no-models 剔掉）
    启动程序.bat / 自检.bat / README.md / 操作教程.md / llms.txt

**绝不带出去的**（这是个人的使用痕迹，不是软件的一部分）：
    voices/     自己录/存下来的音色
    outputs/    生成结果与 history.json
    web/_uploads  web/_preview    界面上传的临时音频、试听缓存
    web/_cand                      多份候选的临时落盘（选中的才进 outputs/）
    .workbuddy/ 项目笔记

用法（在项目根目录）：
    runtime\\python\\python.exe tools\\pack_release.py --list        # 只看清单，不写盘
    runtime\\python\\python.exe tools\\pack_release.py              # 复制成 dist\\ 下的一个目录
    runtime\\python\\python.exe tools\\pack_release.py --no-models  # 轻量包（约 8.7 G）
    runtime\\python\\python.exe tools\\pack_release.py --zip        # 另存成一个 zip
    runtime\\python\\python.exe tools\\pack_release.py --split 3800 # 分卷（每卷 3800 MB）

想拷进移动硬盘就用目录形态；想传网盘再压缩；微信/QQ 那种 2–4 G 的限额只能用 --split。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 顶层要带出去的目录。少一个都跑不起来，所以写死在这儿，不做「自动发现」——
# 自动发现会把 dist/、备份目录之类的一起卷进去。
TOP_DIRS = ["app", "web", "vendor", "runtime", "models", "tools"]

# 顶层单个文件，缺了也能跑，但没有说明文档对方会一脸茫然
# llms.txt 是给 AI/脚本看的操作说明（怎么探活、CLI 怎么调、哪里会炸），
# 跟着包一起发，拿到包的人不管是自己用还是把 AI 接上去，都不用先翻代码。
TOP_FILES = ["启动程序.bat", "启动程序-局域网.bat", "自检.bat", "打包整合包.bat", "更新代码.bat",
             "README.md", "操作教程.md", "llms.txt"]

# 内容一律不带、但**目录要建出来**的：第一次启动时程序自己也会建，
# 建出来是为了让对方解压后一眼看到「音色放这儿、结果出这儿」。
EMPTY_DIRS = ["voices", "outputs", "web/_uploads", "web/_preview", "web/_cand"]

# 任何一层遇到这些目录名就整棵跳过
SKIP_DIR_NAMES = {
    "__pycache__",
    ".workbuddy",     # 项目笔记（含主人的工作记录）
    ".git",
    "dist",           # 自己的产物
    "_ab",            # tools 下的 A/B 实验音频
    "_bench",         # tools 下的基准脚本产物（wav / json）
    "_book",          # tools 下的一次性任务目录：脚本是临时的，正文还是个人创作
    ".venv",
}

# 文件名匹配上就跳过
SKIP_FILE_RE = re.compile(
    r"(\.pyc$|\.log$|^nohup\.out$|^_tmp|^_.*\.png$|^\.DS_Store$|desktop\.ini$)",
    re.IGNORECASE,
)

# 同样是「用户数据」，但它们在 web/ 里面 —— voices/、outputs/ 压根不在 TOP_DIRS 里，
# 所以不用管；这两个得单独挡，否则会把别人上传的参考音频一起打出去。
# （第一版就漏在这儿：清单里 web/ 显示 16.5 M，其中 15 M 是 _uploads。）
SKIP_REL_DIRS = {"web/_uploads", "web/_preview", "web/_cand"}


def human(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


def walk_plan() -> tuple[list[tuple[Path, Path]], int]:
    """返回 [(源文件, 包内相对路径)] 与总字节数。不做任何写盘动作。"""
    items: list[tuple[Path, Path]] = []

    def add(p: Path) -> None:
        items.append((p, p.relative_to(ROOT)))

    for f in TOP_FILES:
        fp = ROOT / f
        if fp.is_file():
            add(fp)
        else:
            print(f"  [警告] 少了 {f}")

    for top in TOP_DIRS:
        base = ROOT / top
        if not base.is_dir():
            print(f"  [警告] 少了目录 {top}/")
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            d = Path(dirpath)
            rel = d.relative_to(ROOT)
            if any(part in SKIP_DIR_NAMES for part in rel.parts):
                dirnames[:] = []
                continue
            rl = rel.as_posix()
            if any(rl == s or rl.startswith(s + "/") for s in SKIP_REL_DIRS):
                dirnames[:] = []
                continue
            dirnames[:] = [x for x in dirnames if x not in SKIP_DIR_NAMES]
            for fn in filenames:
                if SKIP_FILE_RE.search(fn):
                    continue
                add(d / fn)

    total = sum(p.stat().st_size for p, _ in items)
    return items, total


def top_sizes() -> list[tuple[str, int]]:
    """每个顶层项各占多大 —— 先在终端里摊开，比打完包再后悔强。"""
    out = []
    for name in TOP_FILES + TOP_DIRS:
        p = ROOT / name
        if p.is_file():
            out.append((name, p.stat().st_size))
        elif p.is_dir():
            n = 0
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [x for x in dirnames if x not in SKIP_DIR_NAMES]
                rl = Path(dirpath).relative_to(ROOT).as_posix()
                if any(rl == s or rl.startswith(s + "/") for s in SKIP_REL_DIRS):
                    dirnames[:] = []
                    continue
                for fn in filenames:
                    if SKIP_FILE_RE.search(fn):
                        continue
                    try:
                        n += (Path(dirpath) / fn).stat().st_size
                    except OSError:
                        pass
            out.append((name + "/", n))
    return out


def write_notes(dest: Path, include_models: bool, items: list[tuple[Path, Path]]) -> None:
    """给对方留一份能自己看懂的东西 —— 出问题时不至于只能来问你。"""
    readme = f"""Qwen3-TTS Studio · 解压即用
========================================

怎么跑起来
----------
1. 把整个文件夹解压到一个**英文短路径**下，例如  D:\\qwen-studio
   （路径里有中文一般也能跑，但少数杀毒软件和驱动会在这上面出幺蛾子）
2. 双击  启动程序.bat
3. **浏览器会自己打开** http://127.0.0.1:7867
   （它是在确认服务真的起来之后才开的，不会开出「无法连接」的页面。
     不想自动开就先设 QWENSTUDIO_NO_BROWSER=1）
4. 第一次生成会加载权重，慢一次（十几到几十秒）；之后同规格会复用，快很多。

想确认环境行不行，先双击  自检.bat：会把显卡档位、权重是否就位、音色库状态
打出来。缺什么它直接说。

关于显卡
--------
* 有 NVIDIA 显卡 → 自动走 GPU；显存 6 G 以上体验最好。
* 没有独显 / 显存不够 → 自动降级到 CPU，**能跑，但慢**（这是设计好的降级，不是报错）。
* 软件完全离线：权重和推理内核都随包带着，不联网、不下载。

怎么更新
--------
整合包里的**代码**会跟着版本走，权重和运行时一辈子不变，所以更新很轻：

1. 关掉 启动程序.bat
2. 从项目主页下载最新的代码 zip（就是仓库页的 Download ZIP，几百 KB）
3. 双击  更新代码.bat  —— 它把代码盖到安装目录上，并清掉新版本里已经不存在的旧文件。
   你的音色库（voices）、生成记录（outputs）、权重、运行时都不动。
4. 重新双击 启动程序.bat。点左下角那枚「运行环境」芯片，最下面一行就是新的代码版本号。

第一次用建议看  操作教程.md
--------------------------
里面有「怎么写情感指令」「参考音频怎么挑」这些真正影响成品质量的细节，
比界面上的说明详细得多。

常见问题
--------
* 端口被占：先设环境变量再启动，例如  set QWENSTUDIO_PORT=7877
* 杀毒软件拦截：便携版 Python + ffmpeg 容易被误报，加白名单即可。
* 双击 启动程序.bat 一闪而过：说明 Python 运行时没找着，确认 runtime\\ 目录完整。
* 音色库是空的：这是正常的 —— 你自己的音色不会跟着软件走。
  想快速试，可以去「官方音色」页（9 个内置说话人，开箱可用），
  或者照 操作教程.md 里「新增音色」那段存一个自己的。

包里含权重：{"是（5 套齐全）" if include_models else "否（需要自己补 models/ 目录）"}
文件数：{len(items)}
"""
    (dest / "首次运行-必读.txt").write_text(readme, encoding="utf-8", newline="\r\n")

    by_top: dict[str, int] = {}
    for p, rel in items:
        by_top[rel.parts[0]] = by_top.get(rel.parts[0], 0) + p.stat().st_size
    lines = [
        "打包清单",
        "=" * 40,
        f"打包时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"文件数：{len(items)}",
        "",
        "已包含：",
    ]
    for k, v in sorted(by_top.items(), key=lambda x: -x[1]):
        lines.append(f"  {k:<22} {human(v)}")
    lines += [
        "",
        "**未包含**（这些是打包者个人的使用数据，不属于软件本体）：",
        "  voices/            音色库（参考音频 / 参考文本 / 音色基因）",
        "  outputs/           生成结果与合成记录 history.json",
        "  web/_uploads/      界面上传的临时音频",
        "  web/_preview/      官方说话人试听缓存",
        "  web/_cand/         多份候选的临时落盘（选中的才进 outputs/）",
        "  .workbuddy/        项目笔记",
        "",
        "这些目录已经在包里建好了空壳，程序第一次启动会把它们用起来。",
    ]
    (dest / "打包清单.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\r\n")


def make_folder(dest: Path, items: list[tuple[Path, Path]], total: int,
                hardlink: bool = False) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    # 空壳目录也得建出来：程序启动时自己也会建，但解压完一眼能看到
    # 「音色放这儿、结果出这儿」，比翻文档强
    for d in EMPTY_DIRS:
        (dest / d).mkdir(parents=True, exist_ok=True)
    done = 0
    t0 = time.time()
    last = 0.0
    for src, rel in items:
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if hardlink:
            # 只用来「打包后自测」：同一块盘上秒级建出一份 26 G 的副本，不占额外空间。
            # **不能拿去分发** —— 对方拿到的会是断链（文件本体还在你这块盘上）。
            try:
                os.link(src, out)
            except OSError:
                shutil.copy2(src, out)      # 跨盘/不支持硬链接时自动退回真复制
        else:
            shutil.copy2(src, out)
        done += src.stat().st_size
        now = time.time()
        if now - last > 2 or done >= total:
            last = now
            pct = done / total * 100 if total else 100
            speed = done / max(now - t0, 0.001)
            sys.stdout.write(f"\r  复制中 {pct:5.1f}%  {human(done)}/{human(total)}"
                             f"  {human(speed)}/s   ")
            sys.stdout.flush()
    print()


def make_zip(zip_path: Path, items: list[tuple[Path, Path]], total: int, top: str) -> None:
    # compresslevel 用 1：包里大头是 safetensors 和 .dll，压不动多少，
    # 高压缩级别只会让打包从十几分钟变一个小时。
    # 包内多套一层 top/ 目录：解压时不会把二十几个文件和目录直接倒进当前文件夹。
    done = 0
    last = 0.0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        # 空目录 zip 不会自己记，得显式写一条进去
        for d in EMPTY_DIRS:
            zf.writestr(str(Path(top) / d) + "/", "")
        for src, rel in items:
            zf.write(src, arcname=str(Path(top) / rel))
            done += src.stat().st_size
            now = time.time()
            if now - last > 2 or done >= total:
                last = now
                sys.stdout.write(f"\r  压缩中 {done / total * 100:5.1f}%  "
                                 f"{human(done)}/{human(total)}   ")
                sys.stdout.flush()
    print()


def split_file(path: Path, mb: int) -> list[Path]:
    """把 zip 切成若干分卷，并留一个合并脚本。

    不用 zip 的「分卷压缩」是因为那不是标准格式，Windows 自带解压不认识；
    切成裸字节 + 一个 copy /b 合并脚本，对方双击就行，不依赖任何软件。
    """
    part = mb * 1024 * 1024
    parts: list[Path] = []
    with open(path, "rb") as f:
        i = 1
        while True:
            chunk = f.read(part)
            if not chunk:
                break
            p = path.with_name(f"{path.name}.{i:03d}")
            p.write_bytes(chunk)
            parts.append(p)
            print(f"  分卷 {p.name}  {human(len(chunk))}")
            i += 1
    path.unlink()      # 合并脚本会重新拼出来，原件不必留着占地方

    lines = [
        "@echo off",
        "rem 把分卷拼回一个 zip 再解压。GBK + CRLF。",
        "cd /d \"%~dp0\"",
        f"echo 正在合并 {len(parts)} 个分卷...",
        f'copy /b "{parts[0].name}"',
    ]
    for p in parts[1:]:
        lines.append(f'+ "{p.name}"')
    lines += [
        f'"{path.name}" >nul',
        "if errorlevel 1 (",
        "    echo [失败] 合并出错，确认分卷都在同一个目录里。",
        "    pause",
        "    exit /b 1",
        ")",
        "echo 合并完成，开始解压...",
        f'powershell -NoProfile -Command "Expand-Archive -LiteralPath \'{path.name}\' -DestinationPath . -Force"',
        "if errorlevel 1 (",
        "    echo [失败] 自动解压没成功，请手动用解压软件打开 " + path.name,
        "    pause",
        "    exit /b 1",
        ")",
        "echo 解压完成，可以删掉分卷和 " + path.name + " 了。",
        "pause",
    ]
    bat = path.with_name("合并并解压.bat")
    bat.write_bytes("\r\n".join(lines).encode("gbk", "replace"))
    return parts


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="打一个解压即用的整合包")
    ap.add_argument("--name", default=f"qwen-studio-{time.strftime('%Y%m%d')}", help="包名")
    ap.add_argument("--out", default="dist", help="输出目录（默认 dist/）")
    ap.add_argument("--no-models", action="store_true", help="不含权重（省 18 G）")
    ap.add_argument("--no-tools", action="store_true", help="不含 tools/（验证脚本）")
    ap.add_argument("--zip", action="store_true", help="另外压成一个 zip")
    ap.add_argument("--split", type=int, metavar="MB", help="分卷，每卷不超过这么多 MB")
    ap.add_argument("--hardlink", action="store_true",
                    help="用硬链接建副本（秒级、不占空间）—— 只用于打包后自测，不能分发")
    ap.add_argument("--list", action="store_true", help="只打印清单，不写盘")
    ap.add_argument("--yes", action="store_true", help="不确认，直接开干")
    args = ap.parse_args()

    global TOP_DIRS
    if args.no_models:
        TOP_DIRS = [t for t in TOP_DIRS if t != "models"]
    if args.no_tools:
        TOP_DIRS = [t for t in TOP_DIRS if t != "tools"]

    print("=" * 62)
    print("  各部分体积")
    print("=" * 62)
    for name, size in top_sizes():
        mark = ""
        if name.startswith("models/") and args.no_models:
            mark = "  ← 已排除"
        if name.startswith("tools/") and args.no_tools:
            mark = "  ← 已排除"
        print(f"  {name:<22} {human(size):>8}{mark}")

    items, total = walk_plan()
    print()
    print(f"  合计 {len(items)} 个文件，{human(total)}")
    print("  不含：voices/ outputs/ web/_uploads/ web/_preview/ web/_cand/ .workbuddy/（个人数据）")
    if args.list:
        print("\n（--list：没有写任何文件）")
        return 0
    if not args.yes:
        ans = input("\n继续打包？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消。")
            return 0

    out_dir = (ROOT / args.out).resolve()
    pkg_dir = out_dir / args.name
    # 默认包名只带日期，同一天打第二次就会撞上。自动让一位（-2 / -3 …）而不是
    # 直接报错 —— 报错会让人以为打包坏了，其实只是想再打一份。
    if pkg_dir.exists():
        n = 2
        while (out_dir / f"{args.name}-{n}").exists():
            n += 1
        print(f"\n  注意：{args.name} 已存在，这次改用 {args.name}-{n}")
        args.name = f"{args.name}-{n}"
        pkg_dir = out_dir / args.name

    if args.hardlink and (args.zip or args.split):
        print("\n[错误] --hardlink 只用于自测，别跟压缩/分卷一起用。")
        return 1

    print()
    make_folder(pkg_dir, items, total, hardlink=args.hardlink)
    write_notes(pkg_dir, not args.no_models, items)

    if args.zip or args.split:
        zip_path = out_dir / f"{args.name}.zip"
        print(f"\n  压缩到 {zip_path.name} ...")
        make_zip(zip_path, items, total, args.name)
        print(f"  zip：{human(zip_path.stat().st_size)}"
              f"（原始 {human(total)}，压掉 {(1 - zip_path.stat().st_size / total) * 100:.0f}%）")
        if args.split:
            print(f"\n  按每卷 {args.split} MB 切分 ...")
            parts = split_file(zip_path, args.split)
            print(f"  分卷 {len(parts)} 个，合并脚本：合并并解压.bat")
            print(f"  {parts[0].name} 的 sha256：{sha256(parts[0])[:16]}…")
        else:
            print(f"  sha256：{sha256(zip_path)[:16]}…")

    print()
    print("=" * 62)
    print(f"  打包完成：{pkg_dir}")
    if args.split:
        print(f"  要转发的是 {out_dir} 下的分卷 + 合并并解压.bat")
    elif args.zip:
        print(f"  要转发的是 {out_dir / (args.name + '.zip')}")
    else:
        print("  这个是目录形态，直接拷进移动硬盘即可；想压成单文件加 --zip")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
