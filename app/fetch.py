"""Qwen3-TTS Studio — 从链接拉取参考音频。

为什么要有它：参考音频经常不在本地 —— 在素材站的直链上、在别人发来的地址里。
而浏览器的上传对话框只肯交出**本地文件**，所以这类音频过去必须先手动下载一遍
再拖进来：两次操作，中间还得自己记得存到哪了。

职责刻意做小：本模块只负责「把远程音频落成一个本地文件，并回报与
``POST /api/upload`` **完全一致**的字段」。裁剪、转写、入库、抽基因一概不重做 ——
那些下游只认 ``web/_uploads/`` 里的文件名，多一个来源它们一行都不用改。

只允许 http / https，体积 / 超时 / 重定向次数都有上限。失败一律抛
``RuntimeError``，消息是给用户看的中文，逐条说清是网络不通、404、防盗链、
还是贴成了网页 —— 而不是丢一个 traceback 出来。
"""

from __future__ import annotations

import re
import urllib.parse
import uuid
from pathlib import Path

import requests

import config as cfg
import voices as voices_mod

# Content-Type → 扩展名。只列音频；认不出的留给内容嗅探 ——
# libsndfile 是看文件头的，扩展名不对照样能读（见 voices.ensure_decodable）。
_CT_EXT = {
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
    "audio/vnd.wave": ".wav",
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/x-mp3": ".mp3",
    "audio/flac": ".flac", "audio/x-flac": ".flac",
    "audio/ogg": ".ogg", "application/ogg": ".ogg", "audio/opus": ".ogg",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/m4a": ".m4a",
    "audio/aac": ".aac", "audio/x-aac": ".aac",
    "audio/x-ms-wma": ".wma", "audio/wma": ".wma",
}
# 与 web/server.py 的上传白名单保持一致，两个入口认同一批格式
ALLOWED_EXT = (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wma")

# 指向本机时绕开环境变量里的代理。本机就带着 http_proxy=127.0.0.1:xxxx 这种设置，
# 而 requests 只在 **NO_PROXY 里写了** 才会自动跳过 —— 而指向 127.0.0.1 的链接
# 只可能是用户自己在跑的服务，经代理去访问它永远没有意义。
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
_REDIRECT_LIMIT = 5
_CHUNK = 64 * 1024
_UA = "Qwen3-TTS-Studio/1.0"


def _norm(url: str) -> str:
    """把用户粘进来的东西收敛成一个 http(s) URL，不合法就抛。"""
    s = (url or "").strip()
    if not s:
        raise RuntimeError("请先填写音频链接。")
    if len(s) > 2048:
        raise RuntimeError("链接太长了，可能粘贴时带了别的内容。")
    p = urllib.parse.urlparse(s)
    if p.scheme.lower() not in ("http", "https"):
        raise RuntimeError("链接要以 http:// 或 https:// 开头（只支持这两种）。")
    if not p.netloc:
        raise RuntimeError("链接不完整，缺少域名部分。")
    return s


def _pick_ext(ct: str, url: str) -> str:
    """决定落盘时用什么扩展名。"""
    # 先看服务器说的类型 —— 它比链接后缀可信（很多 CDN 用无后缀的地址）
    ext = _CT_EXT.get(ct)
    if ext:
        return ext
    tail = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if tail in ALLOWED_EXT:
        return tail
    # 两边都没线索：按 wav 落盘。真要是 m4a 之类，ensure_decodable 会接手转码。
    return ".wav"


def _original_name(headers, url: str, ext: str) -> str:
    """给界面看的「原文件名」——只用来显示和当默认音色名。"""
    cd = headers.get("content-disposition") or ""
    m = re.search(r"filename\*?=(?:UTF-8''|[\"'])?([^\"';]+)", cd, re.I)
    if m:
        name = Path(urllib.parse.unquote(m.group(1).strip())).name
        if name:
            return name
    base = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
    return base or f"audio{ext}"


def _friendly_status(code: int) -> str:
    if code == 401 or code == 403:
        return f"对方拒绝了这次访问（{code}）—— 多半要登录，或者这个地址有防盗链，换个直链试试。"
    if code == 404:
        return "链接返回 404，文件已经不在了。"
    return f"链接返回 {code}，没有拿到音频。"


def download_audio(url: str, dest_dir: str | Path | None = None,
                   max_mb: float | None = None, timeout: float | None = None) -> dict:
    """下载 ``url`` 到 ``dest_dir``，返回 ``{"path", "original", "size"}``。

    全程流式落盘，不把音频读进内存；一旦超过体积上限就删掉半成品再抛错。
    """
    s = _norm(url)
    dest = Path(dest_dir or cfg.UPLOAD_DIR)
    dest.mkdir(parents=True, exist_ok=True)
    mb = cfg.URL_IMPORT_MAX_MB if max_mb is None else max_mb
    cap = int(mb * 1024 * 1024)
    wait = cfg.URL_IMPORT_TIMEOUT if timeout is None else timeout

    session = requests.Session()
    session.max_redirects = _REDIRECT_LIMIT
    if urllib.parse.urlparse(s).hostname in _LOOPBACK_HOSTS:
        session.trust_env = False
    session.headers.update({"User-Agent": _UA, "Accept": "*/*"})

    try:
        r = session.get(s, stream=True, timeout=(min(wait, 10.0), wait),
                        allow_redirects=True)
    except requests.exceptions.SSLError:
        raise RuntimeError("HTTPS 证书校验没过，这个地址要么证书过期，要么被中间人挡了。")
    except requests.exceptions.TooManyRedirects:
        raise RuntimeError("跳转次数太多，链接可能是个死循环。")
    except requests.exceptions.Timeout:
        raise RuntimeError(f"连接超时（{wait:.0f} 秒），对方没有响应。")
    except requests.exceptions.ConnectionError:
        raise RuntimeError("连不上这个地址 —— 检查一下网络，或者链接有没有写错。")
    except requests.exceptions.MissingSchema:
        raise RuntimeError("链接要以 http:// 或 https:// 开头（只支持这两种）。")

    with r:
        if r.status_code >= 400:
            raise RuntimeError(_friendly_status(r.status_code))

        ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        # 最常见的错：把「打开音频的那个网页」的地址复制了过来。
        # 在写盘之前就拦住，免得往 _uploads/ 丢一个 .wav 结尾的 HTML。
        if ct.startswith("text/") or ct in ("application/json", "application/xml"):
            raise RuntimeError(
                f"这个链接指向的是网页（{ct}），不是音频文件。"
                "请换成音频直链 —— 在播放器上右键「复制音频地址」那种。"
            )

        ext = _pick_ext(ct, s)
        original = _original_name(r.headers, s, ext)
        dst = dest / f"{uuid.uuid4().hex[:12]}{ext}"
        got = 0
        try:
            with dst.open("wb") as f:
                for chunk in r.iter_content(_CHUNK):
                    if not chunk:
                        continue
                    got += len(chunk)
                    if got > cap:
                        raise RuntimeError(
                            f"文件超过 {mb:g} MB，已中断下载。"
                            "参考音频 3–12 秒就够，先把文件裁小再传吧。"
                        )
                    f.write(chunk)
        except BaseException:
            dst.unlink(missing_ok=True)   # 半成品不留
            raise

    if got == 0:
        dst.unlink(missing_ok=True)
        raise RuntimeError("链接下下来是空文件。")

    return {
        "path": dst,
        "original": original,
        "size": got,
    }


def fetch_reference(url: str, dest_dir: str | Path | None = None) -> dict:
    """下载 → 保证能解码 → 回报与 ``/api/upload`` 同一套字段。

    返回 ``{"name", "original", "duration", "url", "source"}``：
    ``url`` 是回放地址，``source`` 只用于界面上标一句「来自链接」。
    字段名与 ``api_upload`` 对齐是刻意的 —— 前端拿到之后走的是同一条下游，
    不必为「链接来的音频」分叉。
    """
    got = download_audio(url, dest_dir)
    try:
        path = voices_mod.ensure_decodable(got["path"])
        dur = voices_mod.duration_of_file(path)
    except Exception:
        # 解不开就把下下来的东西一并清掉，别在临时目录里攒垃圾
        got["path"].unlink(missing_ok=True)
        raise
    if dur <= 0:
        path.unlink(missing_ok=True)
        raise RuntimeError("下下来了，但解不出音频 —— 这个地址多半不是音频文件。")
    return {
        "name": path.name,
        "original": got["original"],
        "duration": dur,
        "url": f"/media/upload/{path.name}",
        "source": url.strip(),
    }
