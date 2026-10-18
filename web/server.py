"""Qwen3-TTS Studio — WebUI 后端（FastAPI）。

本文件是**唯一的界面实现**。刻意没有沿用原整合包的 Gradio：
参考项目（omnivoice-studio）已经踩过这个坑 —— Gradio 会在组件标签、主题变量、
DOM 结构上反复与你抢控制权（哈希类名随版本变、注入的 CSS 选择器被重写），
想把每个细节做到位，成本比自己写前端更高。

分工：
  · 模型调用与音色库读写 —— 直接复用 app/ 下的**模块级函数**，不重复实现
    （core.synthesize / voices.rows / script.preview …）
  · 界面与交互 —— 全部由 web/static/index.html 自持
  · 接口文档 —— 内容在 web/api_docs.py，本文件只把它挂成 GET /api/docs

接口：
    GET  /api/bootstrap                    硬件档位 + 音色列表 + 说话人 + 语种 + 模型状态
    GET  /api/hardware                     硬件详情（排障用）
    POST /api/upload                       上传参考音频到临时目录
    POST /api/upload/url                   从链接拉取参考音频（同样落临时目录）
    POST /api/transcribe                   试转写（可选能力，默认不可用）
    GET  /api/voices                       音色列表（含时长/长度提示/基因状态）
    POST /api/voices                       保存音色（可带裁剪区间，并自动排队抽取音色基因）
    POST /api/voices/{name}/trim           就地裁剪参考音频
    POST /api/voices/{name}/rename         重命名
    POST /api/voices/{name}/text           更新参考文本
    POST /api/voices/{name}/prompt         生成音色基因（.pt）
    POST /api/voices/delete                批量删除
    POST /api/script/preview               剧本实时预览（纯解析，不加载模型）
    POST /api/jobs       GET /api/jobs/{id}  提交合成任务 / 轮询进度
    POST /api/speakers/{name}/preview      官方说话人试听（缓存命中直接给 URL，否则排队生成）
    GET  /api/history                      合成记录
    POST /api/history/delete  /clear       删除 / 清空记录
    GET  /media/{out|upload|voice|preview}/{name}  音频回放
    GET  /api/docs                         接口清单
    GET  /openapi.json                     FastAPI 自动生成的机器可读规格

启动：
    runtime\\python.exe web\\server.py
    或  启动程序.bat
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import api_docs as api_docs_mod  # noqa: E402  —— 接口文档的唯一来源
import asr as asr_mod  # noqa: E402
import buildinfo as buildinfo_mod  # noqa: E402  —— 代码版本标记（打包/覆盖更新后靠它认版本）
import config as cfg  # noqa: E402
import core  # noqa: E402  —— 生成核心（import 不会加载模型）
import fetch as fetch_mod  # noqa: E402  —— 从链接拉参考音频
import history as history_mod  # noqa: E402
import postproc  # noqa: E402  —— 输出后处理（纯 numpy，不加载模型）
import script as script_mod  # noqa: E402
import voices as voices_mod  # noqa: E402

from fastapi import FastAPI, File, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import (  # noqa: E402
    FileResponse, JSONResponse, Response, StreamingResponse,
)

app = FastAPI(title="Qwen3-TTS Studio", docs_url=None, redoc_url=None)

_STATIC = ROOT / "web" / "static"
_INDEX = _STATIC / "index.html"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _safe(name: str) -> str:
    """只取文件名部分，挡住 ../ 之类的目录穿越。"""
    return Path(name or "").name


def _ok(**kw):
    return JSONResponse({"ok": True, **kw})


def _err(msg: str, code: int = 400):
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


# ---------------------------------------------------------------------------
# 首屏
# ---------------------------------------------------------------------------
@app.get("/api/hardware")
def api_hardware():
    p = cfg.profile()
    return {
        "ok": True,
        "profile": p.to_dict(),
        "rows": [{"k": k, "v": v} for k, v in p.rows()],
        "notes": p.notes,
        "short": p.short(),
    }


def _lan_ip() -> str:
    """本机的局域网 IPv4。

    用 UDP "连接" 8.8.8.8 让系统选默认出口 —— **不会真的发出任何包**，
    拿的是默认路由所在那块网卡的地址。比按地址段猜靠谱得多：
    按地址段猜会挑中 WSL / 虚拟网卡的 172.x.x.x，朋友拿那个地址肯定连不上。
    取不到（比如完全离线的机器）就返回空字符串，界面上不显示这一行。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


