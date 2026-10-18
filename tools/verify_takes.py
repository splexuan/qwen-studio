"""验证「多份候选 / 长文本切段 / 真进度 / 原子落盘」这几块不需要 GPU 的逻辑。

为什么单独一个脚本（而不是塞进 smoke.py）：这几项里有两处是**并发正确性**，
用「跑一次看结果」根本验不出来 —— 一个是 outputs/ 抢文件名，一个是进度回调
的数量与顺序。它们必须被压着跑很多次、在隔离的目录里跑，才可信。

全程把 ``cfg.OUTPUT_DIR`` 指到临时目录，**不在用户的 outputs/ 里留任何东西**
（上一版直接写真目录，留下 100 多个 0 字节文件，把 verify_postproc.py 的
「最近 8 条真实音频」抽样全变成读不出来的空文件）。

跑法：
    runtime\\python\\python.exe tools\\verify_takes.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import numpy as np  # noqa: E402

import config as cfg  # noqa: E402
import core  # noqa: E402
import history  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'  OK  ' if ok else ' FAIL '} {name}{'  — ' + detail if detail else ''}")


def tone(n: int, value: float = 0.1) -> np.ndarray:
    return np.full(n, value, dtype=np.float32)


# ---------------------------------------------------------------------------
# 0. 隔离：所有落盘测试都跑在临时目录里
# ---------------------------------------------------------------------------
_tmp = Path(tempfile.mkdtemp(prefix="qs_takes_"))
_real_out, _real_cand = cfg.OUTPUT_DIR, cfg.CAND_DIR
cfg.OUTPUT_DIR = _tmp / "outputs"
cfg.CAND_DIR = _tmp / "cand"
cfg.OUTPUT_DIR.mkdir()
cfg.CAND_DIR.mkdir()

try:
    # -----------------------------------------------------------------------
    # 1. 进度口径：先报 load，再逐段报 gen，最后一段一条不落
    # -----------------------------------------------------------------------
    seen: list[tuple[int, int, str]] = []
    wav, sr = core._run_pieces(
        ["a", "b", "c"],
        lambda piece, i: (tone(240), 24000),
        0.1,
        lambda d, t, s: seen.append((d, t, s)),
    )
    check("三段各自 240 采样 + 两段 0.1s 静音", len(wav) == 3 * 240 + 2 * 2400, str(len(wav)))
    check("进度从 (0,3,load) 开始", seen[0] == (0, 3, "load"), str(seen[0]))
    check("进度以 (3,3,gen) 收尾", seen[-1] == (3, 3, "gen"), str(seen[-1]))
    check("段序号逐条递增不跳",
          [d for d, _, s in seen if s == "gen"] == [1, 2, 3],
          str([d for d, _, s in seen if s == "gen"]))

    seen1: list[tuple[int, int, str]] = []
    core._run_pieces(["a"], lambda p, i: (tone(10), 24000), 0,
                     lambda d, t, s: seen1.append((d, t, s)))
    check("单段只报 load + gen 两条", seen1 == [(0, 1, "load"), (1, 1, "gen")], str(seen1))

    # 回调抛异常不能把好不容易跑完的生成带崩
    back = core._run_pieces(
        ["a", "b"], lambda p, i: (tone(10), 24000), 0,
        lambda d, t, s: (_ for _ in ()).throw(ValueError("boom")),
    )
    check("进度回调抛异常被吞掉", len(back[0]) == 20, str(len(back[0])))

    # -----------------------------------------------------------------------
    # 2. 抢文件名：8 线程 × 20 次，一个都不能撞
    # -----------------------------------------------------------------------
    names: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(20):
            p = core._reserve("t")
            with lock:
                names.append(p.name)

    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("160 次抢名字互不相同", len(set(names)) == 160, f"取到 {len(set(names))} 个不同的")

    # -----------------------------------------------------------------------
    # 3. save_wav：写成功、且不留 .part 半截文件
    # -----------------------------------------------------------------------
    fn = core.save_wav(tone(480), 24000, "t2")
    check("save_wav 写出了非空文件", (cfg.OUTPUT_DIR / fn).stat().st_size > 0, fn)
    parts = list(cfg.OUTPUT_DIR.glob("*.part*"))
    check("save_wav 不留 .part 半截文件", not parts, str([p.name for p in parts]))
    check("save_wav 文件名带时间戳", fn.startswith("t2_") and fn.endswith(".wav"), fn)

    # 同秒内连续两次不会互相覆盖（老实现靠 exists() 查-写，有竞态）
    a = core.save_wav(tone(240), 24000, "t4")
    b = core.save_wav(tone(480), 24000, "t4")
    check("同一秒两次 save_wav 拿到不同文件", a != b, f"{a} / {b}")
    check("两份内容都在", (cfg.OUTPUT_DIR / a).stat().st_size !=
          (cfg.OUTPUT_DIR / b).stat().st_size,
          f"{(cfg.OUTPUT_DIR / a).stat().st_size} vs {(cfg.OUTPUT_DIR / b).stat().st_size}")

    # -----------------------------------------------------------------------
    # 4. 候选：写进临时目录 → 转正 → 原名消失、新的出现在 outputs/
    # -----------------------------------------------------------------------
    folder = cfg.CAND_DIR / "unitjob"
    cn = core.save_candidate(tone(480), 24000, 1, folder)
    check("候选写进 _cand/<job>/1.wav", (folder / cn).exists(), cn)
    check("候选没进 outputs/", not (cfg.OUTPUT_DIR / cn).exists())

    fn2 = core.promote_wav(folder / cn, "t3")
    check("转正后出现在 outputs/", (cfg.OUTPUT_DIR / fn2).exists(), fn2)
    check("转正后候选侧的原文件已移走", not (folder / cn).exists())
    check("转正的文件名是正式前缀", fn2.startswith("t3_"), fn2)

    # -----------------------------------------------------------------------
    # 5. 内存护栏：超长文本在**开头**就被拒，不是跑到 90% 才炸
    # -----------------------------------------------------------------------
    try:
        core._synth_clone({"text": "字" * 100000, "ref_audio": "x", "xvec_only": True},
                          "Auto", None, "1.7B")
    except RuntimeError as e:
        check("超过 4 小时音频被拒", "内存" in str(e), str(e)[:46])
    else:
        check("超过 4 小时音频被拒", False, "没有抛异常")

    # -----------------------------------------------------------------------
    # 6. 长度闸只留给「不能切段」的声音设计
    # -----------------------------------------------------------------------
    try:
        core.synthesize({"mode": "design", "text": "字" * 3000, "instruct": "x"})
    except RuntimeError as e:
        check("声音设计仍限 2000 字", "声音设计" in str(e), str(e)[:40])
    else:
        check("声音设计仍限 2000 字", False, "没有抛异常")

    # 克隆模式 3000 字不该被长度闸拦。
    # 注意：synthesize 会把业务错误吞成 (…, "error", …) 而不是抛异常，
    # 所以这里比的是**报错内容**——没给音色，它该说「请选择音色」，绝不该说「过长」。
    _, _, status, level, _ = core.synthesize({"mode": "clone", "text": "字" * 3000})
    check("克隆 3000 字不会被长度闸拦",
          level == "error" and "上限" not in status and "过长" not in status,
          status.splitlines()[0][:40] if status else "")

    # -----------------------------------------------------------------------
    # 7. 流式与多份候选互斥（后端也要挡，别只靠前端）
    # -----------------------------------------------------------------------
    ok1, why1 = core.stream_ready({"mode": "clone", "text": "短句。"})
    check("1 份时克隆可以流式", ok1, why1)
    ok2, why2 = core.stream_ready({"mode": "clone", "text": "短句。", "n": 3})
    check("多份候选不允许流式", not ok2 and "候选" in why2, why2)
    ok3, why3 = core.stream_ready({"mode": "clone", "text": "短句。", "n": "2"})
    check("n 传成字符串也能挡住", not ok3, why3)

    # -----------------------------------------------------------------------
    # 8. 语速估计取自真实历史记录
    # -----------------------------------------------------------------------
    cps = history.chars_per_sec()
    check("chars_per_sec 是个合理的语速", 2.0 <= cps <= 9.0, str(cps))
finally:
    cfg.OUTPUT_DIR, cfg.CAND_DIR = _real_out, _real_cand
    shutil.rmtree(_tmp, ignore_errors=True)

check("临时目录已清干净", not _tmp.exists(),
      "" if not _tmp.exists() else str(_tmp))
check("真实 outputs/ 没被这次验证写过", not list(_real_out.glob("t_*")),
      str([p.name for p in _real_out.glob("t_*")][:3]))
check("真实的 web/_cand/ 是空的", not list(_real_cand.glob("*")),
      str([p.name for p in _real_cand.glob("*")][:3]))

bad = [r for r in results if not r[1]]
print(f"\n{'❌' if bad else '✅'} {len(results) - len(bad)}/{len(results)} 通过")
if bad:
    for n, _, d in bad:
        print(f"   ✗ {n} {d}")
raise SystemExit(1 if bad else 0)
