"""Qwen3-TTS Studio 命令行入口 —— 给人和 AI 共用的一层薄壳。

**为什么要有它**：界面是给人点的，AI 要用则得走 HTTP。但直接手拼 curl 有三个稳定的坑 ——
中文 JSON 在 Windows 控制台会被 GBK 编码弄坏、轮询要自己写循环、错误要自己判 state。
这个脚本把这套流程收成四条子命令，stdout 只输出一行结论（可 `--json`），
进度一律走 stderr —— 所以 `qwen.py say ... | ...` 能直接用。

**它不重复实现任何东西**：所有能力都在 `web/server.py` 的 HTTP 接口后面，
这里只负责「组请求、轮询、把结果说清楚」。

用法：
    python tools\\qwen.py status                     # 服务通不通、权重齐不齐、有多少音色
    python tools\\qwen.py voices                     # 可用音色（含时长、克隆方式、试听地址）
    python tools\\qwen.py speakers                   # 9 个官方说话人
    python tools\\qwen.py say "今天天气不错。" --voice 御姐
    python tools\\qwen.py say --mode script --text -  < 剧本.txt
    python tools\\qwen.py say "你好" --speaker vivian --instruct "轻快一点"
    python tools\\qwen.py say "你好" --design "三十岁女声，偏低，语速稳"
    python tools\\qwen.py record --limit 5

连接哪个服务：
    QWENSTUDIO_BASE=http://127.0.0.1:7867   整个地址（优先）
    QWENSTUDIO_PORT=7867                    只给端口（默认 7867）
    注意本机 http_proxy 会代理 loopback，本脚本**强制不走代理**。

退出码：0 成功；1 失败（服务没起、任务报错、参数不合法）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# loopback 一律直连：本机那份 http_proxy=127.0.0.1:1917 会把 127.0.0.1 的请求
# 也丢给代理，症状是 curl 卡住、urllib 报 502。空 ProxyHandler 是唯一可靠的解法。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = "7867"


class ApiError(RuntimeError):
    """服务端明确拒绝（4xx/5xx 且带 error 说明）。"""


def base() -> str:
    b = (os.environ.get("QWENSTUDIO_BASE") or "").strip()
    if b:
        return b.rstrip("/")
    port = (os.environ.get("QWENSTUDIO_PORT") or DEFAULT_PORT).strip() or DEFAULT_PORT
    return f"http://127.0.0.1:{port}"


def call(method: str, path: str, body: dict | None = None,
         timeout: float = 30.0, raw: bool = False) -> Any:
    url = base() + path
    data = None
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            payload = r.read()
            return payload if raw else json.loads(payload or b"{}")
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            info = json.loads(payload or b"{}")
        except Exception:  # noqa: BLE001
            info = {}
        msg = info.get("error") or payload.decode("utf-8", "replace")[:200] or f"HTTP {e.code}"
        raise ApiError(msg) from None
    except urllib.error.URLError as e:
        raise ApiError(
            f"连不上 {base()}（{e.reason}）。服务起了吗？"
            " 双击「启动程序.bat」，或用 QWENSTUDIO_PORT 指到别的端口。"
        ) from None


def out(text: str = "") -> None:
    print(text)


def info(text: str) -> None:
    """进度 / 说明一律走 stderr：stdout 只留给结果，管道才干净。"""
    print(text, file=sys.stderr, flush=True)


def human_dur(sec: float | int | None) -> str:
    s = float(sec or 0)
    return f"{s:.1f} 秒" if s < 60 else f"{int(s // 60)} 分 {s % 60:.0f} 秒"


def _dw(s: str) -> int:
    """字符串的终端显示宽度：中文 / 全角算两列，ASCII 算一列。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _dw(s))