@app.get("/api/bootstrap")
def api_bootstrap():
    p = cfg.profile()
    missing = cfg.missing_models()
    asr_ok, asr_why = asr_mod.available()
    lan_ip = _lan_ip()
    return {
        "ok": True,
        # 对方是拿 GitHub 的 zip 手动覆盖更新的，这一行是他们唯一能看出
        # 「覆盖成功了没、是不是最新版」的地方，所以放在首屏数据里。
        "version": buildinfo_mod.CODE_VERSION,
        "pack_stamp": buildinfo_mod.PACK_STAMP,
        "hardware": {
            "short": p.short(),
            "rows": [{"k": k, "v": v} for k, v in p.rows()],
            "notes": p.notes,
            "device": p.device,
            # 侧栏芯片要显示「显卡名 · 精度 · 显存」，缺了 device_name 就只能
            # 拼出一个孤零零的「· 4.0 GiB」。这里补上（前端还有 rows 兜底）。
            "device_name": p.device_name,
            "compute_cap": p.compute_cap,
            "dtype": p.dtype,
            "vram_gib": p.vram_gib,
            "default_size": p.default_size,
            "is_gpu": p.is_gpu,
        },
        # 局域网访问：界面「系统设置 → 版本与端点」要把这个地址显示出来，
        # 用户得知道该把哪个地址发给朋友
        "net": {
            "host": cfg.HOST,
            "port": cfg.PORT,
            "lan_url": (f"http://{lan_ip}:{cfg.PORT}" if lan_ip else ""),
            "local_url": f"http://127.0.0.1:{cfg.PORT}",
        },
        "languages": cfg.LANGUAGES,
        "sizes": list(cfg.SIZES),
        "speakers": [{"key": k, "label": v} for k, v in cfg.OFFICIAL_SPEAKERS],
        "voices": voices_mod.rows(),
        "models": {
            "missing": missing,
            "ready": not missing,
            "map": {f"{k}|{s}": cfg.MODEL_DIRS[(k, s)]
                    for k in cfg.KINDS for s in cfg.SIZES if (k, s) in cfg.MODEL_DIRS},
        },
        "asr": {"available": asr_ok, "reason": asr_why},
        "limits": {
            # 只对**不能分段**的模式有效（目前只有声音设计）。clone / official /
            # script 都是按句切段生成的，长度不再有硬上限 —— 前端据 mode 判断。
            "max_text_chars": cfg.MAX_TEXT_CHARS,
            "ref_min_secs": voices_mod.REF_MIN_SECS,
            "ref_good_max_secs": voices_mod.REF_GOOD_MAX_SECS,
            "url_max_mb": cfg.URL_IMPORT_MAX_MB,
            # 长文本预估用的三个数：本机实测语速（中位数）、单段切分长度、
            # 单次能装下的音频时长。前端不许自己写死这几个值 —— 换个档位就全错。
            "chars_per_sec": history_mod.chars_per_sec(),
            "chunk_chars": cfg.profile().script_chunk_chars,
            "max_audio_secs": cfg.MAX_AUDIO_SECONDS,
        },
        "defaults": {
            "xvec_only": cfg.CLONE_XVEC_ONLY,
            "gap_secs": cfg.SCRIPT_GAP_SECS,
            # 输出后处理的默认开关与阈值 —— 界面据此画那一组勾选，
            # 前端不再自己写一遍默认值（两边各写一份迟早会分叉）
            "postprocess": postproc.defaults(),
        },
        "model_status": core.model_status(),
    }


