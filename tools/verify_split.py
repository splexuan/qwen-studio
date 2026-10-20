"""验证「长文本切段 / 真进度 / 原子落盘」这几块不需要 GPU 的逻辑。

为什么单独一个脚本（而不是塞进 smoke.py）：这里有两处是**并发正确性**，
用「跑一次看结果」根本验不出来 —— 一个是 outputs/ 抢文件名，一个是进度回调
的数量与顺序。它们必须被压着跑很多次、在隔离的目录里跑，才可信。

全程把 ``cfg.OUTPUT_DIR`` 指到临时目录，**不在用户的 outputs/ 里留任何东西**
（上一版直接写真目录，留下 100 多个 0 字节文件，把 verify_postproc.py 的
「最近 8 条真实音频」抽样全变成读不出来的空文件）。

跑法：
    runtime\\python\\python.exe tools\\verify_split.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "web"))

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:  # noqa: BLE001
    pass

import numpy as np  # noqa: E402

import config as cfg  # noqa: E402
import core  # noqa: E402
import history  # noqa: E402
import script as script_mod  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'  OK  ' if ok else ' FAIL '} {name}{'  — ' + detail if detail else ''}")


def tone(n: int, value: float = 0.1) -> np.ndarray:
    return np.full(n, value, dtype=np.float32)


# ---------------------------------------------------------------------------
# 0. 隔离：所有落盘测试都跑在临时目录里
# ---------------------------------------------------------------------------
_tmp = Path(tempfile.mkdtemp(prefix="qs_split_"))
_real_out = cfg.OUTPUT_DIR
cfg.OUTPUT_DIR = _tmp / "outputs"
cfg.OUTPUT_DIR.mkdir()

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
    seen_with_meta = []
    core._run_pieces(
        ["a"], lambda p, i: (tone(10), 24000), 0,
        lambda d, t, s, m=None: seen_with_meta.append((d, t, s, m)),
        [{"role": "甲", "line": 1, "text": "a"}],
    )
    check("带 metadata 的单段仍只报两条",
          [(d, t, s) for d, t, s, _ in seen_with_meta]
          == [(0, 1, "load"), (1, 1, "gen")], str(seen_with_meta))

    # 回调抛异常不能把好不容易跑完的生成带崩
    back = core._run_pieces(
        ["a", "b"], lambda p, i: (tone(10), 24000), 0,
        lambda d, t, s: (_ for _ in ()).throw(ValueError("boom")),
    )
    check("进度回调抛异常被吞掉", len(back[0]) == 20, str(len(back[0])))

    # -----------------------------------------------------------------------
    # 1b. 进度 metadata：新回调拿到角色/行，旧三参数回调仍然可用
    # -----------------------------------------------------------------------
    meta_seen: list[tuple[int, int, str, dict | None]] = []
    core._run_pieces(
        ["a", "b"],
        lambda p, i: (tone(10), 24000),
        0,
        lambda d, t, s, m=None: meta_seen.append((d, t, s, m)),
        [
            {"role": "甲", "line": 2, "text": "a"},
            {"role": "乙", "line": 3, "text": "b"},
        ],
    )
    check("新进度回调收到 metadata",
          meta_seen[-1][3] == {"role": "乙", "line": 3, "text": "b"},
          str(meta_seen[-1][3]))
    old_seen: list[tuple[int, int, str]] = []
    core._run_pieces(
        ["a"], lambda p, i: (tone(10), 24000), 0,
        lambda d, t, s: old_seen.append((d, t, s)),
        [{"role": "甲", "line": 1, "text": "a"}],
    )
    check("旧三参数进度回调保持兼容",
          old_seen == [(0, 1, "load"), (1, 1, "gen")], str(old_seen))

    # -----------------------------------------------------------------------
    # 1c. gap_secs：默认 / 显式 / 非法值
    # -----------------------------------------------------------------------
    check("gap 默认取 config", core._gap_value(None) == cfg.SCRIPT_GAP_SECS,
          str(core._gap_value(None)))
    check("gap 显式值生效", core._gap_value(0.75) == 0.75, str(core._gap_value(0.75)))
    check("gap 非法值退回默认", core._gap_value("bad") == cfg.SCRIPT_GAP_SECS,
          str(core._gap_value("bad")))
    check("gap 负数被夹到 0", core._gap_value(-1) == 0.0, str(core._gap_value(-1)))
    gapped, _ = core._run_pieces(
        ["a", "b"], lambda p, i: (tone(100), 24000), 0.75,
    )
    check("gap 0.75s 真的插入两段间静音", len(gapped) == 200 + int(24000 * 0.75),
          str(len(gapped)))

    # -----------------------------------------------------------------------
    # 1d. 剧本解析：稳定 id / 场景 / 禁用行 / cast / 统计
    # -----------------------------------------------------------------------
    sample = (
        "=== 第一场 客厅 ===\n"
        "[甲][压低声音] 你好。\n"
        "# [乙] 这句先不念。\n"
        "// [丙] 也不念。\n"
        "> 甲转身离开。\n"
        "场景：走廊\n"
        "[乙] 收到。\n"
        "角色：这种写法仍报错"
    )
    lines = script_mod.parse(sample, chars_per_sec=2.0)
    pv = script_mod.preview(sample, {"甲"}, chars_per_sec=2.0)
    check("剧本行 id 基于源行号", [ln["id"] for ln in lines[:3]] == ["L1", "L2", "L3"],
          str([ln["id"] for ln in lines[:3]]))
    check("场景标题被识别", lines[0]["kind"] == "scene"
          and lines[0]["scene"] == "第一场 客厅", str(lines[0]["scene"]))
    check("第二种场景标题也识别", lines[5]["scene"] == "走廊", str(lines[5]["scene"]))
    check("禁用行不破坏解析", lines[2]["disabled"] and lines[2]["role"] == "乙"
          and not lines[2]["ok"], str(lines[2]))
    check("注释行保留但不合成", lines[3]["kind"] == "comment"
          and lines[3]["ignored"] and not lines[3]["ok"], str(lines[3]))
    check("动作行保留但不合成", lines[4]["kind"] == "action"
          and lines[4]["ignored"] and lines[4]["content"] == "甲转身离开。",
          str(lines[4]))
    check("禁用行不计入校验", all("这句先不念" not in i for i in pv["issues"]),
          " / ".join(pv["issues"]))
    check("统计区分禁用/场景/错误", pv["stats"]["ok_lines"] == 2
          and pv["stats"]["bad_lines"] == 1
          and pv["stats"]["disabled_lines"] == 1
          and pv["stats"]["comment_lines"] == 1
          and pv["stats"]["action_lines"] == 1
          and pv["stats"]["scene_lines"] == 2, str(pv["stats"]))
    check("cast 汇总角色/字数/时长",
          [(r["role"], r["lines"], r["chars"], r["estimated_secs"]) for r in pv["cast"]]
          == [("甲", 1, 3, 1.5), ("乙", 1, 3, 1.5)],
          str([(r["role"], r["lines"], r["chars"], r["estimated_secs"]) for r in pv["cast"]]))
    check("cast 标出缺失音色", pv["stats"]["missing_voices"] == ["乙"],
          str(pv["stats"]["missing_voices"]))
    check("roles 继续保留且不含禁用行", pv["roles"] == ["甲", "乙"], str(pv["roles"]))
    check("scenes 汇总每场的台词 id",
          [(s["title"], s["line_ids"]) for s in pv["scenes"]]
          == [("第一场 客厅", ["L2"]), ("走廊", ["L7"])],
          str([(s["title"], s["line_ids"]) for s in pv["scenes"]]))
    rendered = script_mod.render(lines)
    check("整理保留场景/注释/动作/禁用状态",
          "=== 第一场 客厅 ===" in rendered
          and "// [丙] 也不念。" in rendered
          and "> 甲转身离开。" in rendered
          and "# [乙] 这句先不念。" in rendered
          and "[乙] 收到。" in rendered,
          rendered.replace("\n", " | "))

    synth_calls: list[str] = []
    original_gen = core._gen_clone
    core._gen_clone = lambda text, *a, **k: (
        synth_calls.append(text), (tone(10), 24000))[1]
    try:
        fake_voice = {
            "audio_path": "unused", "ref_text": "", "xvec_only": True,
            "prompt": None,
        }
        original_load = core.voices_mod.load_voice
        core.voices_mod.load_voice = lambda _name: fake_voice
        original_list = core.voices_mod.list_voices
        core.voices_mod.list_voices = lambda: ["甲", "乙"]
        try:
            core.gen_script(sample, "Auto", "1.7B")
        finally:
            core.voices_mod.load_voice = original_load
            core.voices_mod.list_voices = original_list
    finally:
        core._gen_clone = original_gen
    check("合成只念有效台词，跳过注释/动作/禁用/场景",
          synth_calls == ["你好。", "收到。"], str(synth_calls))

    # -----------------------------------------------------------------------
    # 1e. 单句入口的纯参数校验：不能加载模型
    # -----------------------------------------------------------------------
    _real_load = core.load_model

    def _no_load(*_args, **_kwargs):
        raise AssertionError("纯校验不应加载模型")

    core.load_model = _no_load
    try:
        try:
            core.preview_script_line("")
        except RuntimeError as e:
            check("单句入口空文本在加载前被拒", "请先输入" in str(e), str(e)[:40])
        else:
            check("单句入口空文本在加载前被拒", False, "没有抛异常")
        try:
            core.preview_script_line("你好", voice="不存在的音色")
        except RuntimeError as e:
            check("单句入口音色不存在在加载前被拒", "没有" in str(e), str(e)[:40])
        else:
            check("单句入口音色不存在在加载前被拒", False, "没有抛异常")
    finally:
        core.load_model = _real_load

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
    # 4. 内存护栏：超长文本在**开头**就被拒，不是跑到 90% 才炸
    # -----------------------------------------------------------------------
    try:
        core._synth_clone({"text": "字" * 100000, "ref_audio": "x", "xvec_only": True},
                          "Auto", None, "1.7B")
    except RuntimeError as e:
        check("超过 4 小时音频被拒", "内存" in str(e), str(e)[:46])
    else:
        check("超过 4 小时音频被拒", False, "没有抛异常")

    # -----------------------------------------------------------------------
    # 5. 长度闸只留给「不能切段」的声音设计
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
    # 6. 语速估计取自真实历史记录
    # -----------------------------------------------------------------------
    cps = history.chars_per_sec()
    check("chars_per_sec 是个合理的语速", 2.0 <= cps <= 9.0, str(cps))
finally:
    cfg.OUTPUT_DIR = _real_out
    shutil.rmtree(_tmp, ignore_errors=True)

check("临时目录已清干净", not _tmp.exists(),
      "" if not _tmp.exists() else str(_tmp))
check("真实 outputs/ 没被这次验证写过", not list(_real_out.glob("t_*")),
      str([p.name for p in _real_out.glob("t_*")][:3]))

# ---------------------------------------------------------------------------
# 7. 接口文档对账 + 服务端纯 Python 路由
# ---------------------------------------------------------------------------
import api_docs as api_docs_mod  # noqa: E402
import server as server_mod  # noqa: E402

real_routes = {
    (r.path, tuple(sorted(getattr(r, "methods", []) or [])))
    for r in server_mod.app.routes
    if getattr(r, "path", "") in ("/openapi.json",)
    or getattr(r, "path", "").startswith(("/api", "/media"))
}
real_routes = {
    (p, tuple(m for m in ms if m.upper() != "HEAD"))
    for p, ms in real_routes
}
docs = api_docs_mod.render(real_routes)
check("/api/docs 路由对账一致", docs["audit"]["consistent"],
      str(docs["audit"]))
declared = {(e["method"], e["path"]) for e in api_docs_mod.ENDPOINTS}
check("单句试听接口已登记", ("POST", "/api/script/line/preview") in declared)

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server_mod.app)
r = client.post("/api/script/preview", json={
    "text": "=== 第一场 ===\n[甲] 你好。\n# [乙] 不念。",
    "mode": "script",
})
body = r.json()
check("POST /api/script/preview 仍返回 roles",
      r.status_code == 200 and body.get("ok")
      and body.get("roles") == ["甲"], str(body.get("roles")))
check("POST /api/script/preview 返回 cast/scenes",
      body.get("cast") and body.get("scenes")
      and body["stats"]["disabled_lines"] == 1,
      str(body.get("stats", {}).get("disabled_lines")))
r = client.post("/api/script/line/preview", json={"text": ""})
check("POST /api/script/line/preview 空文本不加载模型",
      r.status_code == 400 and not r.json().get("ok"),
      r.json().get("error", ""))

bad = [r for r in results if not r[1]]
print(f"\n{'❌' if bad else '✅'} {len(results) - len(bad)}/{len(results)} 通过")
if bad:
    for n, _, d in bad:
        print(f"   ✗ {n} {d}")
raise SystemExit(1 if bad else 0)