def one_line(s: str, limit: int = 60) -> str:
    """预览压成一行：正文里的换行会把列表排版整个打散。"""
    flat = " ".join((s or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def pp_text(pp: dict | None) -> str:
    """和后端 postprocess.summary() 同一套口径，但说人话。"""
    if not pp:
        return ""
    if pp.get("skipped") == "stream":
        return "边生成边播，跳过"
    if not pp.get("applied"):
        return "关闭"
    bits = []
    if pp.get("silence"):
        cut = float(pp.get("cut_secs") or 0)
        bits.append(f"剪掉 {cut:.1f}s 静音" if cut > 0.05 else "静音已紧凑")
    if pp.get("normalize"):
        g = float(pp.get("gain_db") or 0)
        bits.append(f"响度 {g:+.1f} dB" if abs(g) >= 0.15 else "响度已达标")
    if pp.get("fade"):
        bits.append("首尾渐变")
    return "，".join(bits) or "无变化"


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_status(a) -> int:
    try:
        bo = call("GET", "/api/bootstrap")
    except ApiError as e:
        if a.json:
            out(json.dumps({"ok": False, "base": base(), "error": str(e)}, ensure_ascii=False))
        else:
            out(f"✘ {e}")
        return 1

    hw = bo.get("hardware") or {}
    ms = bo.get("model_status") or {}
    voices = bo.get("voices") or []
    missing = (bo.get("models") or {}).get("missing") or []
    loaded = ms.get("loaded")
    if a.json:
        out(json.dumps({
            "ok": True, "base": base(),
            "hardware": hw.get("short"), "gpu": bool(hw.get("is_gpu")),
            "default_size": hw.get("default_size"),
            "voices": len(voices), "missing_models": missing,
            "loaded": loaded, "version": bo.get("version"),
            "limits": bo.get("limits"), "defaults": bo.get("defaults"),
        }, ensure_ascii=False))
        return 0

    out(f"服务      {base()}")
    out(f"版本      {bo.get('version') or '未标记'}")
    out(f"硬件      {hw.get('short') or '—'}")
    out(f"权重      {'齐全' if not missing else '缺 ' + '、'.join(missing)}")
    if loaded:
        out(f"驻留      {loaded.get('kind')} · {loaded.get('size')}"
            f"（切换规格会先卸载，6 GB 装不下两套）")
    else:
        out("驻留      未加载（首次生成时要等加载 + 预热 CUDA graph）")
    out(f"音色库    {len(voices)} 个")
    lim = bo.get("limits") or {}
    out(f"单次上限  {lim.get('max_text_chars')} 字")
    return 0


def cmd_voices(a) -> int:
    try:
        r = call("GET", "/api/voices")
    except ApiError as e:
        out(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False) if a.json else f"✘ {e}")
        return 1
    items = r.get("voices") or []
    if a.json:
        out(json.dumps({"ok": True, "voices": [
            {"name": v.get("name"), "duration": v.get("duration"),
             "mode": "xvec" if v.get("xvec_only") else "icl",
             "has_prompt": bool(v.get("has_prompt")),
             "ref_text": v.get("ref_text", ""),
             "url": "/media/voice/" + urllib.parse.quote(v.get("name", ""))}
            for v in items]}, ensure_ascii=False))
        return 0
    if not items:
        out("（音色库是空的：去界面「语音克隆 → 新增音色」存一个，或上传参考音频）")
        return 0
    name_w = max(_dw(v.get("name") or "") for v in items)
    out(f"{len(items)} 个音色（* = 已生成基因，克隆时省一次参考音频编码）")
    for v in items:
        mark = "*" if v.get("has_prompt") else " "
        mode = "仅向量" if v.get("xvec_only") else "完整ICL"
        out(f" {mark} {_pad(v.get('name') or '', name_w)}  {human_dur(v.get('duration')):>8}  {mode}")
    return 0


def cmd_speakers(a) -> int:
    try:
        bo = call("GET", "/api/bootstrap")
    except ApiError as e:
        out(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False) if a.json else f"✘ {e}")
        return 1
    items = bo.get("speakers") or []
    if a.json:
        out(json.dumps({"ok": True, "speakers": items}, ensure_ascii=False))
        return 0
    for s in items:
        out(f" {s.get('key', ''):<10} {s.get('label', '')}")
    out("\n0.6B 的官方精品不支持情感指令；多人对话（[说话人] 前缀分行）也要 1.7B。")
    return 0


def cmd_record(a) -> int:
    try:
        r = call("GET", f"/api/history?limit={int(a.limit)}")
    except ApiError as e:
        out(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False) if a.json else f"✘ {e}")
        return 1
    items = r.get("history") or []
    if a.json:
        out(json.dumps({"ok": True, "history": items}, ensure_ascii=False))
        return 0
    if not items:
        out("（还没有合成记录）")
        return 0
    for it in items:
        out(f"{it.get('ts', '')}  {it.get('mode', ''):<8} "
            f"{human_dur(it.get('duration')):>8}  {it.get('file', '')}")
        out(f"    {one_line(it.get('text', ''))}")
    return 0


def _read_text(raw: str) -> str:
    """``-`` 表示从 stdin 读 —— 长文本（剧本）走命令行参数会被 shell 和编码折腾。"""
    if raw == "-":
        return sys.stdin.read()
    if raw.startswith("@"):
        return Path(raw[1:]).read_text(encoding="utf-8")
    return raw