# ---------------------------------------------------------------------------
# 上传与转写
# ---------------------------------------------------------------------------
@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    raw = _safe(file.filename or "ref.wav")
    ext = Path(raw).suffix.lower()
    if ext not in (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wma"):
        return _err(f"不支持的音频格式：{ext or '未知'}")
    dst = cfg.UPLOAD_DIR / f"{uuid.uuid4().hex[:12]}{ext}"
    try:
        with dst.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    except Exception as e:  # noqa: BLE001
        return _err(f"保存上传文件失败：{e}")
    # libsndfile 认不了 m4a / aac / wma 这些容器，而上传框里一直写着支持 ——
    # 所以这里补一道 ffmpeg 转码，让「界面说支持的格式」和「真能用的格式」对齐。
    try:
        dst = voices_mod.ensure_decodable(dst)
    except Exception as e:  # noqa: BLE001
        dst.unlink(missing_ok=True)
        return _err(str(e))
    dur = voices_mod.duration_of_file(dst)
    if dur <= 0:
        dst.unlink(missing_ok=True)
        return _err("这个文件读不出音频，可能不是有效的音频文件。")
    return _ok(name=dst.name, original=raw, duration=dur, url=f"/media/upload/{dst.name}")


@app.post("/api/upload/url")
def api_upload_url(payload: dict):
    """从链接拉取参考音频（界面上「新增音色 → 粘贴音频直链」）。

    落盘位置与返回字段都跟 ``/api/upload`` 一致，所以前端拿到之后走的是
    **同一条**下游：波形裁剪 → 转写 → 保存音色。

    刻意写成同步函数而非 ``async def``：下载是阻塞的，FastAPI 会把它丢进
    线程池，事件循环照常转（还能同时回放音频、看进度）；写成 async 才真会卡住服务。
    """
    try:
        return _ok(**fetch_mod.fetch_reference(payload.get("url") or ""))
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@app.post("/api/transcribe")
async def api_transcribe(payload: dict):
    ok, why = asr_mod.available()
    if not ok:
        return _err(why)
    src = payload.get("upload") or payload.get("voice") or ""
    if payload.get("upload"):
        path = cfg.UPLOAD_DIR / _safe(src)
    else:
        path = voices_mod.path_of(_safe(src), voices_mod.WAV_EXT)
    if not path.exists():
        return _err("找不到要转写的音频。")
    try:
        arr, sr = voices_mod.read_audio(path)
        if payload.get("start") is not None and payload.get("end") is not None:
            arr = voices_mod.trim(arr, sr, float(payload["start"]), float(payload["end"]))
        text = asr_mod.transcribe(arr, sr)
    except Exception as e:  # noqa: BLE001
        return _err(f"转写失败：{e}")
    return _ok(text=text)


# ---------------------------------------------------------------------------
# 音色库
# ---------------------------------------------------------------------------
@app.get("/api/voices")
def api_voices():
    return {"ok": True, "voices": voices_mod.rows()}


@app.post("/api/voices")
async def api_voice_create(payload: dict):
    """保存音色。音频来源三选一：

    * ``upload`` —— 刚上传的文件名（``web/_uploads/``）
    * ``voice``  —— 已有音色名（「另存为」）
    * ``out``    —— ``outputs/`` 里的生成结果文件名（声音设计的「存进音色库」走这条：
      生成出来的那把嗓子复现不了，得直接拿产物当参考音频，不必先复制一份到别处）

    参考文本**必填**：默认走完整 ICL，模型要把「这段音频 + 它说的字」一起喂进去；
    文本空了 ICL 根本没法用，只能退回只取音色的向量模式 —— 那正是「克隆不像」的主因。
    所以这道校验放在入口，而不是等合成时才报错。

    音色基因（.pt）是**可选的加速缓存**，默认不再顺手生成：建一次要加载模型、
    几十秒起步，而省下的只是每次生成的一次 speaker 编码。需要时显式传 ``prompt=true``。
    """
    name = (payload.get("name") or "").strip()
    if not name:
        return _err("请填写音色名。")
    ref_text = (payload.get("ref_text") or "").strip()
    # 「另存为」：音频一模一样，它说的字自然也一模一样 —— 从源音色继承文本。
    # 否则用户只是想改个名字，却被迫把同一段话再逐字抄一遍。
    if not ref_text and payload.get("voice"):
        _src = voices_mod.load_voice(_safe(payload["voice"]))
        if _src:
            ref_text = (_src.get("ref_text") or "").strip()
    if not ref_text:
        return _err("参考文本必填：请逐字照抄这段参考音频里说的话。")
    try:
        source = "upload"
        if payload.get("upload"):
            src = cfg.UPLOAD_DIR / _safe(payload["upload"])
            if not src.exists():
                return _err("上传的音频已失效，请重新上传。")
            arr, sr = voices_mod.read_audio(src)
            if payload.get("start") is not None and payload.get("end") is not None:
                arr = voices_mod.trim(arr, sr, float(payload["start"]), float(payload["end"]))
            audio = (arr, sr)
        elif payload.get("voice"):
            v = voices_mod.load_voice(_safe(payload["voice"]))
            if v is None:
                return _err(f"音色 [{payload['voice']}] 不存在。")
            audio = v["audio_path"]
            source = f"copy:{payload['voice']}"
        elif payload.get("out"):
            out = _safe(payload["out"])
            src = cfg.OUTPUT_DIR / out
            if not src.exists():
                return _err("这条生成结果的音频已经不在 outputs/ 里了，没法再拿它存音色。")
            audio = src
            source = f"out:{out}"
        else:
            return _err("请先上传参考音频、选择一个已有音色，或指定一条生成结果。")

        # 参考文本必填之后，默认就该走完整 ICL（更像本人）。
        # 想只取音色向量（换语种更干净、速度更快）要显式传 xvec_only=true。
        xvec = payload.get("xvec_only")
        if xvec is None:
            xvec = False

        info = voices_mod.save_voice(
            name,
            audio,
            ref_text=ref_text,
            xvec_only=bool(xvec),
            overwrite=bool(payload.get("overwrite")),
            source=source,
        )

        # 基因默认不建：它是缓存不是数据，缺了只是每次多编码一遍参考音频。
        # 要的话显式传 prompt=true，或者之后在音色库里手动「生成音色基因」。
        job = None
        if payload.get("prompt", False):
            job = _new_job("prompt", {"name": info["name"], "size": payload.get("size")})

        return _ok(name=info["name"], replaced=info["replaced"],
                   voices=voices_mod.rows(), job=job)
    except voices_mod.VoiceExistsError as e:
        # 单独带一个 code：界面据此把「已存在」升级成「要不要覆盖」的二次确认，
        # 而不是只能看到一句红字、还得自己回音色库去删。音色名唯一这条规则
        # 由**这里**保证（而不是前端拦一下），前端那层确认只是顺手。
        return JSONResponse(
            {"ok": False, "error": str(e), "code": "name_exists",
             "name": voices_mod.sanitize_name(name)},
            status_code=409,
        )
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@app.post("/api/voices/{name}/trim")
async def api_voice_trim(name: str, payload: dict):
    try:
        r = voices_mod.trim_voice(_safe(name), float(payload.get("start", 0)),
                                  float(payload.get("end", 0)))
        return _ok(**r, voices=voices_mod.rows())
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@app.post("/api/voices/{name}/rename")
async def api_voice_rename(name: str, payload: dict):
    try:
        new = voices_mod.rename_voice(_safe(name), payload.get("new_name") or "")
        return _ok(name=new, voices=voices_mod.rows())
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@app.post("/api/voices/{name}/text")
async def api_voice_text(name: str, payload: dict):
    try:
        voices_mod.update_ref_text(_safe(name), payload.get("ref_text") or "")
        return _ok(voices=voices_mod.rows())
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@app.post("/api/voices/{name}/star")
async def api_voice_star(name: str, payload: dict):
    """收藏 / 取消收藏。只影响列表置顶，不参与合成。"""
    try:
        if not voices_mod.set_starred(_safe(name), bool((payload or {}).get("starred"))):
            return _err("音色不存在。", 404)
        return _ok(voices=voices_mod.rows())
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


@app.post("/api/voices/{name}/prompt")
async def api_voice_prompt(name: str, payload: dict | None = None):
    """生成音色基因（.pt）。需要加载模型，耗时较长，因此走后台线程 + 轮询。"""
    n = _safe(name)
    if voices_mod.load_voice(n) is None:
        return _err(f"音色 [{n}] 不存在。")
    jid = _new_job("prompt", {"name": n, "size": (payload or {}).get("size")})
    return _ok(job=jid)


@app.post("/api/voices/delete")
async def api_voice_delete(payload: dict):
    done = voices_mod.delete_voices(payload.get("names") or [])
    return _ok(deleted=done, voices=voices_mod.rows())


# ---------------------------------------------------------------------------
# 模型管理（界面「系统设置 → 模型与显存」用）
# ---------------------------------------------------------------------------
@app.get("/api/model")
def api_model():
    """当前驻留的模型 / 显存占用 / 缺失的权重。

    以前只有下面两个 POST，状态完全靠它们自己回传 —— 生成完之后前端那份
    BOOT.model_status 就过期了（权重明明在显存里，界面还写「当前未加载」）。
    系统设置页每次进来都拉一次这个接口，以服务端为准。
    """
    return _ok(status=core.model_status(), missing=cfg.missing_models())


@app.post("/api/model/load")
async def api_model_load(payload: dict):
    """主动加载一套权重。用来把「首次生成的那十几秒」提前到用户可等待的时机。"""
    kind = (payload.get("kind") or "").lower()
    size = (payload.get("size") or "").strip()
    if kind not in cfg.KINDS:
        return _err(f"未知的功能：{kind}")
    if size in ("", "auto"):
        size = cfg.profile().default_size
    if size not in cfg.SIZES:
        return _err(f"未知的规格：{size}")
    try:
        core.load_model(kind, size)
    except Exception as e:  # noqa: BLE001
        return _err(f"加载失败：{e}")
    return _ok(status=core.model_status())


@app.post("/api/model/unload")
async def api_model_unload():
    core.unload_model()
    return _ok(status=core.model_status())


# ---------------------------------------------------------------------------
# 剧本预览（不加载模型）
# ---------------------------------------------------------------------------
@app.post("/api/script/preview")
async def api_script_preview(payload: dict):
    text = payload.get("text") or ""
    mode = (payload.get("mode") or "script").lower()
    known = voices_mod.list_voices() if mode == "script" else cfg.SPEAKER_NAMES
    try:
        return _ok(**script_mod.preview(text, known),
                   roles=script_mod.all_roles(script_mod.parse(text)))
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


# ---------------------------------------------------------------------------
# 合成任务
# ---------------------------------------------------------------------------
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()

# 输出文件名的前缀。四个模式共用一套，别让文件名和 mode 对不上。
_TAG = {"clone": "clone", "design": "design", "official": "official", "script": "script"}


def _job_set(jid: str, **kw) -> None:
    with _JOBS_LOCK:
        _JOBS.setdefault(jid, {}).update(kw)


def _new_job(kind: str, params: dict) -> str:
    jid = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[jid] = {
            "id": jid, "kind": kind, "state": "queued",
            "message": "排队中…", "progress": 0.0, "result": None,
            "created": time.time(),
        }
        # 只保留最近 100 个任务，避免长跑后内存无限增长
        if len(_JOBS) > 100:
            for k in sorted(_JOBS, key=lambda x: _JOBS[x]["created"])[:-100]:
                _JOBS.pop(k, None)
    threading.Thread(target=_run_job, args=(jid, kind, params), daemon=True).start()
    return jid


def _prog_cb(jid: str, lo: float, hi: float):
    """把 core 的「第 i/n 段」映射成任务的整体进度。

    区间由这里给：core 只知道「这一段生成完了」，不知道这一单从开跑到收尾
    还有多少 —— 线性映射只做一次，百分比的口径就全在一处，前端不用关心是实现细节。
    """
    def cb(done: int, total: int, stage: str = "gen") -> None:
        frac = (done / total) if total else 0.0
        if stage == "load":
            msg = "正在准备模型…"
        elif total <= 1:
            msg = "正在生成音频…"
        else:
            msg = f"第 {done}/{total} 段"
        _job_set(jid, progress=round(lo + (hi - lo) * frac, 3), message=msg,
                 pieces=int(total), piece=int(done))
    return cb


def _run_job(jid: str, kind: str, p: dict) -> None:
    try:
        if kind == "prompt":
            _job_set(jid, state="running", message="正在加载模型并抽取音色基因…", progress=0.15)
            name = voices_mod.build_prompt(p["name"], p.get("size"))
            _job_set(jid, state="done", progress=1.0, message=f"音色基因已生成：{name}.pt",
                     result={"name": name, "voices": voices_mod.rows()})
            return

        if kind == "preview":
            _job_set(jid, state="running", progress=0.15,
                     message="正在准备模型 —— 首次试听要加载权重，通常十几到几十秒。")
            path = core.build_speaker_preview(p["speaker"], p["size"])
            _job_set(jid, state="done", progress=1.0, message="试听已就绪",
                     result={"speaker": p["speaker"], "url": f"/media/preview/{path.name}"})
            return

        # kind == "synth"
        # 直调 API 可以省掉 language / size，服务端会兜底（Auto / 按档位挑）。
        # 但合成记录里存的是原始 payload —— 省了就记成空串，回看时不知道当时用的哪套。
        # 这里先归一化成「实际生效的值」，synthesize 拿到的输入完全等价。
        p["language"] = p.get("language") or "Auto"
        p["size"] = cfg.resolve_size(p.get("size"))

        _job_set(jid, state="running", progress=0.02,
                 message="正在准备模型 —— 首次使用该规格要加载权重并预热 CUDA graph，"
                         "通常十几到几十秒。")
        t_synth = time.time()
        tag = _TAG.get(p.get("mode", "clone"), "audio")

        # 长文本在这里已经是「切成几十段逐段跑」了（core.split_text），
        # 进度回调会一段一段报上来 —— 前端看到 pieces > 1 就显示「第 i/N 段」。
        wav, sr, status, level, pp_stats = core.synthesize(p, _prog_cb(jid, 0.04, 0.90))
        # 生成耗时：合成记录和结果卡显示的是同一个数（之前记录里根本没有这项，
        # 结果卡的耗时是前端掐的，两边对不上）
        took = round(time.time() - t_synth, 2)
        if level == "error":
            _job_set(jid, state="error", message=status, progress=1.0)
            history_mod.add({**p, "status": "error", "file": "", "duration": 0, "took": took})
            return

        _job_set(jid, message="正在写入音频…", progress=0.92)
        fname = core.save_wav(wav, sr, tag)
        dur = round(len(wav) / sr, 2) if sr else 0.0
        head = status.splitlines()[0] if status else ""
        # 后处理统计跟着记录一起存：事后回看「这条为什么比上次短 1 秒」
        # 只能靠它，结果卡上那行小字是不会留在记录里的。
        history_mod.add({**p, "status": level, "file": fname, "duration": dur,
                         "took": took, "postprocess": pp_stats})

        _job_set(
            jid, state="done", progress=1.0,
            message=status,
            result={
                "file": fname, "url": f"/media/out/{fname}",
                "duration": dur, "level": level,
                "took": took,            # 与合成记录同一个数
                "headline": head,
                "postprocess": pp_stats,
            },
        )
    except Exception as e:  # noqa: BLE001
        _job_set(jid, state="error", message=f"{type(e).__name__}: {e}", progress=1.0)


@app.post("/api/jobs")
async def api_job_create(payload: dict):
    mode = (payload.get("mode") or "clone").lower()
    if mode not in cfg.KINDS:
        return _err(f"未知的生成模式：{mode}")
    if not (payload.get("text") or "").strip():
        return _err("请先输入要合成的文本。")

    # 临时上传的参考音频：前端只知道 web/_uploads/ 下的文件名（那是它能拿到的最小信息），
    # 而 core 需要绝对路径。翻译在这一层做，前端就不必知道服务器的目录结构。
    up = payload.get("ref_upload")
    if up:
        p = cfg.UPLOAD_DIR / _safe(up)
        if not p.exists():
            return _err("临时上传的参考音频已失效，请重新上传。")
        payload["ref_audio"] = str(p)

    jid = _new_job("synth", payload)
    return _ok(job=jid)


@app.get("/api/jobs/{jid}")
def api_job_status(jid: str):
    job = _JOBS.get(jid)
    if not job:
        return _err("任务不存在或已过期。", 404)
    return {"ok": True, **job}


# ---------------------------------------------------------------------------
# 流式合成（边生成边播）
# ---------------------------------------------------------------------------
def _sse(event: str, data: dict) -> bytes:
    return (
        f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    ).encode("utf-8")


@app.post("/api/tts/stream")
async def api_tts_stream(payload: dict):
    """边生成边推音频块（SSE + base64 float32 PCM）。

    为什么不用「提交任务 + 轮询」：轮询的最小粒度是「发一次、等一次」，
    用户听到第一声至少晚一个来回。这里一条连接从提交到收尾，首块到了就能播。

    生成是阻塞的 GPU 计算，不能直接跑在事件循环里（会把轮询、静态资源全卡住），
    所以放在一个后台线程里，通过队列把块扔回协程。
    """
    # 归一化和「提交任务」走同一套兜底，否则合成记录里两种任务的参数写法会不一样
    payload["language"] = payload.get("language") or "Auto"
    payload["size"] = cfg.resolve_size(payload.get("size"))

    up = payload.get("ref_upload")
    if up:
        p = cfg.UPLOAD_DIR / _safe(up)
        if not p.exists():
            return _err("临时上传的参考音频已失效，请重新上传。")
        payload["ref_audio"] = str(p)

    ok, why = core.stream_ready(payload)
    if not ok:
        # 409 而不是 400：前端据此「安静地退回普通任务」，不该弹成硬错误
        return JSONResponse({"ok": False, "error": why, "fallback": True}, status_code=409)

    async def gen():
        q: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        stop = threading.Event()

        def worker() -> None:
            stream = core.synthesize_stream(payload)
            try:
                for kind, pl in stream:
                    if stop.is_set():
                        break
                    loop.call_soon_threadsafe(q.put_nowait, (kind, pl))
            except Exception as e:  # noqa: BLE001
                loop.call_soon_threadsafe(
                    q.put_nowait, ("error", {"message": f"{type(e).__name__}: {e}"})
                )
            finally:
                # 显式 close：客户端半路断开时，core 的 with _GPU_LOCK 要立刻释放，
                # 不能等垃圾回收 —— 否则下一单会一直卡在等锁。
                stream.close()
                loop.call_soon_threadsafe(q.put_nowait, ("__end__", None))

        threading.Thread(target=worker, name="tts-stream", daemon=True).start()

        parts: list[np.ndarray] = []
        sr = core.STREAM_SAMPLE_RATE
        try:
            while True:
                kind, pl = await q.get()
                if kind == "__end__":
                    break

                if kind == "meta":
                    sr = int(pl.get("sr") or sr)
                    yield _sse("meta", pl)

                elif kind in ("pcm", "gap"):
                    arr = np.asarray(pl, dtype=np.float32).reshape(-1)
                    if arr.size:
                        parts.append(arr)
                        yield _sse(
                            "pcm",
                            {"b": base64.b64encode(arr.tobytes()).decode("ascii")},
                        )

                elif kind == "error":
                    yield _sse("error", pl)
                    return

                elif kind == "done":
                    if not parts:
                        yield _sse("error", {"message": "没有生成任何音频。"})
                        return
                    # 收尾：落盘 + 记一条合成记录 —— 流式听到的这一条也要能下载、能回看
                    wav = np.concatenate(parts)
                    dur = round(len(wav) / sr, 2) if sr else 0.0
                    took = pl.get("took")
                    tag = {"clone": "clone", "design": "design",
                           "official": "official", "script": "script"}.get(
                        payload.get("mode", "clone"), "audio")
                    fname = core.save_wav(wav, sr, tag)
                    status = core.status_line(wav, sr, took or 0.0)
                    # 流式不做后处理（音频已经边生成边播出去了），
                    # 但记录里必须留痕 —— 否则回看时看到「后处理开着」却拿到未处理的音频，
                    # 会以为功能坏了。skipped 这个标记就是给这件事用的。
                    skipped = {"applied": False, "skipped": "stream"}
                    history_mod.add({**payload, "status": "ok", "file": fname,
                                     "duration": dur, "took": took,
                                     "postprocess": skipped})
                    yield _sse("done", {
                        "file": fname, "url": f"/media/out/{fname}",
                        "duration": dur, "took": took, "level": "ok",
                        "status": status,
                        "headline": status.splitlines()[0] if status else "",
                        "pieces": pl.get("pieces"),
                        "postprocess": skipped,
                    })
                    return
        finally:
            stop.set()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # 别让任何中间层攒着再发
        },
    )


