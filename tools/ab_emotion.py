# -*- coding: utf-8 -*-
"""对照实验：情绪指令到底有没有在起作用，卡在「模式」还是卡在「写法」。

打的是**正在运行的服务**的接口（默认 7867），产物落在 outputs/，
所以跑完可以直接在「合成记录」里听、也可以和用户自己生成的对比。

    stage1  2x2：模式（完整 ICL / 仅向量）× 写法（标签词 / 描述句），外加空指令基线
    stage2  换载体：同一句描述句分别交给 clone（两种模式）/ 声音设计 / 官方
    stage3  更极限的台词：标签 vs 描述 vs 哭腔写法
    stage4  2x2：标点（原样 / 加 ——！……）× 指令（空 / 描述句）—— 结论最硬的一组

**每格只跑 1 条。要做统计就把 stage4 重复跑几遍、把文件攒起来**：
同参数重复生成的音高起伏标准差有 2.5 半音，单条对比读出来全是对比噪声。
先用量噪声底：

    runtime\\python\\python.exe tools\\measure_prosody.py --spread 样本1 样本2 ...

再出试听页：

    runtime\\python\\python.exe tools\\emotion_report.py tools\\ab_stage4.json ^
        --out 情绪对照.html --title "..." --notes 说明.txt
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

PORT = (sys.argv[2] if len(sys.argv) > 2 else "7867").strip() or "7867"
BASE = f"http://127.0.0.1:{PORT}"

TEXT = "你们这满朝文武，把朕当傻子吗？一个个的连哄带骗，真以为朕不敢杀你们吗？"

# 「怎么念」而不是「什么情绪」：音量 · 语速 · 重音落点 · 句尾走向 · 气息
DESC = ("愤怒地质问：音量很大近似吼叫，语速偏快但每个字都咬得很重，"
        "句尾向上扬起，气息粗重，停顿短促")
LABEL = "生气"

STAGE1 = [
    ("A", "基线：完整ICL + 空指令", dict(mode="clone", voice="皇帝", xvec_only=False, instruct="")),
    ("B", "描述句：完整ICL", dict(mode="clone", voice="皇帝", xvec_only=False, instruct=DESC)),
    ("C", "描述句：仅向量", dict(mode="clone", voice="皇帝", xvec_only=True, instruct=DESC)),
    ("D", "标签词：完整ICL", dict(mode="clone", voice="皇帝", xvec_only=False, instruct=LABEL)),
    ("E", "标签词：仅向量", dict(mode="clone", voice="皇帝", xvec_only=True, instruct=LABEL)),
]

STAGE2 = [
    ("F", "clone 完整ICL + 描述句（1/2）", dict(mode="clone", voice="皇帝", xvec_only=False, instruct=DESC)),
    ("G", "clone 完整ICL + 描述句（2/2）", dict(mode="clone", voice="皇帝", xvec_only=False, instruct=DESC)),
    ("H", "clone 仅向量 + 描述句（1/2）", dict(mode="clone", voice="皇帝", xvec_only=True, instruct=DESC)),
    ("I", "clone 仅向量 + 描述句（2/2）", dict(mode="clone", voice="皇帝", xvec_only=True, instruct=DESC)),
    ("J", "声音设计 + 描述句", dict(mode="design", instruct=DESC)),
    ("K", "官方 uncle_fu + 描述句", dict(mode="official", speaker="uncle_fu", instruct=DESC)),
]

# ---- 第三段：换一句更极限的台词，「悲痛绝望」这类要拆成两层 ----
# 悲痛 ≠ 大喊。喊解决「大」，哭腔/气息断续/句尾垮掉才解决「悲痛」。
# 只在音量上加码，结果就是一条平着喊的直线（能量动态从 81.8 dB 掉到 55.2 dB）。
DESPAIR_TEXT = "叶凡，我饶不了你，我做鬼也不会放过你的！"
DESPAIR_LABEL = "悲痛绝望的大喊"

# 五个旋钮：音量 · 语速 · 重音落点 · 句尾走向 · 气息
DESPAIR_DESC = ("绝望的嘶喊：音量拉到最大接近破音，声音发抖带着哭腔，语速时快时慢，"
                "中段一字一顿咬碎，最后气息越来越弱、句尾垮下去")
# 更侧重「悲痛」而不是「大喊」：把哭腔和呼吸放在音量前面
DESPAIR_WEEP = ("强忍着哭腔的绝望呼喊：声音发闷发抖，气息断续像喘不上气，"
                "音量很大但已经劈了，句尾向下拖长后突然收住")

STAGE3 = [
    ("L", "标签词：完整ICL（复刻现状）", dict(mode="clone", voice="皇帝", xvec_only=False, instruct=DESPAIR_LABEL)),
    ("M", "标签词：仅向量", dict(mode="clone", voice="皇帝", xvec_only=True, instruct=DESPAIR_LABEL)),
    ("N", "描述句：完整ICL", dict(mode="clone", voice="皇帝", xvec_only=False, instruct=DESPAIR_DESC)),
    ("O", "描述句：仅向量", dict(mode="clone", voice="皇帝", xvec_only=True, instruct=DESPAIR_DESC)),
    ("P", "哭腔句：仅向量", dict(mode="clone", voice="皇帝", xvec_only=True, instruct=DESPAIR_WEEP)),
    ("Q", "声音设计 + 描述句", dict(mode="design", instruct=DESPAIR_DESC)),
]
for _c in STAGE3:
    _c[2].setdefault("text", DESPAIR_TEXT)

# ---- 第四段：2x2「标点 × 指令」—— 目前结论最硬的一组 ----
# 教训：这一段的每格都要跑 5 条以上。同参数重复生成的音高起伏标准差有 2.5 半音，
# 单条对比读出来全是对比噪声。看基频（方差小），别只看音高起伏（各条件都 22~24 半，几乎不动）。
PUNC_PLAIN = "叶凡，我饶不了你，我做鬼也不会放过你的！"
# 破折号/省略号/感叹号给停顿与拖腔留出位置 —— 标点本身就是一套韵律记号
PUNC_RICH = "叶凡——！我饶不了你！我做鬼……也不会放过你的！"

STAGE4 = [
    ("W1", "文本原样 + 空指令", dict(mode="clone", voice="皇帝", xvec_only=True,
                                     text=PUNC_PLAIN, instruct="")),
    ("W5", "文本加停顿标点 + 空指令", dict(mode="clone", voice="皇帝", xvec_only=True,
                                           text=PUNC_RICH, instruct="")),
    ("W2", "文本原样 + 标签词", dict(mode="clone", voice="皇帝", xvec_only=True,
                                     text=PUNC_PLAIN, instruct=DESPAIR_LABEL)),
    ("W3", "文本原样 + 描述句", dict(mode="clone", voice="皇帝", xvec_only=True,
                                     text=PUNC_PLAIN, instruct=DESPAIR_DESC)),
    ("W4", "文本加停顿标点 + 描述句", dict(mode="clone", voice="皇帝", xvec_only=True,
                                           text=PUNC_RICH, instruct=DESPAIR_DESC)),
]
# 产物：tools/ab_stage4.json —— 用 emotion_report.py 把它汇总成试听页


def post(path: str, obj: dict) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(obj).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def run_one(code: str, note: str, extra: dict) -> dict | None:
    payload = {"text": TEXT, "language": "Auto", "size": "auto", **extra}
    t0 = time.time()
    r = post("/api/jobs", payload)
    if not r.get("ok"):
        print(f"  [{code}] 起任务失败：{r}")
        return None
    jid = r["job"]
    while True:
        st = get("/api/jobs/" + jid)
        if st.get("state") == "done":
            fname = (st.get("result") or {}).get("file")
            print(f"  [{code}] {note:<32} -> {fname}  ({time.time()-t0:.1f}s)")
            return {"code": code, "note": note, "file": fname, **extra}
        if st.get("state") == "error":
            print(f"  [{code}] 失败：{st.get('message')}")
            return None
        if time.time() - t0 > 300:
            print(f"  [{code}] 超时")
            return None
        time.sleep(0.5)


def run_stage(name: str, cases: list, out_file: str) -> list:
    texts = {c[2].get("text", TEXT) for c in cases}
    print("=" * 78)
    print(f"{name}")
    for t in texts:
        print(f"  句子：{t}")
    print("=" * 78)
    out = []
    for code, note, extra in cases:
        r = run_one(code, note, extra)
        if r:
            out.append(r)
    Path(out_file).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> {out_file}（{len(out)} 条）")
    return out


def main() -> None:
    which = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    if which in ("all", "stage1"):
        run_stage("第一段 2x2：模式 × 写法", STAGE1, "tools/ab_stage1.json")
    if which in ("all", "stage2"):
        run_stage("第二段：换载体", STAGE2, "tools/ab_stage2.json")
    if which in ("all", "stage3"):
        run_stage("第三段：悲痛绝望的嘶喊（标签 vs 描述 vs 哭腔）", STAGE3, "tools/ab_stage3.json")
    if which in ("all", "stage4"):
        run_stage("第四段 2x2：标点 × 指令（每组跑 1 条，要统计请重复跑）",
                  STAGE4, "tools/ab_stage4.json")
    print()
    print("下一步：python tools/measure_prosody.py 逐条量，或直接看 outputs/ 和合成记录。")


if __name__ == "__main__":
    main()
