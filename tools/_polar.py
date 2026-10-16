# -*- coding: utf-8 -*-
"""是不是「模型能力」问题？—— 测指令对模型的推动幅度，而不是测某一条音色。

设计要点：**说话人固定、文本固定，只把指令翻到相反的一极**，看输出被推开多远。
    A 极：咆哮 —— 音量拉满、语速快、句尾撕开、气息粗重
    B 极：低语 —— 音量极小近气声、语速慢、字间发黏、句尾散掉
推动幅度（A 的基频 - B 的基频）越大，说明这个模型越"听得懂"指令。

对照组：同一个模型内部比（说话人不变），所以音色差异不会混进来。
    · clone  （皇帝，仅向量）—— 语音克隆
    · official（uncle_fu，1.7B）—— 官方音色

注意：design（声音设计）不参与这个比较 —— 它的指令描述的是**说话人是谁**，
改指令等于换人，没法在"说话人固定"的前提下比。

每组 6 条，比均值（同参数重复生成的基频标准差约 4 Hz，6 条足够看出 20 Hz 级的差距）。
"""
from __future__ import annotations

import json
import statistics as S
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:7867"
N = 6
TEXT = "你听我说完，这件事不像你想的那样。"

SHOUT = "极度愤怒地咆哮：音量拉到最大接近破音，语速很快，句尾向上撕开，气息粗重"
WHISPER = "极度虚弱地低语：音量很小近乎气声，语速很慢，字与字之间发黏，句尾向下散掉"

MODELS = [
    ("clone", "克隆（皇帝·仅向量）", dict(mode="clone", voice="皇帝", xvec_only=True)),
    ("official", "官方（uncle_fu）", dict(mode="official", speaker="uncle_fu")),
]
POLES = [("A", "咆哮", SHOUT), ("B", "低语", WHISPER)]


def post(p, o):
    r = urllib.request.Request(BASE + p, data=json.dumps(o).encode("utf-8"),
                               headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=60) as f:
        return json.loads(f.read().decode("utf-8"))


def get(p):
    with urllib.request.urlopen(BASE + p, timeout=60) as f:
        return json.loads(f.read().decode("utf-8"))


def run(tag, extra):
    payload = {"text": TEXT, "language": "Auto", "size": "auto", **extra}
    got = []
    for _ in range(N):
        jid = post("/api/jobs", payload)["job"]
        t0 = time.time()
        while True:
            st = get("/api/jobs/" + jid)
            if st.get("state") == "done":
                got.append(st["result"]["file"])
                break
            if st.get("state") == "error":
                print(f"    [{tag}] 失败：{st.get('message')}")
                break
            if time.time() - t0 > 300:
                print(f"    [{tag}] 超时")
                break
            time.sleep(0.4)
    print(f"  [{tag}] {len(got)} 条")
    return {"code": tag, "note": f"{extra.get('label')}", "text": TEXT,
            "instruct": extra.get("instruct", ""), "files": got,
            "model": extra.get("model"), "pole": extra.get("pole")}


out = []
print("=" * 78)
print("指令对模型的推动幅度 —— 说话人固定、文本固定，只翻指令极性")
print("=" * 78)
for mid, mnote, mpar in MODELS:
    for pid, pnote, pins in POLES:
        extra = dict(mpar)
        extra["instruct"] = pins
        extra["model"] = mid
        extra["pole"] = f"{mid}-{pid}"
        extra["label"] = f"{mnote} · {pnote}"
        out.append(run(f"{mid}-{pid}", extra))

Path("tools/_ab/_polar.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print("-> tools/_ab/_polar.json")
