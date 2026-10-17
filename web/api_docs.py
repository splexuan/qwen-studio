"""Qwen3-TTS Studio — 接口文档的**唯一来源**。

为什么单独一个文件：接口说明如果写在别处（README、前端注释、docstring），
改了路由忘了改文案是迟早的事。这里把「声明」集中成纯数据，
:func:`render` 再与 FastAPI 上**真实注册的路由**对账，
把「文档里写了但实际没有」和「实际有但文档没写」都暴露出来。

它是纯数据 + 纯函数，import 它不会拉起模型或 FastAPI，所以 server.py
和离线工具都能安全引用。
"""

from __future__ import annotations

# 每个接口：方法 / 路径 / 说明 / 参数说明
ENDPOINTS: list[dict] = [
    {
        "method": "GET", "path": "/api/bootstrap", "group": "首屏",
        "desc": "一次拿全首屏所需：硬件档位、语种、规格、官方说话人、音色列表、模型就位情况、ASR 可用性、各项上限与默认值。",
        "params": [],
    },
    {
        "method": "GET", "path": "/api/hardware", "group": "首屏",
        "desc": "硬件探测详情（设备/显存/精度/驱动/torch 版本）与自动决策出的档位、提示。排障时先看这个。",
        "params": [],
    },
    {
        "method": "POST", "path": "/api/upload", "group": "音频",
        "desc": "上传参考音频到 web/_uploads/，返回临时文件名与时长。支持 wav/mp3/m4a/flac/ogg/aac/wma。"
                "其中 m4a / aac / wma 是 soundfile 读不了的容器，会自动用随包 ffmpeg 转成 wav 再入库。",
        "params": [("file", "form-data", "音频文件本体")],
    },
    {
        "method": "POST", "path": "/api/upload/url", "group": "音频",
        "desc": "从链接拉取参考音频，落盘位置与返回字段**与 /api/upload 完全一致**，"
                "所以下游（裁剪 / 转写 / 保存音色）不必为「链接来的音频」另走一条路。"
                "只接受 http/https；重定向最多 5 跳，体积与超时上限见 config.URL_IMPORT_MAX_MB / "
                "URL_IMPORT_TIMEOUT。指向网页的链接会直接拒绝，并提示改贴音频直链。",
        "params": [("url", "str", "音频直链（http/https）")],
    },
    {
        "method": "POST", "path": "/api/transcribe", "group": "音频",
        "desc": "参考音频转写（SenseVoice Small，CPU）。用于少打字，**不是**给你保证正确的："
                "同音字和标点会有出入，而完整 ICL 模式下参考文本写错会直接污染输出。"
                "是否可用由 /api/bootstrap 的 asr 字段报告。",
        "params": [
            ("upload", "str", "刚上传的临时文件名（与 voice 二选一）"),
            ("voice", "str", "已有音色名（与 upload 二选一）"),
            ("start/end", "float?", "可选，先裁剪再转写（秒）。建议与保存音色时用同一段"),
        ],
    },
    {
        "method": "GET", "path": "/api/voices", "group": "音色库",
        "desc": "音色列表，含时长、参考文本字数、是否已生成基因、长度是否合适（偏短/合适/偏长）。",
        "params": [],
    },
    {
        "method": "POST", "path": "/api/voices", "group": "音色库",
        "desc": "保存音色。音频来源三选一：upload（刚上传的文件）、voice（已有音色，用于「另存为」）、"
                "out（outputs/ 里的生成结果文件名 —— 声音设计页的「存进音色库」走这条："
                "设计出来的嗓子复现不了，只能直接拿产物当参考音频）。"
                "**音色名唯一**：不传 overwrite 时同名直接失败（409，响应里 code=name_exists），"
                "由界面问一句是否覆盖；overwrite=true 才覆盖"
                "（覆盖会删掉旧基因，因为它对应的是旧音频）。"
                "保存成功后默认**顺手排一个后台任务**抽取音色基因，响应里的 job 即任务号，"
                "用 /api/jobs/{id} 轮询即可 —— 抽基因要加载模型，所以不阻塞保存本身。",
        "params": [
            ("name", "str", "音色名（会做非法字符清洗）"),
            ("upload", "str?", "临时文件名（web/_uploads/ 下，三选一）"),
            ("voice", "str?", "已有音色名（三选一）"),
            ("out", "str?", "生成结果文件名（outputs/ 下，三选一）"),
            ("ref_text", "str?", "参考音频实际说的字（完整 ICL 模式必需）"),
            ("xvec_only", "bool?", "是否只用说话人向量；不传则沿用全局默认"),
            ("start/end", "float?", "可选裁剪区间（秒）"),
            ("overwrite", "bool?", "同名时是否覆盖"),
            ("prompt", "bool?", "是否顺手生成音色基因，默认 true；批量导入只想要音频时传 false"),
        ],
    },
    {
        "method": "POST", "path": "/api/voices/{name}/trim", "group": "音色库",
        "desc": "就地裁剪参考音频（界面上拖选后保存）。裁剪会让已生成的基因失效并自动删除。",
        "params": [("start", "float", "起点（秒）"), ("end", "float", "终点（秒）")],
    },
    {
        "method": "POST", "path": "/api/voices/{name}/rename", "group": "音色库",
        "desc": "重命名音色，同步迁移 .wav/.txt/.json/.pt 四件套。",
        "params": [("new_name", "str", "新名字")],
    },
    {
        "method": "POST", "path": "/api/voices/{name}/text", "group": "音色库",
        "desc": "更新参考文本。",
        "params": [("ref_text", "str", "参考音频实际说的字")],
    },
    {
        "method": "POST", "path": "/api/voices/{name}/prompt", "group": "音色库",
        "desc": "生成音色基因（.pt）。需加载模型，故返回 job id，用 /api/jobs/{id} 轮询。"
                "保存音色时也会自动排一个同样的任务。",
        "params": [("size", "str?", "用哪套权重抽；传 auto / 不传都按本机档位取，不是必须给具体规格")],
    },
    {
        "method": "POST", "path": "/api/voices/delete", "group": "音色库",
        "desc": "批量删除音色（含其参考文本、元数据、基因文件）。",
        "params": [("names", "str[]", "要删除的音色名列表")],
    },
    {
        "method": "POST", "path": "/api/script/preview", "group": "剧本",
        "desc": "剧本实时预览：解析 + 校验 + 统计。**不加载模型**，所以界面能做到边打字边提示。",
        "params": [
            ("text", "str", "剧本全文"),
            ("mode", "str", "script=按音色库校验；official=按官方说话人校验"),
        ],
    },
    {
        "method": "POST", "path": "/api/jobs", "group": "合成",
        "desc": "提交合成任务，立即返回 job id。生成是重活，一律走后台线程，前端轮询进度。",
        "params": [
            ("mode", "str", "clone | design | official | script"),
            ("text", "str", "要合成的文本"),
            ("language", "str", "Auto / Chinese / English / …"),
            ("size", "str?", "1.7B | 0.6B，缺省用档位默认"),
            ("instruct", "str?", "情感或风格指令（自由文本，不查词表）"),
            ("voice", "str?", "mode=clone 时的音色库名字"),
            ("ref_upload", "str?", "mode=clone 时用刚上传的临时音频（与 voice 二选一）"),
            ("ref_audio", "str?", "mode=clone 时的参考音频绝对路径（脚本调用用这个）"),
            ("ref_text", "str?", "参考音频文本；xvec_only=false 时必填"),
            ("xvec_only", "bool?", "仅用说话人向量；false 走完整 ICL，更贴参考韵律"),
            ("speaker", "str?", "mode=official 时的官方说话人 key"),
        ],
    },
    {
        "method": "GET", "path": "/api/model", "group": "合成",
        "desc": "当前驻留的权重、显存占用、缺失的权重目录。界面「系统设置」每次进入都读它。",
        "params": [],
    },
    {
        "method": "POST", "path": "/api/model/load", "group": "合成",
        "desc": "主动加载一套权重（把首次生成的加载耗时提前到可等待的时机）。同一时刻只驻留一套。",
        "params": [
            ("kind", "str", "clone | design | official"),
            ("size", "str", "1.7B | 0.6B；auto 用档位默认"),
        ],
    },
    {
        "method": "POST", "path": "/api/model/unload", "group": "合成",
        "desc": "卸载当前权重并释放显存。下次生成会重新加载。",
        "params": [],
    },
    {
        "method": "GET", "path": "/api/jobs/{jid}", "group": "合成",
        "desc": "轮询任务：state ∈ queued/running/done/error；done 时 result 带音频 URL 与时长。",
        "params": [("jid", "str", "任务 id")],
    },
    {
        "method": "POST", "path": "/api/speakers/{name}/preview", "group": "合成",
        "desc": "官方说话人的试听样例。样例得真跑一次模型才有，所以**缓存命中就直接给 url**；"
                "没命中才排后台任务并返回 job，用 /api/jobs/{id} 轮询 —— 响应里 url 与 job 必有其一。"
                "规格进缓存文件名，1.7B 与 0.6B 各存一份。",
        "params": [
            ("name", "str", "官方说话人 key，如 vivian"),
            ("size", "str?", "1.7B | 0.6B；auto 用档位默认"),
        ],
    },
    {
        "method": "GET", "path": "/api/history", "group": "记录",
        "desc": "合成记录（倒序），标注音频文件是否还在。只存参数，不存音频副本。",
        "params": [("limit", "int?", "最多返回条数，0=全部")],
    },
    {
        "method": "POST", "path": "/api/history/delete", "group": "记录",
        "desc": "删除指定记录（不动 outputs/ 里的 wav）。",
        "params": [("ids", "str[]", "记录 id 列表")],
    },
    {
        "method": "POST", "path": "/api/history/clear", "group": "记录",
        "desc": "清空全部记录（同样不动 wav）。",
        "params": [],
    },
    {
        "method": "GET", "path": "/media/out/{name}", "group": "媒体",
        "desc": "回放合成结果。", "params": [("name", "str", "outputs/ 下的文件名")],
    },
    {
        "method": "GET", "path": "/media/upload/{name}", "group": "媒体",
        "desc": "回放临时上传的音频。", "params": [("name", "str", "临时文件名")],
    },
    {
        "method": "GET", "path": "/media/voice/{name}", "group": "媒体",
        "desc": "回放某音色的参考音频。", "params": [("name", "str", "音色名")],
    },
    {
        "method": "GET", "path": "/media/preview/{name}", "group": "媒体",
        "desc": "回放官方说话人的试听样例（缓存在 web/_preview/）。",
        "params": [("name", "str", "试听文件名")],
    },
    {
        "method": "GET", "path": "/api/docs", "group": "文档",
        "desc": "本接口清单，并与真实注册路由对账。", "params": [],
    },
    {
        "method": "GET", "path": "/openapi.json", "group": "文档",
        "desc": "FastAPI 自动生成的机器可读规格（给脚本 / Agent 用）。", "params": [],
    },
]

