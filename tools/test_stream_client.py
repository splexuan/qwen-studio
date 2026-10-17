"""流式端点的端到端测试：量首块到达时间（TTFA）、块间隔、落盘与记录。

为什么不用 curl：TTFA 是「首块真的到客户端手上」的时间，curl 的输出管道
本身有缓冲，量出来的是缓冲区填满的时间，不是网络到达时间。这里逐行读
SSE，每读到一条就记一次时间戳。

用法：先起一个验证实例（别占用户的端口）
    QWENSTUDIO_PORT=7877 runtime\\python\\python.exe web/server.py
再跑：
    runtime\\python\\python.exe tools\\test_stream_client.py 7877
"""

from __future__ import annotations

import base64
import json
import sys
import time

import numpy as np
import requests

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7877
BASE = f"http://127.0.0.1:{PORT}"


def sse(path: str, payload: dict, timeout: float = 300.0) -> dict:
    s = requests.Session()
    s.trust_env = False          # 本机 http_proxy 会代理 loopback，必须关掉
    t0 = time.perf_counter()
    r = s.post(BASE + path, json=payload, stream=True, timeout=timeout)
    if r.status_code != 200:
        return {"http": r.status_code, "body": r.text[:300]}

    marks: list[tuple[float, str]] = []
    chunks: list[np.ndarray] = []
    meta: dict = {}
    done: dict = {}
    error: str = ""
    sr = 24000
    ev = ""

    for raw in r.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        if raw.startswith("event: "):
            ev = raw[7:].strip()
            continue
        if not raw.startswith("data: "):
            continue
        now = time.perf_counter() - t0
        marks.append((now, ev))
        data = json.loads(raw[6:])
        if ev == "meta":
            meta = data
            sr = int(data.get("sr") or sr)
        elif ev == "pcm":
            chunks.append(np.frombuffer(base64.b64decode(data["b"]), dtype=np.float32))
        elif ev == "done":
            done = data
        elif ev == "error":
            error = data.get("message", "")

    total = time.perf_counter() - t0
    pcm_marks = [t for t, e in marks if e == "pcm"]
    audio_s = sum(len(c) for c in chunks) / sr if chunks else 0.0
    gaps = [round((b - a) * 1000) for a, b in zip(pcm_marks, pcm_marks[1:])]
    return {
        "http": 200,
        "meta": meta,
        "chunks": len(chunks),
        "ttfa_ms": round(pcm_marks[0] * 1000) if pcm_marks else None,
        "total_s": round(total, 2),
        "audio_s": round(audio_s, 2),
        "rtf": round(total / audio_s, 3) if audio_s else None,
        "gap_ms_min_max": (min(gaps), max(gaps)) if gaps else None,
        "done": done,
        "error": error,
    }


def main() -> None:
    print("=== [1] 不支持流式的场景应返回 409 + fallback ===")
    print(sse("/api/tts/stream", {"mode": "script", "text": "[甲] 你好。"}))

    print("\n=== [2] 官方精品单人（首次调用含加载与 CUDA graph 捕获）===")
    r1 = sse("/api/tts/stream", {
        "mode": "official", "text": "你好，这是流式测试，请听一下效果。",
        "speaker": "vivian", "language": "Chinese", "size": "0.6B",
    })
    print(json.dumps(r1, ensure_ascii=False, indent=1))

    if r1.get("done", {}).get("url"):
        s = requests.Session()
        s.trust_env = False
        h = s.get(BASE + r1["done"]["url"], timeout=30)
        print(f"\n  下载 {r1['done']['url']} → HTTP {h.status_code}, {len(h.content)} 字节")

    print("\n=== [3] 同参数第二次（模型已热）===")
    r2 = sse("/api/tts/stream", {
        "mode": "official", "text": "你好，这是流式测试，请听一下效果。",
        "speaker": "vivian", "language": "Chinese", "size": "0.6B",
    })
    print(json.dumps(r2, ensure_ascii=False, indent=1))

    print("\n=== [4] 多人对话应被挡掉 ===")
    print(sse("/api/tts/stream", {
        "mode": "official", "text": "[vivian] 你好。\n[ryan] 你也好。",
        "language": "Chinese", "size": "1.7B",
    }))

    # 克隆走的是另一条分支（_resolve_clone_source + 基因预热），必须单独验：
    # 规格不匹配的基因曾经会把 prefill 拼崩（1024 vs 2048），这里正好覆盖修复。
    names = requests.Session()
    names.trust_env = False
    vs = names.get(BASE + "/api/voices", timeout=30).json()
    voice = (vs.get("voices") or vs.get("rows") or [{}])[0].get("name")
    if voice:
        for size in ("0.6B", "1.7B"):
            print(f"\n=== [5] 克隆模式流式 / {voice} / {size} ===")
            r = sse("/api/tts/stream", {
                "mode": "clone", "text": "克隆模式的流式测试，听一下有没有断。",
                "voice": voice, "language": "Chinese", "size": size,
            })
            print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print("\n（音色库为空，跳过克隆分支）")


if __name__ == "__main__":
    main()