def _build_payload(a, text: str) -> dict:
    mode = a.mode
    if not mode:
        if a.speaker:
            mode = "official"
        elif a.voice or a.ref_audio:
            mode = "clone"
        elif a.design:
            mode = "design"
        else:
            raise ApiError("要说清楚用哪种方式：--voice（音色库）/ --speaker（官方）/ "
                           "--design（音色描述）/ --ref-audio（临时参考音频）")
    p: dict = {"mode": mode, "text": text, "language": a.lang or "Auto",
               "size": a.size or "auto"}
    if a.voice:
        p["voice"] = a.voice
    if a.speaker:
        p["speaker"] = a.speaker
    if a.ref_audio:
        path = Path(a.ref_audio).expanduser().resolve()
        if not path.exists():
            raise ApiError(f"参考音频不存在：{path}")
        # 走上传接口，服务端才会做转码（m4a/aac/wma 不是 libsndfile 能直接读的）
        p["ref_audio"] = str(path)
        p["ref_text"] = a.ref_text or ""
    if a.xvec_only:
        p["xvec_only"] = True
    elif a.full_icl:
        p["xvec_only"] = False
    instruct = a.instruct or a.design
    if instruct:
        p["instruct"] = instruct
    if a.mode == "design" and not instruct:
        raise ApiError("声音设计必须给 --design \"音色描述\"。")
    if a.mode == "script" and not text.strip():
        raise ApiError("剧本模式需要剧本正文（可 --text - 从 stdin 读）。")

    # 后处理：默认全开，与服务端默认一致；只有显式关才发 false
    if a.no_postprocess:
        p["postprocess"] = False
    else:
        if a.no_silence:
            p["pp_silence"] = False
        if a.no_normalize:
            p["pp_normalize"] = False
        if a.no_fade:
            p["pp_fade"] = False
    if a.keep_raw:
        p["postprocess"] = False
    return p