# ---------------------------------------------------------------------------
# 官方说话人试听
# ---------------------------------------------------------------------------
@app.post("/api/speakers/{name}/preview")
async def api_speaker_preview(name: str, payload: dict | None = None):
    """试听官方说话人的样例音频。

    样例得真跑一次模型才有，所以**缓存命中就直接给 URL**，没命中才排后台任务；
    响应里 url 与 job 必有其一，前端两条路都要认。
    """
    key = _safe(name).lower()
    if key not in cfg.SPEAKER_NAMES:
        return _err(f"没有这个官方说话人：{name}")
    size = cfg.resolve_size((payload or {}).get("size"))
    path = core.speaker_preview_path(key, size)
    if path.exists():
        return _ok(speaker=key, url=f"/media/preview/{path.name}", cached=True)
    return _ok(speaker=key, job=_new_job("preview", {"speaker": key, "size": size}))


# ---------------------------------------------------------------------------
# 合成记录
# ---------------------------------------------------------------------------
@app.get("/api/history")
def api_history(limit: int = 0):
    return {"ok": True, "history": history_mod.list_all(limit)}


@app.post("/api/history/delete")
async def api_history_delete(payload: dict):
    return _ok(removed=history_mod.delete(payload.get("ids") or []))


@app.post("/api/history/clear")
async def api_history_clear():
    return _ok(removed=history_mod.clear())


