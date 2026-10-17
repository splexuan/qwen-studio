"""Qwen3-TTS Studio — 本地语音合成工作台：全局配置。

路径全部**相对本工具根目录**解析，因此整个目录可以随意搬运 / 压缩分发，
解压到任意路径都能直接运行（无需改任何配置）。

与 OmniVoice Studio 的差异（本项目是「多模型」而非「单模型」）：
    Qwen3-TTS 有 5 套权重，按功能 + 规格两个维度组合。本文件把
    「功能 → 规格 → 目录名」的映射收敛成 :data:`MODEL_DIRS`，
    app/core.py 只负责按这份映射加载。

    design（声音设计） 只有 1.7B；
    clone / official  各有 1.7B 与 0.6B，可自由切换。
"""

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 目录（相对本文件 → 随包移动不受影响）
# ---------------------------------------------------------------------------
STUDIO_DIR = Path(__file__).resolve().parent.parent   # 工具根目录
APP_DIR = STUDIO_DIR / "app"
WEB_DIR = STUDIO_DIR / "web"
# 让 `import hardware` 在「python app/core.py」和「从仓库根导入」两种方式下都成立
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# vendor/ 放的是加速推理内核源码（faster-qwen3-tts）与外部工具（ffmpeg/sox）。
# 必须在 import faster_qwen3_tts 之前注入 sys.path —— 这是本工具唯一的
# 「非 pip 安装」依赖来源，早期那套 editable 安装（.pth 指向开发机路径）
# 一旦整个目录被搬走就会失效，故改为显式路径注入。
VENDOR_DIR = STUDIO_DIR / "vendor"
VENDOR_SRC = VENDOR_DIR / "faster-qwen3-tts-main"
VENDOR_BIN = VENDOR_DIR / "bin"
if VENDOR_SRC.exists() and str(VENDOR_SRC) not in sys.path:
    sys.path.insert(0, str(VENDOR_SRC))

# 外部工具（sox / ffmpeg）加进 PATH：pydub 转码与部分音频后处理会用到
if VENDOR_BIN.exists():
    os.environ["PATH"] = f"{VENDOR_BIN}{os.pathsep}{os.environ.get('PATH', '')}"

# 缓解显存碎片、降低 OOM 风险（Linux 有效；Windows 不支持该项，设了只会告警）
if sys.platform != "win32":
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

# 全离线：本工具不联网取权重，权重与依赖都随包分发。
# 这三个开关必须在 import transformers 之前设置，故放在 config 顶层。
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("MODELSCOPE_OFFLINE", "1")