# 给「API 对接」页用的示例
EXAMPLES: list[dict] = [
    {
        "title": "克隆一段语音（含情绪指令）",
        "lang": "bash",
        "code": """curl -X POST {base}/api/jobs \\
  -H "Content-Type: application/json" \\
  -d '{
    "mode": "clone",
    "text": "这句话我想说得压着点，别太用力。",
    "language": "Chinese",
    "size": "auto",
    "voice": "你的音色名",
    "instruct": "压低声音，语速偏慢，带一点克制"
  }'
# voice 必须是音色库里已有的名字（见界面「音色库」页），否则会返回
#   {"ok": false, "error": "音色库里没有 [xxx]。"}
# size 用 auto 就按本机档位挑；显式写 1.7B / 0.6B 也行
# → {"ok": true, "job": "a1b2c3d4e5f6"}
# 再轮询：curl {base}/api/jobs/a1b2c3d4e5f6""",
    },
    {
        "title": "用文字描述捏一个音色",
        "lang": "bash",
        "code": """curl -X POST {base}/api/jobs \\
  -H "Content-Type: application/json" \\
  -d '{
    "mode": "design",
    "text": "欢迎收听今晚的节目。",
    "language": "Chinese",
    "instruct": "三十五岁上下的男声，嗓音偏低有颗粒感，语速沉稳"
  }'""",
    },
    {
        "title": "官方说话人 + 多人对话",
        "lang": "bash",
        "code": """curl -X POST {base}/api/jobs \\
  -H "Content-Type: application/json" \\
  -d '{
    "mode": "official",
    "language": "Chinese",
    "size": "1.7B",
    "text": "[vivian][轻快地] 你终于来了。\\n[uncle_fu][低沉] 路上耽搁了一会儿。"
  }'
# 注意：0.6B 规格会强制忽略 instruct，多人对话请用 1.7B""",
    },
    {
        "title": "剧本工坊（用音色库里的音色）",
        "lang": "bash",
        "code": """curl -X POST {base}/api/jobs \\
  -H "Content-Type: application/json" \\
  -d '{
    "mode": "script",
    "language": "Chinese",
    "size": "1.7B",
    "text": "[你的音色名][压低了声音] 此丹的药力，恐怕不止于此。\\n[另一个音色名] 你是说……"
  }'""",
    },
    {
        "title": "Python 调用",
        "lang": "python",
        "code": """import time, requests

BASE = "{base}"

jid = requests.post(f"{BASE}/api/jobs", json={
    "mode": "clone",
    "text": "先跑通流程，再谈音质。",
    "language": "Chinese",
    "voice": "你的音色名",      # 换成音色库里已有的名字
    "instruct": "平静但有一点疲惫",
}).json()["job"]

while True:
    r = requests.get(f"{BASE}/api/jobs/{jid}").json()
    if r["state"] in ("done", "error"):
        break
    time.sleep(0.5)

print(r["message"])
if r["state"] == "done":
    audio = requests.get(BASE + r["result"]["url"]).content
    open("out.wav", "wb").write(audio)""",
    },
]


