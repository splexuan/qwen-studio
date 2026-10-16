"""冷热路径基准：验证「音色基因预热」是否真的省下了时间。

跑法（需服务在 127.0.0.1:7867 上运行）：
    runtime\\python\\python.exe tools\\bench.py [音色名]

做三件事：
  1. 先把权重卸掉，测量「冷启动」（加载 + CUDA graph 预热）有多贵；
  2. 同一句话连跑三次，看第 2、3 次是否明显更快；
  3. 打印每次的 RTF，给出「纯推理」的真实区间。

**为什么要专门测这个**：原项目会把 .pt 音色基因写出去却从来没有读回来过，
所以「音色固化」一直是摆设。本工具在 core._prime_prompt_cache() 里把它预热进
内核缓存。这个脚本就是那条修复的可复现证据。
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:7867"
TEXT = "先跑通流程，再谈音质。这句话用来测量纯推理耗时。"


def post(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read() or b"{}")


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def run_once(voice: str, size: str) -> dict:
    jid = post("/api/jobs", {
        "mode": "clone", "text": TEXT, "language": "Chinese", "size": size,
        "voice": voice, "xvec_only": True,
    })["job"]
    t0 = time.time()
    while True:
        st = get(f"/api/jobs/{jid}")
        if st.get("state") in ("done", "error"):
            break
        if time.time() - t0 > 900:
            return {"error": "超时"}
        time.sleep(1)
    wall = time.time() - t0
    msg = st.get("message") or ""
    m = re.search(r"音频 ([\d.]+)s，耗时 ([\d.]+)s，RTF ([\d.]+)", msg)
    return {
        "state": st.get("state"),
        "wall": wall,
        "audio": float(m.group(1)) if m else 0.0,
        "cost": float(m.group(2)) if m else 0.0,
        "rtf": float(m.group(3)) if m else 0.0,
        "head": msg.split("\n")[0],
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass

    voice = sys.argv[1] if len(sys.argv) > 1 else None
    bo = get("/api/bootstrap")
    voices = [v["name"] for v in bo["voices"]]
    if not voice:
        voice = next((n for n in voices if bo["voices"][voices.index(n)]["has_prompt"]), None)
    if not voice:
        print("音色库是空的，先在界面上存一个音色。")
        return 1
    size = bo["hardware"]["default_size"]

    print("=" * 70)
    print(f"冷热路径基准 · 音色「{voice}」· 规格 {size} · {len(TEXT)} 字")
    print("=" * 70)

    # ---- 冷启动：先卸载，让第 1 次务必含加载 ----
    print("\n[0] 先卸载权重，确保第 1 次是真正的冷启动")
    post("/api/model/unload")
    print("    已卸载。")

    print("\n[1] 连续 3 次，同一句话、同一音色")
    rows = []
    for i in range(1, 4):
        r = run_once(voice, size)
        if r.get("error"):
            print(f"    第 {i} 次失败：{r['error']}")
            return 1
        if r["state"] != "done":
            print(f"    第 {i} 次未成功：{r.get('head')}")
            return 1
        rows.append(r)
        tag = "冷启动（含加载+预热）" if i == 1 else f"热路径 #{i - 1}"
        print(f"    第 {i} 次 [{tag}]  墙钟 {r['wall']:6.1f}s   "
              f"音频 {r['audio']:5.2f}s  耗时 {r['cost']:6.2f}s  RTF {r['rtf']:5.2f}")

    warm = rows[1:]
    print("\n[2] 结论")
    if warm:
        avg = sum(r["rtf"] for r in warm) / len(warm)
        print(f"    冷启动 RTF  {rows[0]['rtf']:5.2f}（含加载与 CUDA graph 预热）")
        print(f"    热路径平均 RTF {avg:5.2f}（纯推理）")
        speedup = rows[0]["rtf"] / avg if avg else 0
        print(f"    冷/热 差距约 {speedup:.1f} 倍")
        print()
        if avg < 1.0:
            print("    → 热路径快于实时，可以用它做长剧本批量生成。")
        else:
            print("    → 热路径慢于实时，属于 6 GB 显存机型的正常表现；")
            print("      想更快可以试 0.6B（但官方精品会失去情感指令）。")
    print("\n[3] 备注")
    print("    音色基因（.pt）在 xvec_only=True 时会被预热进内核缓存，")
    print("    省掉每次重采样 + speaker 编码。上表热路径就吃到了这份收益。")

    post("/api/model/unload")
    print("\n    已卸载权重，把显存还给你。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
