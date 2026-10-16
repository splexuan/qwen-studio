"""一次性冒烟测试：把 qwen-studio 的所有 HTTP 接口过一遍。

纯标准库，不依赖 requests。跑法：
    runtime\\python\\python.exe tools\\smoke.py
运行前请先让服务在 127.0.0.1:7867 上跑着。

想测别的端口（例如 7867 已被占用、另起了一个测试实例），用环境变量指过去：
    set QWENSTUDIO_PORT=7877 && runtime\\python\\python.exe tools\\smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 默认打 7867；测试实例另起了别的端口时用 QWENSTUDIO_PORT 指过去。
_PORT = os.environ.get("QWENSTUDIO_PORT", "7867").strip() or "7867"
BASE = f"http://127.0.0.1:{_PORT}"
FAILS: list[str] = []

# 音色库里几个音色的**真实**参考文本（来自原项目 personas/*.txt）。
# 用途有二：一是给转写测试当真值，二是测试若改动了这些数据，收尾时能还原回去。
VOICE_TRUTH = {
    "御姐": "小弟弟，有兴趣陪姐姐喝一杯吗？",
    "韩立": "嘿嘿，为了此丹，大晋元婴中期修士都接连陨落，你说它效用如何？",
}


def call(method: str, path: str, body: dict | None = None, raw: bool = False):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            payload = r.read()
            if raw:
                return r.status, r.headers.get("content-type", ""), payload
            return r.status, r.headers.get("content-type", ""), json.loads(payload or b"{}")
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, e.headers.get("content-type", ""), json.loads(payload or b"{}")
        except Exception:  # noqa: BLE001
            return e.code, "", payload.decode("utf-8", "replace")


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "OK  " if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f"  → {detail}" if detail else ""))
    if not cond:
        FAILS.append(label)


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass

    print("=" * 70)
    print("Qwen3-TTS Studio · 接口冒烟测试")
    print("=" * 70)

    # ---- 1. 首屏 ----
    print("\n[1] 首屏")
    st, _, hw = call("GET", "/api/hardware")
    check("GET /api/hardware", st == 200 and hw.get("ok"), hw.get("short", ""))
    st, _, bo = call("GET", "/api/bootstrap")
    check("GET /api/bootstrap", st == 200 and bo.get("ok"))
    check("  · 权重齐全", not bo["models"]["missing"], str(bo["models"]["missing"]))
    # 只要求「迁移进来的那 6 个还在」，不要求总数恰好多少 ——
    # 用户随时会自己加音色（也删过迁入的），写死总数会让测试无谓地红。
    check("  · 音色已导入", len(bo["voices"]) >= 6, f"{len(bo['voices'])} 个")
    check("  · 9 个官方说话人", len(bo["speakers"]) == 9)
    check("  · 11 个语种", len(bo["languages"]) == 11)

    # ---- 2. 静态页 ----
    print("\n[2] 前端与文档")
    st, ct, html = call("GET", "/", raw=True)
    check("GET /", st == 200 and b"Qwen3-TTS" in html, f"{len(html)} bytes")
    check("  · 七个视图", html.count(b'id="view-') >= 7, f"{html.count(b'id=\"view-')} 个")
    st, _, _ = call("GET", "/favicon.ico")
    check("GET /favicon.ico", st == 204)
    st, _, oa = call("GET", "/openapi.json")
    check("GET /openapi.json", st == 200 and "paths" in oa, f"{len(oa.get('paths', {}))} 条路径")
    st, _, docs = call("GET", "/api/docs")
    audit = docs.get("audit", {})
    check("GET /api/docs 对账一致", audit.get("consistent") is True,
          f"{audit.get('declared')} 声明 / {audit.get('real')} 实际")
    check("  · 5 段代码示例", len(docs.get("examples", [])) == 5, str(len(docs.get("examples", []))))

    # ---- 3. 媒体 ----
    print("\n[3] 媒体回放")
    name = urllib.parse.quote("御姐")
    st, ct, blob = call("GET", f"/media/voice/{name}", raw=True)
    check("GET /media/voice/御姐", st == 200 and len(blob) > 1000,
          f"{len(blob)} bytes, {ct}")
    st, _, _ = call("GET", "/media/voice/" + urllib.parse.quote("不存在的音色"), raw=True)
    check("GET 不存在的音色 → 404", st == 404)

    # ---- 4. 剧本解析 ----
    print("\n[4] 剧本解析（不加载模型）")
    st, _, pv = call("POST", "/api/script/preview", {
        "text": "[韩立][压低了声音] 此丹的药力，恐怕不止于此。\n"
                "[不存在的角色] 你是说……\n"
                "角色：台词这种写法",
        "mode": "script",
    })
    check("POST /api/script/preview (script)", st == 200 and pv.get("ok"))
    check("  · 3 行全部计入", pv["stats"]["total_lines"] == 3, str(pv["stats"]))
    # ok_lines 的口径是「格式能解析」，不是「角色在库里」——
    # 库成员校验是独立一层，由 issues 报告。两者刻意分开：
    # 格式错没法救，角色不在库只是会被跳过。
    check("  · 2 行格式正确 / 1 行格式错", pv["stats"]["ok_lines"] == 2 and pv["stats"]["bad_lines"] == 1)
    check("  · 报出 2 个问题", len(pv["issues"]) == 2, " / ".join(pv["issues"])[:64])
    check("  · 其中 1 条是「不在库里」",
          sum("不在库里" in i for i in pv["issues"]) == 1)
    check("  · 解析出 2 个角色", pv["roles"] == ["韩立", "不存在的角色"], str(pv["roles"]))

    st, _, pv2 = call("POST", "/api/script/preview", {
        "text": "[vivian][轻快地] 你终于来了。\n[uncle_fu][低沉] 路上耽搁了一会儿。",
        "mode": "official",
    })
    check("POST /api/script/preview (official)", st == 200 and pv2["stats"]["ok_lines"] == 2)
    check("  · 无问题", not pv2["issues"], str(pv2["issues"]))
    check("  · 2 个角色", pv2["roles"] == ["vivian", "uncle_fu"], str(pv2["roles"]))

    # ---- 5. 错误分支 ----
    print("\n[5] 错误分支")
    st, _, e1 = call("POST", "/api/jobs", {"mode": "clone", "text": "   "})
    check("空文本被拒", st == 400 and not e1.get("ok"), e1.get("error", ""))
    st, _, e2 = call("POST", "/api/jobs", {"mode": "nope", "text": "x"})
    check("未知模式被拒", st == 400, e2.get("error", ""))
    st, _, e3 = call("POST", "/api/voices", {"name": "", "voice": "御姐"})
    check("空音色名被拒", st == 400, e3.get("error", ""))
    st, _, e4 = call("GET", "/api/jobs/deadbeef")
    check("不存在的 job → 404", st == 404, e4.get("error", ""))
    # 从链接拉参考音频。源就用**本服务自己**的 /media/voice —— 不依赖外网，
    # 却真会走完「HTTP 下载 → 落盘 → 解码 → 回报时长」，顺带钉住几条错误话术。
    src_url = f"{BASE}/media/voice/{urllib.parse.quote('御姐')}"
    st, _, u1 = call("POST", "/api/upload/url", {"url": src_url})
    check("从链接拉参考音频", st == 200 and u1.get("ok") and (u1.get("duration") or 0) > 1,
          f"{u1.get('duration')}s · {u1.get('name', '')}")
    st, _, u2 = call("POST", "/api/upload/url", {"url": "file:///C:/Windows/win.ini"})
    check("非 http(s) 链接被拒", st == 400 and "http" in (u2.get("error") or ""), u2.get("error", ""))
    st, _, u3 = call("POST", "/api/upload/url", {"url": BASE + "/"})
    check("指向网页的链接被拒", st == 400 and "网页" in (u3.get("error") or ""), u3.get("error", ""))
    st, _, u4 = call("POST", "/api/upload/url", {"url": BASE + "/media/voice/nope"})
    check("链接 404 有明确说明", st == 400 and "404" in (u4.get("error") or ""), u4.get("error", ""))

    # ---- 6. 音色库读路径 ----
    print("\n[6] 音色库")
    st, _, vs = call("GET", "/api/voices")
    names = [v["name"] for v in vs["voices"]]
    check("GET /api/voices", st == 200 and len(names) >= 6, "、".join(names))
    # 从原项目迁入的 6 个：断言「都在」，而不是「只有这 6 个」。
    # 用户新增的音色不该让测试失败；`李老师` 原本也是迁入的，用户已删，不再断言
    # （要恢复的话素材在 Qwen3TTS_Pro/personas/ 下）。
    need = {"御姐", "韩立", "南宫婉", "旁白", "小林", "渊博小叔"}
    check("  · 迁移的 6 个音色都在", need <= set(names),
          f"缺 {sorted(need - set(names))}" if need - set(names) else "")
    # 基因状态同理：只查这 6 个。新建的音色可能正处在「后台任务还没跑完」
    # 的状态，或者用户主动用了 prompt=false，都不该算失败。
    missing = [v["name"] for v in vs["voices"] if v["name"] in need and not v["has_prompt"]]
    check("  · 迁移音色的基因都在", not missing, "、".join(missing))
    # 写回**真值**而不是空串：既验证了接口，又不会短暂清空这条音色的参考文本
    #（服务与用户自己的实例共用同一份 voices 数据文件，中途被打断就真丢了）。
    st, _, tx = call("POST", "/api/voices/%s/text" % urllib.parse.quote("御姐"),
                     {"ref_text": VOICE_TRUTH["御姐"]})
    check("POST /api/voices/{name}/text", st == 200 and tx.get("ok"))

    # ---- 7. 自动转写（可选能力）----
    print("\n[7] 参考音频自动转写")
    asr_info = bo.get("asr", {})
    if not asr_info.get("available"):
        print(f"       跳过 —— {asr_info.get('reason')}")
    else:
        for who in ("御姐", "韩立"):
            truth = VOICE_TRUTH[who]
            st, _, tr = call("POST", "/api/transcribe",
                             {"voice": who})
            got = (tr.get("text") or "").strip()
            # 识别不要求逐字全对（同音字是这类模型的常态），
            # 要求的是「大体正确、能当打草稿的起点」：字符重合度 ≥ 70%
            hit = sum(1 for c in set(got) if c in truth) / max(1, len(set(got)))
            check(f"  转写 {who}", st == 200 and bool(got), got[:40])
            check(f"    与真值重合度 ≥ 70%", hit >= 0.70, f"{hit:.0%}")
        st, _, tr = call("POST", "/api/transcribe", {"voice": "不存在的音色"})
        check("  不存在的音色 → 400", st == 400, tr.get("error", ""))

    # ---- 收尾：恢复被改动的数据 ----
    # 这一段的测试会写音色库。**收尾必须还原** ——
    # 早先这里直接把 ref_text 写成空字符串「验证幂等」，
    # 结果把御姐的参考文本真的清掉了。测完要还原成原值。
    st, _, _ = call("POST", "/api/voices/%s/text" % urllib.parse.quote("御姐"),
                    {"ref_text": VOICE_TRUTH["御姐"]})
    check("已还原测试中改动的音色文本", st == 200)

    # ---- 8. 音色基因 ----
    # 这一节盯一个真实踩过的坑：界面规格选择器的值是 "auto"，
    # 而 MODEL_DIRS 只有 ("clone","1.7B") 这样的实键。早先 build_prompt 用
    # `size = size or default` 判断 —— "auto" 是真值，等于没判断，
    # 于是直接抛「未知的功能/规格组合：clone / auto」，点「生成音色基因」必失败。
    print("\n[8] 音色基因")
    st, _, r = call("POST", "/api/voices/%s/prompt" % urllib.parse.quote("不存在的音色"),
                    {"size": "auto"})
    check("  不存在的音色 → 400", st == 400, r.get("error", ""))

    st, _, r = call("POST", "/api/voices/%s/prompt" % urllib.parse.quote("御姐"),
                    {"size": "auto"})
    check("  size=auto 不再抛「未知的功能/规格组合」",
          st == 200 and bool(r.get("job")), r.get("error") or str(r.get("job")))
    if r.get("job"):
        t0 = time.time()
        stt = {}
        while time.time() - t0 < 300:
            _, _, stt = call("GET", "/api/jobs/" + r["job"])
            if stt.get("state") in ("done", "error"):
                break
            time.sleep(1)
        check("  基因任务走到 done", stt.get("state") == "done",
              f"{time.time() - t0:.1f}s · {stt.get('message', '')[:40]}")

    # 保存音色时应当**自动**排一个基因任务，且 prompt=false 能关掉。
    # 这里故意**不传 ref_text** —— 「只上传音频、没填参考文本」是完全合法的
    # 用法（默认的仅向量模式不需要它），但内核在 x_vector_only_mode=False
    # 且 ref_text 为空时会抛 ValueError。这一段就是钉住这个坑。
    # 临时音色用完即删，别留在用户库里。
    tmp_a, tmp_b = "__冒烟_自动基因", "__冒烟_不排基因"
    call("POST", "/api/voices/delete", {"names": [tmp_a, tmp_b]})
    try:
        st, _, r = call("POST", "/api/voices", {"name": tmp_a, "voice": "御姐", "prompt": True})
        check("  保存音色时自动排基因任务", st == 200 and bool(r.get("job")),
              r.get("error") or str(r.get("job")))
        _, _, vs_a = call("GET", "/api/voices")
        rt_a = next((v.get("ref_text") for v in vs_a["voices"] if v["name"] == tmp_a), "")
        check("  该音色没有参考文本（确实走到了「空 ref_text」路径）", not rt_a, repr(rt_a))
        if r.get("job"):
            t0 = time.time()
            while time.time() - t0 < 300:
                _, _, stt = call("GET", "/api/jobs/" + r["job"])
                if stt.get("state") in ("done", "error"):
                    break
                time.sleep(1)
            check("    自动任务走到 done", stt.get("state") == "done", stt.get("message", "")[:40])
            _, _, vs2 = call("GET", "/api/voices")
            has = next((v["has_prompt"] for v in vs2["voices"] if v["name"] == tmp_a), False)
            check("    自动任务确实产出了 .pt", bool(has))

        # 同名音色只能有一个。以前这里是「悄悄改名成 xxx_2」，用户会以为
        # 存了两个不同的音色，其实是同一个名字的两份拷贝，用错一份很难看出来。
        # （放在基因断言之后：覆盖会把 tmp_a 的 .pt 删掉，先放会打乱上面那条断言。）
        st, _, dup = call("POST", "/api/voices", {"name": tmp_a, "voice": "御姐", "prompt": False})
        check("  同名被拒（409 · code=name_exists）",
              st == 409 and dup.get("code") == "name_exists",
              f"{st} · {dup.get('error', '')}")
        _, _, vs_d = call("GET", "/api/voices")
        ghost = [v["name"] for v in vs_d["voices"] if v["name"].startswith(tmp_a + "_")]
        check("  没有偷偷生成「…_2」", not ghost, "、".join(ghost))
        st, _, rep = call("POST", "/api/voices",
                          {"name": tmp_a, "voice": "御姐", "prompt": False, "overwrite": True})
        check("  overwrite=true 能覆盖同名", st == 200 and rep.get("replaced") is True,
              f"{st} · replaced={rep.get('replaced')}")

        st, _, r = call("POST", "/api/voices", {"name": tmp_b, "voice": "御姐", "prompt": False})
        check("  prompt=false 不排任务", st == 200 and r.get("job") is None, str(r.get("job")))
    finally:
        call("POST", "/api/voices/delete", {"names": [tmp_a, tmp_b]})

    # ---- 9. 真实生成（会加载模型）----
    print("\n[9] 真实生成 · 语音克隆（会加载 1.7B 权重，耐心等）")
    st, _, job = call("POST", "/api/jobs", {
        "mode": "clone",
        "text": "这句话我想说得压着点，别太用力。",
        "language": "Chinese",
        "size": "1.7B",
        "voice": "御姐",
        "instruct": "压低声音，语速偏慢，带一点克制",
        "xvec_only": True,
    })
    check("POST /api/jobs 已受理", st == 200 and job.get("job"), str(job.get("job")))
    jid = job.get("job")
    t0 = time.time()
    last = {}
    while True:
        _, _, last = call("GET", f"/api/jobs/{jid}")
        el = time.time() - t0
        if last.get("state") in ("done", "error") or el > 600:
            break
        print(f"        … {last.get('state')} {last.get('progress', 0):.0%}  {el:5.1f}s", flush=True)
        time.sleep(2)
    check("任务走到 done", last.get("state") == "done",
          f"{time.time() - t0:.1f}s · {last.get('message', '')[:60]}")
    res = last.get("result") or {}
    if last.get("state") == "done":
        check("  · 产出音频文件", bool(res.get("file")), str(res.get("file")))
        check("  · 时长合理", (res.get("duration") or 0) > 1.0, f"{res.get('duration')}s")
        check("  · 状态行含 RTF", "RTF" in (last.get("message") or ""), last.get("message", "").split("\n")[0])
        st, ct, wav = call("GET", res["url"], raw=True)
        check("  · 音频可回放", st == 200 and wav[:4] == b"RIFF", f"{len(wav)} bytes")

    # ---- 8. 模型管理 ----
    print("\n[10] 模型管理")
    st, _, ms = call("GET", "/api/bootstrap")
    check("已加载一套权重", bool(ms["model_status"]["loaded"]), str(ms["model_status"]["loaded"]))
    st, _, mu = call("POST", "/api/model/unload")
    check("POST /api/model/unload", st == 200 and mu["status"]["loaded"] is None)
    st, _, ml = call("POST", "/api/model/load", {"kind": "official", "size": "1.7B"})
    check("POST /api/model/load (official/1.7B)",
          st == 200 and (ml.get("status") or {}).get("loaded", {}).get("kind") == "official",
          str(ml.get("status", {}).get("loaded")))
    call("POST", "/api/model/unload")

    # ---- 9. 合成记录 ----
    print("\n[11] 合成记录")
    st, _, hs = call("GET", "/api/history")
    check("GET /api/history", st == 200 and hs.get("history"), f"{len(hs.get('history', []))} 条")
    if hs.get("history"):
        it = hs["history"][0]
        check("  · 记录了模式与参数", it.get("mode") == "clone" and it.get("voice") == "御姐",
              f"{it.get('mode')} / {it.get('voice')} / {it.get('size')}")

    # ---- 12. 合成记录存的是正文全文，不是预览 ----
    # 这条破例 import 了 app 里的模块：正文长度只在**写**的时候才看得出来，
    # 而写记录是生成流程内部触发的、没有对应接口；为了测一条长度去真跑一次
    # 长文生成要占 GPU 几十秒，不值得。
    # 把 `_PATH` 顶到一个临时文件上，**绝不碰用户真实的 outputs/history.json**。
    print("\n[12] 合成记录的正文上限")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
    import history as history_mod  # noqa: E402

    real_path = history_mod._PATH
    tmp_path = real_path.parent / "_smoke_history_tmp.json"
    try:
        history_mod._PATH = tmp_path
        short_text = "短" * 300
        history_mod.add({"text": short_text, "mode": "clone"})
        long_text = "长" * 5000
        rec = history_mod.add({"text": long_text, "mode": "clone"})
        got_short = history_mod.list_all()[1]["text"]
        got_long = history_mod.list_all()[0]["text"]
        # 以前这里截到 200 字：列表里两行看不太出来，但「悬停看原文」弹出来的
        # 就是那 200 字 —— 用户会以为功能没做完。
        check("  300 字原样存下（没被截成预览）", got_short == short_text, f"{len(got_short)} 字")
        check("  5000 字截到防爆上限 2000", got_long == long_text[:2000], f"{len(got_long)} 字")
        check("  chars 记的是真实字数，与截断无关", rec.get("chars") == 5000, str(rec.get("chars")))
    finally:
        history_mod._PATH = real_path
        try:
            tmp_path.unlink()
        except OSError:
            pass

    print("\n" + "=" * 70)
    if FAILS:
        print(f"结果：{len(FAILS)} 项未通过")
        for f in FAILS:
            print("   - " + f)
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