def render(real_routes: set | None = None, base: str = "") -> dict:
    """返回接口清单；给了 ``real_routes`` 就顺带做一次对账。

    ``real_routes`` 形如 ``{("/api/jobs", ["GET", "POST"]), ...}``。

    ``base`` 是调用方当前访问的地址（例如 ``http://127.0.0.1:7877``）。
    示例里的 ``{base}`` 会被替换成它 —— 换端口、或从另一台机器/反代访问时，
    复制出来的命令不用手改。没给就退回默认的 127.0.0.1:7867。
    """
    groups: dict[str, list[dict]] = {}
    for ep in ENDPOINTS:
        groups.setdefault(ep["group"], []).append(ep)

    base = (base or "http://127.0.0.1:7867").rstrip("/")
    examples = [{**ex, "code": ex["code"].replace("{base}", base)} for ex in EXAMPLES]

    payload: dict = {
        "endpoints": ENDPOINTS,
        "groups": [{"name": g, "items": items} for g, items in groups.items()],
        "examples": examples,
        "count": len(ENDPOINTS),
        # 直调时要先知道的三件事，界面「API 对接」页示例上方会原样渲染
        "notes": [
            f"示例里的地址已经是「你现在访问的这个」：{base}。换端口、"
            "从别的机器或反向代理进来，示例都跟着变，不用手改。",
            "浏览器顶栏里的「语种 / 模型规格」只影响界面（存在浏览器本地）。"
            "直连 API 不传这两个参数时，服务端按档位兜底：language 缺省 Auto、"
            "size 缺省按显存自动挑（本机当前档位见「系统设置」）。要固定就显式传。",
            "voice / 剧本里的角色名必须是音色库里已有的名字（大小写、空格都要一致），"
            "否则直接返回「音色库里没有 [xxx]」。",
        ],
    }

    if real_routes is not None:
        declared = {(ep["method"], ep["path"]) for ep in ENDPOINTS}
        real = {(m, p) for p, ms in real_routes for m in ms}

        undeclared = sorted(f"{m} {p}" for m, p in real - declared if m != "HEAD")
        missing = sorted(f"{m} {p}" for m, p in declared - real)
        payload["audit"] = {
            "declared": len(declared),
            "real": len(real),
            # 有路由但文档没写 —— 补文档
            "undeclared": undeclared,
            # 文档写了但路由不存在 —— 删文档或补实现
            "missing": missing,
            "consistent": not undeclared and not missing,
        }
    return payload