# ---------------------------------------------------------------------------
# 媒体
# ---------------------------------------------------------------------------
@app.get("/media/out/{name}")
def media_out(name: str):
    p = cfg.OUTPUT_DIR / _safe(name)
    if not p.exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(str(p), media_type="audio/wav")


@app.get("/media/upload/{name}")
def media_upload(name: str):
    p = cfg.UPLOAD_DIR / _safe(name)
    if not p.exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(str(p))


@app.get("/media/voice/{name}")
def media_voice(name: str):
    p = voices_mod.path_of(_safe(name), voices_mod.WAV_EXT)
    if not p.exists():
        raise HTTPException(404, "音色不存在")
    # no-cache = 每次都回来问一句，不是「不缓存」。
    # 音色是可以原地换音频的（删除重传 / 编辑音频），URL 不变，
    # 不强制校验的话浏览器会一直放旧的那一份。
    return FileResponse(str(p), media_type="audio/wav",
                        headers={"Cache-Control": "no-cache"})


@app.get("/media/preview/{name}")
def media_preview(name: str):
    p = cfg.PREVIEW_DIR / _safe(name)
    if not p.exists():
        raise HTTPException(404, "试听文件不存在")
    return FileResponse(str(p), media_type="audio/wav")


# ---------------------------------------------------------------------------
# 文档与前端
# ---------------------------------------------------------------------------
@app.get("/api/docs")
def api_docs(request: Request):
    """接口清单：以 api_docs.py 为内容来源，并与真实路由对账。

    只把「本工具自己登记的业务路由」纳入对账范围。FastAPI 内置的
    /openapi.json 也一并纳入 —— 它在 api_docs.ENDPOINTS 里是声明过的，
    若不收进来会被误报成「文档写了但路由不存在」。
    """
    real = {(r.path, tuple(sorted(getattr(r, "methods", []) or [])))
            for r in app.routes
            if getattr(r, "path", "") in ("/openapi.json",)
            or getattr(r, "path", "").startswith(("/api", "/media"))}
    # 去掉 FastAPI 自动为 GET 路由附加的 HEAD
    real = {(p, tuple(m for m in ms if m.upper() != "HEAD")) for p, ms in real}
    # 示例里的地址按「调用方实际访问的那个」填 —— 换端口 / 换机器 / 走反代都对
    base = str(request.base_url).rstrip("/")
    return {"ok": True, **api_docs_mod.render(real, base=base)}


