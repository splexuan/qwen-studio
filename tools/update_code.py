"""用从 GitHub 下载的代码 zip 覆盖安装目录 —— 权重和运行时一概不动。

为什么要有这个脚本：整合包 26 G 里只有 1.2 M 是会变的代码，其余是权重和便携运行时，
一辈子不会跟着代码更新。所以「更新」= 把代码那几兆换掉，别的一根手指都不用动。
手动拖拽覆盖会有两个坑，这个脚本把两个都堵上了：

1. **残留旧文件**：新版本里删掉或改名的模块，老文件还躺在那儿，
   有可能还被 import 到 —— 这种问题现场极难查。所以先覆盖、再删掉
   「安装目录里有、而新代码里没有」的那些文件。
2. **删到手**：只删代码目录（app / tools / web/static）和那几个文档/启动器。
   `voices/` `outputs/` `web/_uploads/` `web/_preview/` `models/` `runtime/`
   `vendor/` 一律不碰 —— 里面是你的音色和权重，删了就是重录/重下。

用法（都在项目根目录 / 集成包根目录下）：
    runtime\\python\\python.exe tools\\update_code.py                    # 自动找 zip
    runtime\\python\\python.exe tools\\update_code.py --zip D:\\下载\\x.zip

或者直接双击根目录的「更新代码.bat」。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---- 会被覆盖的代码部分 ----
CODE_DIRS = ["app", "tools", "web/static"]
CODE_FILES = [
    "web/server.py", "web/api_docs.py",
    "README.md", "操作教程.md",
    "启动程序.bat", "启动程序-局域网.bat", "自检.bat", "打包整合包.bat", "更新代码.bat",
]
# ---- 绝对不碰的东西（列出来是为了让你确认，不是在代码里删）----
KEEP_DIRS = ["voices", "outputs", "models", "runtime", "vendor", "dist",
             "web/_uploads", "web/_preview"]

BIG_ZIP = 200 * 1024 * 1024      # 超过这个大小就不是代码包了，多半是整个整合包


def human(n: float) -> str:
    for u in ("B", "K", "M", "G"):
        if n < 1024 or u == "G":
            return f"{n:.1f}{u}" if u != "B" else f"{n:.0f}B"
        n /= 1024
    return str(n)


def find_zip(arg: str | None) -> Path | None:
    if arg:
        p = Path(arg).expanduser()
        return p if p.is_file() else None
    cands: list[tuple[float, Path]] = []
    for folder in (ROOT, ROOT.parent, Path.home() / "Downloads", Path.home() / "Desktop"):
        if not folder.is_dir():
            continue
        try:
            for f in folder.glob("*.zip"):
                cands.append((f.stat().st_mtime, f))
        except OSError:
            continue
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][1]


def looks_like_code(root: Path) -> bool:
    return (root / "app" / "core.py").is_file() and (root / "web" / "server.py").is_file()


def unzip_to_temp(zp: Path) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="qs_update_"))
    with zipfile.ZipFile(zp) as zf:
        zf.extractall(tmp)
    # GitHub 的仓库 zip 最外层是 <仓库名>-<短 sha>/，要找进去一层
    for cand in (tmp, *[d for d in tmp.iterdir() if d.is_dir()]):
        if looks_like_code(cand):
            return cand
    return tmp


def server_running(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="用 GitHub 下载的代码 zip 覆盖安装目录")
    ap.add_argument("--zip", help="代码 zip 的路径；不给就自动找")
    ap.add_argument("--yes", action="store_true", help="不问就往下走")
    ap.add_argument("--port", type=int, default=7867, help="服务端口，用来判断服务是否在跑")
    args = ap.parse_args()

    print("=" * 60)
    print("  用代码 zip 覆盖更新（权重、运行时、音色库都不动）")
    print("=" * 60)
    print()

    if not looks_like_code(ROOT):
        print(f"[错误] {ROOT} 里没有 app/core.py，看起来不是安装目录。")
        print("       请把本脚本放在 启动程序.bat 同一个目录下运行。")
        return 1

    zp = find_zip(args.zip)
    if not zp:
        print("[错误] 找不到代码 zip。")
        print(f"       请到 GitHub 仓库页 → Code → Download ZIP，"
              f"下载后放到 {ROOT}")
        print("       或显式指定：tools\\update_code.py --zip \"D:\\下载\\xxx.zip\"")
        return 1

    size = zp.stat().st_size
    print(f"  代码包：{zp.name}（{human(size)}）")
    if size > BIG_ZIP:
        print(f"  [提醒] 这个包有 {human(size)}，看起来是**整个整合包**而不是代码包。")
        print("          整合包是给「第一次安装」用的，更新只需要 GitHub 上的代码 zip。")
        if not args.yes and input("          仍然继续？[y/N] ").strip().lower() not in ("y", "yes"):
            print("已取消。")
            return 0

    if server_running(args.port):
        print(f"\n  [注意] {args.port} 端口上有服务在跑。")
        print("         现在覆盖的话，跑着的还是旧代码，必须重启才生效。")
        if not args.yes and input("         继续覆盖？（记得覆盖完重启）[y/N] ").strip().lower() not in ("y", "yes"):
            print("已取消。")
            return 0

    print("\n  正在解压 ...")
    t0 = time.time()
    src = unzip_to_temp(zp)
    if not looks_like_code(src):
        print(f"[错误] {zp.name} 解压后找不到 app/core.py 与 web/server.py，")
        print("       它不像是代码包。多半下成了整个整合包 —— 那种请直接解压覆盖安装目录。")
        return 1

    # ---- 1. 先把新代码盖上去 ----
    new_files: set[str] = set()
    copied = 0
    for rel in CODE_FILES:
        s = src / rel
        if not s.is_file():
            continue
        d = ROOT / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
        new_files.add(rel)
        copied += 1
    for rel in CODE_DIRS:
        base = src / rel
        if not base.is_dir():
            continue
        for s in base.rglob("*"):
            if not s.is_file():
                continue
            r = s.relative_to(src).as_posix()
            d = ROOT / r
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
            new_files.add(r)
            copied += 1
    print(f"  已覆盖 {copied} 个文件")

    # ---- 2. 再删掉「新代码里没有」的老文件（残留的旧模块）----
    removed: list[str] = []
    for rel in CODE_DIRS:
        base = ROOT / rel
        if not base.is_dir():
            continue
        for f in list(base.rglob("*")):
            if not f.is_file():
                continue
            r = f.relative_to(ROOT).as_posix()
            if r not in new_files:
                try:
                    f.unlink()
                    removed.append(r)
                except OSError:
                    pass
    for rel in CODE_FILES:
        if rel == "更新代码.bat":
            continue        # 更新器自己永远留着
        if rel not in new_files and (ROOT / rel).is_file():
            try:
                (ROOT / rel).unlink()
                removed.append(rel)
            except OSError:
                pass
    if removed:
        print(f"  已清掉 {len(removed)} 个新版本里已经不存在的旧文件：")
        for r in removed[:8]:
            print(f"    - {r}")
        if len(removed) > 8:
            print(f"    … 还有 {len(removed) - 8} 个")

    # 顺手清一下 __pycache__，免得新旧字节码混着来
    for pc in ROOT.rglob("__pycache__"):
        if any(str(pc).startswith(str(ROOT / d)) for d in CODE_DIRS):
            shutil.rmtree(pc, ignore_errors=True)

    print(f"\n  用时 {time.time() - t0:.1f}s")
    print("\n  没动过：weights（models/）、便携运行时（runtime/）、ffmpeg（vendor/bin/）、")
    print("          你的音色库（voices/）、生成记录（outputs/）")
    print("\n  下一步：关掉旧服务（如果还开着），双击 启动程序.bat 重新启动。")
    print("  启动后点左下角那枚「运行环境」芯片，最下面一行就是新的代码版本号。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())