def cmd_say(a) -> int:
    try:
        # 位置参数与 --text 二选一，--text 优先（写出来更清楚）
        text = _read_text(a.text_opt or a.text or "").strip()
    except OSError as e:
        out(f"✘ 读不到文本：{e}")
        return 1
    if not text:
        out("✘ 文本是空的。传一段文本，或用 --text - 从 stdin 读。")
        return 1

    try:
        payload = _build_payload(a, text)
    except ApiError as e:
        out(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False) if a.json else f"✘ {e}")
        return 1

    # 临时参考音频要先进 _uploads/，服务端只认它自己的文件名
    if a.ref_audio:
        try:
            up = _upload(Path(a.ref_audio))
        except ApiError as e:
            out(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False) if a.json else f"✘ {e}")
            return 1
        payload.pop("ref_audio", None)
        payload["ref_upload"] = up["name"]

    try:
        info("提交中…（{}｜{} 字{}）".format(
            payload["mode"], len(text),
            ("｜" + (payload.get("voice") or payload.get("speaker") or ""))
            if (payload.get("voice") or payload.get("speaker")) else ""))
        r = call("POST", "/api/jobs", payload, timeout=60)
        jid = r.get("job")
        if not jid:
            raise ApiError(str(r.get("error") or "服务端没给任务号"))
        res = _wait(jid, a.timeout)
    except ApiError as e:
        out(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False) if a.json else f"✘ {e}")
        return 1
    except KeyboardInterrupt:
        out("✘ 已中断等待（服务端的任务可能仍在跑，稍后能在合成记录里找到）")
        return 1

    path = Path(a.out).expanduser().resolve() if a.out else (ROOT / "outputs" / (res.get("file") or ""))
    if a.out and res.get("url"):
        try:
            data = call("GET", res["url"], timeout=120, raw=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except (ApiError, OSError) as e:
            info(f"（下载到 {path} 失败：{e}；服务端那份仍在 outputs/）")
            path = ROOT / "outputs" / (res.get("file") or "")

    if a.json:
        out(json.dumps({
            "ok": True, "file": str(path), "url": base() + (res.get("url") or ""),
            "duration": res.get("duration"), "took": res.get("took"),
            "postprocess": res.get("postprocess"),
            "status": res.get("status") or res.get("headline"),
        }, ensure_ascii=False))
    else:
        out(str(path))
        line = f"音频 {res.get('duration')}s"
        if res.get("took"):
            dur = float(res.get("duration") or 0)
            line += f" · 耗时 {res.get('took')}s · RTF {float(res['took'])/dur:.2f}" if dur else ""
        pp = pp_text(res.get("postprocess"))
        if pp:
            line += f" · 后处理 {pp}"
        info(line)
    return 0


def _upload(path: Path) -> dict:
    """multipart 上传（纯标准库，不引入 requests）。"""
    boundary = "----qwenstudio" + str(int(time.time() * 1000))
    ctype = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
             ".flac": "audio/flac", ".ogg": "audio/ogg"}.get(path.suffix.lower(), "application/octet-stream")
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{path.name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode("utf-8")
    body = head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        base() + "/api/upload", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with _OPENER.open(req, timeout=180) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error")
        except Exception:  # noqa: BLE001
            msg = None
        raise ApiError(msg or f"上传失败 HTTP {e.code}") from None


def _wait(jid: str, timeout: float) -> dict:
    """轮询到 done/error。进度写 stderr —— 长任务时调用方（人或 AI）能看到还活着。"""
    t0 = time.time()
    last_msg = ""
    while True:
        st = call("GET", f"/api/jobs/{jid}", timeout=30)
        state = st.get("state")
        el = time.time() - t0
        if state in ("done", "error"):
            if state == "error":
                raise ApiError(st.get("message") or "生成失败")
            return st.get("result") or {}
        msg = (st.get("message") or "").strip()
        if msg and msg != last_msg:
            info(f"  {msg}")
            last_msg = msg
        elif int(el) % 5 == 0:
            info(f"  {st.get('progress', 0):.0%} · 已等 {el:.0f}s")
        if el > timeout:
            raise ApiError(f"等了 {timeout:.0f} 秒还没完成（任务 {jid} 可能仍在跑，"
                           "长剧本请把 --timeout 调大）")
        time.sleep(1.0)


# ---------------------------------------------------------------------------
def _usage_hint() -> str:
    """把本文件 docstring 里的用法段摘出来当 --help 的结尾。
    摘不到就返回空串 —— 帮助信息缺一段，不该让整个命令挂掉。"""
    try:
        return "用法：\n" + __doc__.split("用法：", 1)[1].split("连接哪个服务", 1)[0].rstrip()
    except (IndexError, AttributeError):
        return ""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="qwen.py",
        description="Qwen3-TTS Studio 命令行（给人和 AI 共用）。不带参数时等同 status。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_usage_hint(),
    )
    sub = ap.add_subparsers(dest="cmd")

    for name, fn, help_ in (("status", cmd_status, "服务与权重状态（默认）"),
                            ("voices", cmd_voices, "音色库列表"),
                            ("speakers", cmd_speakers, "官方说话人"),
                            ("record", cmd_record, "最近的合成记录")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
        if name == "record":
            p.add_argument("--limit", type=int, default=10, help="条数，默认 10")
        p.set_defaults(fn=fn)

    p = sub.add_parser("say", help="合成一段语音")
    p.add_argument("text", nargs="?", default="", help="文本；- 表示从 stdin 读，@file 表示读文件")
    # 位置参数之外再给一个 --text：脚本里写 `--text -` 比裸一个 `-` 更好读，
    # 而且和 `--mode script --text -` 这种组合能自然拼到一起
    p.add_argument("--text", dest="text_opt", default="",
                   help="同位置参数；- 表示从 stdin 读，@file 表示读文件")
    p.add_argument("--mode", choices=["clone", "design", "official", "script"],
                   help="生成模式；不给则按 --voice/--speaker/--design 推断")
    p.add_argument("--voice", help="音色库里的名字（clone）")
    p.add_argument("--speaker", help="官方说话人 key，如 vivian（official）")
    p.add_argument("--design", help="音色描述（design）")
    p.add_argument("--instruct", help="情感 / 风格指令（design 时即音色描述）")
    p.add_argument("--ref-audio", dest="ref_audio", help="临时参考音频路径（clone，会自动上传）")
    p.add_argument("--ref-text", dest="ref_text", help="临时参考音频的文本（完整 ICL 必填）")
    p.add_argument("--size", choices=["1.7B", "0.6B", "auto"], help="模型规格，默认 auto")
    p.add_argument("--lang", help="语种，默认 Auto")
    p.add_argument("--xvec-only", dest="xvec_only", action="store_true",
                   help="仅用说话人向量（更快、换语种更干净，但不学参考语调）")
    p.add_argument("--full-icl", dest="full_icl", action="store_true",
                   help="完整 ICL（更贴参考韵律，必须给 --ref-text）")
    p.add_argument("--out", help="把音频下载到这个路径（默认只报 outputs/ 里的位置）")
    p.add_argument("--no-postprocess", dest="no_postprocess", action="store_true",
                   help="整组后处理关掉，拿模型原样")
    p.add_argument("--no-silence", dest="no_silence", action="store_true", help="不剪长静音")
    p.add_argument("--no-normalize", dest="no_normalize", action="store_true", help="不做响度归一化")
    p.add_argument("--no-fade", dest="no_fade", action="store_true", help="不加首尾淡入淡出")
    p.add_argument("--keep-raw", dest="keep_raw", action="store_true",
                   help="同 --no-postprocess（写起来更短的别名）")
    p.add_argument("--timeout", type=float, default=900.0, help="最长等待秒数，默认 900")
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p.set_defaults(fn=cmd_say)
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001  老版本 Python 或重定向到管道时可能不支持
        pass
    ap = build_parser()
    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        # 不带子命令：等同 status。AI 经常就这么裸跑一次探活，
        # 让它也返回「服务没起」而不是打一屏 usage。
        class _Bare:
            json = False
        a = _Bare()
        a.fn = cmd_status
    try:
        return int(a.fn(a) or 0)
    except ApiError as e:
        print(f"✘ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