@app.get("/")
def index():
    if not _INDEX.exists():
        return JSONResponse({"error": "找不到 web/static/index.html"}, status_code=500)
    return FileResponse(str(_INDEX), media_type="text/html; charset=utf-8")


@app.get("/favicon.ico")
def favicon():
    # 必须返回**没有 body** 的 204。早先这里写的是 JSONResponse({}, 204)：
    # 它带了个 b"{}" 的 body 和 Content-Length: 2，而 204 按规范不允许有 body，
    # httptools 会直接抛 "Response content longer than Content-Length"。
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def _preload_weights() -> None:
    """后台把最常用的一套权重先加载好。

    为什么放在启动时：加载 + 捕获 CUDA graph 要十几到二十秒，而这段时间用户
    只能盯着一句「首次使用该规格要加载权重」。启动后在后台跑掉，第一次点生成
    就是热的。

    **必须跑在后台线程**：加载是长时间阻塞调用，还会抢 _GPU_LOCK；放在主线程上
    端口会一直不通 —— run_web.bat 开的那个页签等到的是「无法连接」。

    规格默认取本机档位（6 GiB 上是 1.7B），kind 默认 clone：
    克隆与剧本工坊共用同一套 Base 权重，是使用频率最高的那一套。

    关掉：``set QWENSTUDIO_PRELOAD=0``（要把这台机器腾出来跑别的程序时）。
    换一套：``QWENSTUDIO_PRELOAD_KIND=official`` / ``QWENSTUDIO_PRELOAD_SIZE=0.6B``。
    """
    if os.environ.get("QWENSTUDIO_PRELOAD", "1").strip().lower() in ("0", "false", "no", "off"):
        print("  [预加载] 已关闭（QWENSTUDIO_PRELOAD=0）", flush=True)
        return
    kind = (os.environ.get("QWENSTUDIO_PRELOAD_KIND") or "clone").strip().lower()
    try:
        size = cfg.resolve_size(os.environ.get("QWENSTUDIO_PRELOAD_SIZE") or None)
        t0 = time.time()
        print(f"  [预加载] 后台加载 {kind} / {size} …", flush=True)
        # 只 load_model 不够：CUDA graph 是第一次 generate 时才捕获的，
        # 那一段十几秒不预热掉，用户点第一单照样要等。
        core.load_model(kind, size)
        t_load = time.time() - t0
        t_warm = core.warmup(kind, size)
        print(f"  [预加载] 就绪：加载 {t_load:.1f}s + 预热 {t_warm:.1f}s"
              f"（总 {time.time() - t0:.1f}s）", flush=True)
    except Exception as e:  # noqa: BLE001  预加载失败不该让服务起不来
        print(f"  [预加载] 失败：{type(e).__name__}: {e}", flush=True)
        print("  [预加载] 生成时会照常重试一次，不影响使用。", flush=True)