MODELS_DIR = STUDIO_DIR / "models"        # 模型权重（5 套）
VOICES_DIR = STUDIO_DIR / "voices"        # 音色库：参考音频 / 参考文本 / prompt 缓存
OUTPUT_DIR = STUDIO_DIR / "outputs"       # 合成结果
UPLOAD_DIR = WEB_DIR / "_uploads"         # 界面上传的临时音频
PREVIEW_DIR = WEB_DIR / "_preview"        # 官方说话人的试听样例（生成一次后长期复用）
for _d in (VOICES_DIR, OUTPUT_DIR, UPLOAD_DIR, PREVIEW_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 模型权重：功能 + 规格 → 目录名
# ---------------------------------------------------------------------------
# 功能代号统一用英文短名，界面文案与后端参数都走这几个 key，避免中文当键。
#   design   声音设计 —— 用文字描述捏一个不存在的音色（仅 1.7B）
#   clone    语音克隆 —— 参考音频复刻音色（1.7B / 0.6B）
#   official 官方精品 —— 9 个官方说话人，支持情感指令与多人对话（1.7B / 0.6B）
#   script   剧本工坊 —— 复用 voices/ 里的固化音色做多角色长剧本（走 clone 权重）
MODEL_DIRS = {
    ("design", "1.7B"): "Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    ("clone", "1.7B"): "Qwen3-TTS-12Hz-1.7B-Base",
    ("clone", "0.6B"): "Qwen3-TTS-12Hz-0.6B-Base",
    ("official", "1.7B"): "Qwen3-TTS-12Hz-1.7B-CustomVoice",
    ("official", "0.6B"): "Qwen3-TTS-12Hz-0.6B-CustomVoice",
}
# 剧本工坊与语音克隆共用同一套 Base 权重（前者只是多了剧本解析与拼接）
MODEL_DIRS[("script", "1.7B")] = MODEL_DIRS[("clone", "1.7B")]
MODEL_DIRS[("script", "0.6B")] = MODEL_DIRS[("clone", "0.6B")]

KINDS = ("clone", "design", "official", "script")
SIZES = ("1.7B", "0.6B")

# 官方精品（CustomVoice）模型内置的 9 个说话人。
# 名称必须与权重 tokenizer 里的 speaker 表一致，小写传入。
OFFICIAL_SPEAKERS = [
    ("vivian", "Vivian · 明亮微锐的年轻女声（中）"),
    ("serena", "Serena · 温和柔韧的年轻女声（中）"),
    ("uncle_fu", "Uncle_Fu · 低沉醇厚的成熟男声（中）"),
    ("dylan", "Dylan · 京腔少年男声（中·北京）"),
    ("eric", "Eric · 明亮沙哑的成都男声（中·四川）"),
    ("ryan", "Ryan · 节奏感强的男声（英）"),
    ("aiden", "Aiden · 阳光美式男声（英）"),
    ("ono_anna", "Ono_Anna · 俏皮日语女声（日）"),
    ("sohee", "Sohee · 温暖韩语女声（韩）"),
]
SPEAKER_NAMES = [k for k, _ in OFFICIAL_SPEAKERS]

# 试听用的样例文本，按说话人的母语给 —— 让日语说话人念中文，听不出它本来的样子。
SPEAKER_LANG = {
    "vivian": "Chinese", "serena": "Chinese", "uncle_fu": "Chinese",
    "dylan": "Chinese", "eric": "Chinese",
    "ryan": "English", "aiden": "English",
    "ono_anna": "Japanese", "sohee": "Korean",
}
SPEAKER_PREVIEW_TEXT = {
    "Chinese": "你好，很高兴认识你。今天天气不错，要不要一起出去走走？",
    "English": "Hello, nice to meet you. The weather is lovely today, shall we take a walk?",
    "Japanese": "こんにちは、はじめまして。今日はいい天気ですね。",
    "Korean": "안녕하세요, 반갑습니다. 오늘 날씨가 정말 좋네요.",
}

# 支持的语言（与官方一致）。Auto = 交给模型按文本判断。
LANGUAGES = [
    "Auto", "Chinese", "English", "Japanese", "Korean", "German",
    "French", "Russian", "Portuguese", "Spanish", "Italian",
]


def model_path(kind: str, size: str) -> Path | None:
    """返回某功能 + 规格对应的权重目录；不存在返回 None。"""
    sub = MODEL_DIRS.get((kind, size))
    if not sub:
        return None
    p = MODELS_DIR / sub
    return p if p.exists() else None


def missing_models() -> list[str]:
    """列出缺失的权重目录名，供自检与界面提示。"""
    want = {MODEL_DIRS[(k, s)] for k in KINDS for s in SIZES if (k, s) in MODEL_DIRS}
    return sorted(m for m in want if not (MODELS_DIR / m).exists())


# ---------------------------------------------------------------------------
# 运行参数 —— 默认全部 auto：按本机显卡 / 显存 / 平台自动适配
# 决策规则集中在 app/hardware.py，这里只写「你想强制什么」。
# ---------------------------------------------------------------------------
# 设备："auto"（cuda > xpu > mps > cpu）；也可强制 "cuda" / "cuda:1" / "cpu"
DEVICE = "auto"

# 精度："auto" = Ampere 及更新用 bfloat16、老 N 卡用 float16、CPU/MPS 用 float32
DTYPE = "auto"

# 默认模型规格："auto" = 按显存自动（≥6 GiB → 1.7B，更小 → 0.6B）
# 1.7B 音质与控制力更好；0.6B 省显存、吞吐高，但官方精品不支持情感指令
# （内核里 0.6B 的 CustomVoice 会强制把 instruct 置空，非本工具限制）
DEFAULT_SIZE = "auto"

# 克隆 / 剧本模式是否只用说话人向量（xvec_only）。
#   True  = 只用 speaker embedding：不会被参考音频的末字串音污染，
#           切换语种更干净，且不需要填写参考文本；
#   False = 完整 ICL：把参考音频的声学 token 一并放进上下文，
#           通常更能还原参考的韵律，但**必须**提供准确的参考文本。
# 内核默认 True，这里保持一致；界面可逐次覆盖。
CLONE_XVEC_ONLY = True

# 长剧本分段合成时，段与段之间插入的静音（秒）。原项目用 0.4s。
SCRIPT_GAP_SECS = 0.4

# 单次生成的最长文本字符数（超出会被拒绝，提示分段）。防止误贴长文直接 OOM。
MAX_TEXT_CHARS = 2000

# 合成记录最多保留条数（只存参数，不存音频副本）
HISTORY_MAX = 300

# WebUI（web/server.py 读取；改这里即可换监听地址与端口）
# 端口选 7867：与 omnivoice-studio 的 7866、原整合包的 7869 都错开。
# 也支持环境变量覆盖 —— 「7867 被占了想临时换个端口」不必改源码：
#     set QWENSTUDIO_PORT=7877 && 启动程序.bat
#
# 监听地址默认 **127.0.0.1**：只有这台机器能打开。
# 想让同一 Wi-Fi 下的朋友/手机访问，双击「启动程序-局域网.bat」（它会设成 0.0.0.0）；
# 也可以手动：set QWENSTUDIO_HOST=0.0.0.0 && 启动程序.bat
# 注意开放后**服务没有密码** —— 同网络的人能生成语音、读音色库、删合成记录。
HOST = os.environ.get("QWENSTUDIO_HOST", "127.0.0.1").strip() or "127.0.0.1"
try:
    PORT = int(os.environ.get("QWENSTUDIO_PORT", "7867"))
except ValueError:
    PORT = 7867

# 参考音频自动转写（app/asr.py）。
# 默认 **auto**：依赖与权重都在就直接可用，缺哪一样就由 asr.available() 给出
# 具体原因，界面据此把按钮禁用并说明 —— 而不是做一个「点了才报错」的按钮。
# 手动覆盖：QWENSTUDIO_ASR=1 强制开，QWENSTUDIO_ASR=0 强制关。
_asr_env = os.environ.get("QWENSTUDIO_ASR", "auto").strip().lower()
ASR_ENABLED: bool | None = (
    None if _asr_env in ("auto", "") else (_asr_env in ("1", "true", "yes"))
)

# 从链接拉取参考音频（界面「新增音色 → 粘贴音频直链」，app/fetch.py 读取）。
# 只接受 http / https。体积与超时**必须**有上限：贴错一个几百 MB 的地址
# 不该把磁盘和等待时间一起拖垮，而下载是占着一个线程同步跑的。
URL_IMPORT_MAX_MB = 64
URL_IMPORT_TIMEOUT = 60.0        # 连接 / 读取各自的上限（秒）

# ---------------------------------------------------------------------------
# 适配档位（缓存）
# ---------------------------------------------------------------------------
_PROFILE = None


def profile():
    """本机适配档位（首次调用时探测硬件，之后缓存）。"""
    global _PROFILE
    if _PROFILE is None:
        import hardware  # 同目录，APP_DIR 已加入 sys.path

        _PROFILE = hardware.detect(
            device=DEVICE,
            dtype=DTYPE,
            default_size=DEFAULT_SIZE,
        )
    return _PROFILE


def resolve_size(size: str | None) -> str:
    """把请求里的规格收敛成 :data:`MODEL_DIRS` 里真实存在的键。

    ``"auto"`` / 空 / 任何未知值 → 本机档位的默认规格。

    这一步是**必须**的，不能省：前端的规格选择器默认就是 ``"auto"``，
    而 ``MODEL_DIRS`` 只有 ``("clone", "1.7B")`` 这样的实键 ——
    把 ``"auto"`` 原样传下去，``MODEL_DIRS.get`` 拿到 None，
    下游会抛「未知的功能/规格组合：clone / auto」。

    注意不能写成 ``size or default``：``"auto"`` 是非空字符串，是真值，
    那样写等于没判断。
    """
    s = (size or "").strip()
    return s if s in SIZES else profile().default_size