def _preload_async() -> None:
    threading.Thread(target=_preload_weights, name="preload", daemon=True).start()


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass

    import uvicorn

    p = cfg.profile()
    missing = cfg.missing_models()
    print("=" * 66)
    print("  Qwen3-TTS Studio")
    # 监听 0.0.0.0 时不能把「0.0.0.0」当地址印出来 —— 那是通配符，
    # 复制到浏览器里打不开。换成真内网 IP（取不到就明说，别印个废地址）。
    if cfg.HOST in ("0.0.0.0", "::"):
        lan = _lan_ip()
        print(f"  本机      http://127.0.0.1:{cfg.PORT}")
        print(f"  局域网    {f'http://{lan}:{cfg.PORT}' if lan else '（没取到内网 IP，用 ipconfig 查）'}")
    else:
        print(f"  本机      http://{cfg.HOST}:{cfg.PORT}")
    print(f"  档位  {p.short()}")
    print(f"  音色  {len(voices_mod.list_voices())} 个 · 输出 {cfg.OUTPUT_DIR}")
    if missing:
        print(f"  [警告] 缺少权重：{', '.join(missing)}")
    for n in p.notes:
        print(f"  · {n}")
    print("=" * 66)

    _preload_async()
    uvicorn.run(app, host=cfg.HOST, port=cfg.PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
