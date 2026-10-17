"""硬件探测 + 自动适配档位（Profile）。

为什么单独一层：不同机器在「有没有独显 / 显存多大 / 什么架构 / 驱动多新」上
差异极大。把 ``cuda + bfloat16`` 写死，会让一部分机器直接跑不起来（精度不支持）、
一部分显存炸掉（OOM）、一部分白白浪费算力（明明能上 1.7B 却默认给 0.6B）。

这里把所有差异收敛成一份 :class:`Profile`：

* ``config.py`` 只负责「用户想强制什么」；
* ``core.py`` 只负责「照 Profile 跑」；
* 决策规则全部集中在本文件 —— 改一处即全局生效。

自动决出的关键项：

==================  ==========================================================
``dtype``           N 卡一律优先 **bfloat16**（数值稳、不易溢出）；只有
                    Pascal 及更老（算力 < 7.0，torch 上 bf16 内核不可用）
                    与 MPS、CPU 才用 **float32**（fp16 在 CPU 上反而更慢且易 NaN）
                    —— **不再给任何卡选 float16**，原因见 :func:`_pick_dtype`
``default_size``    显存 ≥ 6 GiB 给 **1.7B**（音质与控制力上限），
                    更小或纯 CPU 给 **0.6B**（省显存、吞吐高）
``script_chunk_chars`` 长剧本单次送入的字数上限：显存越小给得越少，
                    压低峰值显存。剧本走 Base 权重逐段生成，段长直接决定峰值
==================  ==========================================================

与 OmniVoice 的差异：那边是 diffusion 模型，分档调的是「推理步数」与
「分段时长（秒）」；Qwen3-TTS 是自回归，步数不可调、时长不可直接调，
能调的是「用哪套权重」与「一次喂多少字」——所以 Profile 的字段也不同。

命令行直接查看本机档位（排障用）::

    runtime\\python.exe app\\hardware.py
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field

import torch

# 设备 → torch dtype 名称映射
_DTYPE_MAP = {
    "float16": torch.float16,
    "bfloat16": torch.bfloat16,
    "float32": torch.float32,
}


def _no_window() -> dict:
    """Windows 下调用外部命令时不弹黑框。"""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


# ---------------------------------------------------------------------------
# 底层探测
# ---------------------------------------------------------------------------
def _nvidia_smi() -> tuple[str, str]:
    """返回 (驱动版本, 驱动支持的最高 CUDA 版本)。取不到就给空串。

    必须显式给 ``errors="replace"``：中文 Windows 上 nvidia-smi 会输出本地化字节，
    而本项目的 Python 开了 UTF-8 模式（getpreferredencoding 返回 utf-8），
    subprocess 的子线程解码遇非法字节会抛异常，结果是**整条驱动信息静默丢失**、
    排错表里永远显示「驱动 —」。这个坑在参照项目里踩过一次，这里提前规避。
    """
    try:
        r = subprocess.run(["nvidia-smi"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=10, **_no_window())
        if r.returncode != 0 or not r.stdout:
            return "", ""
        txt = r.stdout
        m_cuda = re.search(r"CUDA Version:\s*([\d.]+)", txt)
        m_drv = re.search(r"Driver Version:\s*([\d.]+)", txt)
        return (m_drv.group(1) if m_drv else ""), (m_cuda.group(1) if m_cuda else "")
    except Exception:  # noqa: BLE001  没有 nvidia-smi / 非 N 卡
        return "", ""


def _total_ram_gib() -> float:
    try:
        import psutil  # transformers/accelerate 会带上，但不强依赖
        return psutil.virtual_memory().total / 1024**3
    except Exception:  # noqa: BLE001
        pass
    if sys.platform == "win32":
        try:
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            st = _MemStatus()
            st.dwLength = ctypes.sizeof(_MemStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.ullTotalPhys / 1024**3
        except Exception:  # noqa: BLE001
            return 0.0
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
    except Exception:  # noqa: BLE001
        return 0.0


def _xpu_available() -> bool:
    xpu = getattr(torch, "xpu", None)
    return bool(xpu is not None and getattr(xpu, "is_available", lambda: False)())


def _mps_available() -> bool:
    mps = getattr(torch.backends, "mps", None)
    return bool(mps is not None and mps.is_available())


def _resolve_backend(requested: str) -> tuple[str, int]:
    """把用户写的 device 字符串解析成 (backend, 设备序号)。"""
    req = (requested or "auto").strip().lower()
    if req == "auto":
        if torch.cuda.is_available():
            return "cuda", 0
        if _xpu_available():
            return "xpu", 0
        if _mps_available():
            return "mps", 0
        return "cpu", 0

    head, _, tail = req.partition(":")
    idx = int(tail) if tail.isdigit() else 0
    if head == "cuda":
        return ("cuda", idx) if torch.cuda.is_available() else ("cpu", 0)
    if head == "xpu":
        return ("xpu", idx) if _xpu_available() else ("cpu", 0)
    if head == "mps":
        return ("mps", 0) if _mps_available() else ("cpu", 0)
    return "cpu", 0


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
@dataclass
class Profile:
    # 最终生效值
    device: str = "cpu"
    dtype: str = "float32"
    default_size: str = "0.6B"
    script_chunk_chars: int = 60
    # 探测到的硬件事实
    backend: str = "cpu"
    device_name: str = "CPU"
    vram_gib: float = 0.0
    compute_cap: str = ""
    torch_cuda: str = ""
    driver_cuda: str = ""
    driver_version: str = ""
    total_ram_gib: float = 0.0
    python: str = ""
    torch_version: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def torch_dtype(self) -> torch.dtype:
        return _DTYPE_MAP[self.dtype]

    @property
    def is_gpu(self) -> bool:
        return self.backend != "cpu"

    def short(self) -> str:
        """一行摘要，给界面标题/日志用。"""
        vram = f"{self.vram_gib:.1f} GiB" if self.vram_gib else "—"
        return f"{self.device_name} · {vram} · {self.dtype} · 默认 {self.default_size}"

    def rows(self) -> list[tuple[str, str]]:
        """键值对形式的详情，供界面/日志渲染。"""
        return [
            ("计算后端", self.backend),
            ("设备", self.device),
            ("设备名称", self.device_name),
            ("显存", f"{self.vram_gib:.1f} GiB" if self.vram_gib else "不适用"),
            ("算力", self.compute_cap or "—"),
            ("精度", self.dtype),
            ("默认规格", self.default_size),
            ("剧本单段上限", f"{self.script_chunk_chars} 字"),
            ("系统内存", f"{self.total_ram_gib:.1f} GiB" if self.total_ram_gib else "—"),
            ("torch", f"{self.torch_version} (CUDA {self.torch_cuda or 'CPU'})"),
            ("驱动", f"{self.driver_version or '—'} / 最高 CUDA {self.driver_cuda or '—'}"),
            ("Python", self.python),
        ]

    def summary(self) -> str:
        """markdown 表格形式，给纯文本场景（日志/终端）用。"""
        return "\n".join(f"| {k} | {v} |" for k, v in self.rows())

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("notes")
        return d


# ---------------------------------------------------------------------------
# 自动决策规则
# ---------------------------------------------------------------------------
def _pick_dtype(backend: str, cc_major: int, requested: str) -> str:
    """决定精度。

    **为什么 N 卡不再选 float16（2026-10-17 实测）**：
    Qwen3-TTS 是照 bfloat16 训的，激活值动态范围大。在 GTX 1650（算力 7.5，
    会自动落到 float16）上实测：code predictor 第 2 层的 MLP 输出冲出
    fp16 上限 65504 → inf；紧接的 RMSNorm 做 `x * rsqrt(var)`，`inf * 0`
    得到 NaN → 采样时 `softmax` 全 NaN → `torch.multinomial` 命中
    device-side assert（`TensorCompare.cu:112 Assertion input[0] != 0 failed`）
    → CUDA 上下文作废，进程被 0xC0000005 直接干掉（界面上就是「生成即闪退」）。
    fp16 在这套权重上不是「精度低一点」，是**跑不起来**，所以整条 N 卡路径
    统一改用 bfloat16：Turing/Volta 没有原生 bf16，靠 fp32 模拟会慢一些，
    但结果是正确的 —— 正确优先于快。
    """
    if requested and requested != "auto":
        return requested
    if backend == "cuda":
        if cc_major >= 7:          # Volta / Turing / Ampere / Ada / Hopper / Blackwell
            return "bfloat16"
        return "float32"           # Pascal 及更老：torch 的 bf16 内核不可用
    if backend == "xpu":
        return "float16"
    if backend == "mps":
        return "float32"           # MPS 上 fp16 易出数值问题，统一 fp32
    return "float32"


def _pick_size(backend: str, vram: float, requested: str) -> str:
    """决定默认用 1.7B 还是 0.6B。

    依据是本机实测：Qwen3-TTS 的 1.7B 在 6 GiB 显存上能跑（官方建议 8-12 GiB，
    6 GiB 属临界可用）；4 GiB 及以下只能上 0.6B。
    """
    if requested and requested != "auto":
        return requested
    if backend == "cpu":
        return "0.6B"
    if vram >= 6:
        return "1.7B"
    return "0.6B"


def _pick_chunk_chars(backend: str, vram: float) -> int:
    """长剧本单段字数上限。段越长，自回归生成的峰值显存越高。

    中文按约 4-5 字/秒估算：100 字≈20 秒音频，对 6 GiB 卡是安全的余量。
    """
    if backend == "cpu":
        return 40
    if vram >= 8:
        return 150
    if vram >= 6:
        return 100
    if vram >= 4:
        return 70
    return 50


def _collect_notes(p: Profile) -> list[str]:
    notes: list[str] = []

    if p.backend == "cuda":
        notes.append(f"已启用 GPU 加速（{p.device_name}，{p.vram_gib:.1f} GiB，{p.dtype}）。")
        if p.dtype == "bfloat16" and p.compute_cap and p.compute_cap.split(".")[0].isdigit() \
                and int(p.compute_cap.split(".")[0]) < 8:
            notes.append(f"本卡（算力 {p.compute_cap}）没有原生 bfloat16，是拿 fp32 模拟的，"
                         "生成会慢一些；但这类卡的 float16 会让本模型数值溢出（实测直接崩），"
                         "所以精度上只能选 bf16。想要更快只能换卡。")
        if p.vram_gib < 6:
            notes.append("显存偏小：已默认选 0.6B 规格。1.7B 需要约 6 GiB，"
                         "强上会在加载或生成时 OOM。")
        elif p.vram_gib < 8:
            notes.append("显存处于临界区间：1.7B 可用，但长剧本会自动切短分段，"
                         "且切换功能时会先卸载上一个模型以腾出显存。")
    elif p.backend in ("xpu", "mps"):
        notes.append(f"已启用 {p.backend.upper()} 加速（{p.device_name}）。")
    else:
        notes.append("未检测到可用 GPU，将使用 CPU 推理（速度可能比显卡慢十倍以上），"
                     "已默认选 0.6B 规格。")
        if p.driver_cuda:
            notes.append(
                f"但本机装有 NVIDIA 驱动（{p.driver_version}，最高支持 CUDA {p.driver_cuda}）——"
                f"说明是 torch 与驱动不匹配：当前 torch 编译于 CUDA {p.torch_cuda or '无（CPU-only 版）'}。"
                f"torch 的 CUDA 版本不能高于驱动支持的版本。"
            )
    if p.total_ram_gib and p.total_ram_gib < 8:
        notes.append("系统内存偏小（< 8 GiB）：加载 1.7B 权重时需约 4 GiB 常驻内存，"
                     "建议改用 0.6B。")
    if p.dtype == "float32" and p.backend == "cuda":
        notes.append("该显卡不支持高效半精度，已回退 float32（显存占用翻倍）。")
    return notes


def detect(
    device: str = "auto",
    dtype: str = "auto",
    default_size: str = "auto",
) -> Profile:
    """探测本机硬件并给出适配档位。参数即「用户覆盖值」，``auto`` 表示交给自动决策。"""
    p = Profile()
    p.python = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    p.torch_version = torch.__version__
    p.torch_cuda = torch.version.cuda or ""
    p.driver_version, p.driver_cuda = _nvidia_smi()
    p.total_ram_gib = round(_total_ram_gib(), 1)

    backend, index = _resolve_backend(device)
    if (device or "auto").lower() != "auto" and backend != (device or "").split(":")[0]:
        p.notes.append(f"你指定了 {device}，但本机不可用，已回退 CPU。")
    p.backend = backend

    cc_major = 0
    if backend == "cuda":
        index = min(index, max(torch.cuda.device_count() - 1, 0))
        props = torch.cuda.get_device_properties(index)
        cc_major, cc_minor = props.major, props.minor
        p.device = f"cuda:{index}" if index else "cuda"
        p.device_name = torch.cuda.get_device_name(index)
        p.vram_gib = round(props.total_memory / 1024**3, 1)
        p.compute_cap = f"{cc_major}.{cc_minor}"
    elif backend == "xpu":
        p.device = "xpu"
        props = torch.xpu.get_device_properties(index)
        p.device_name = getattr(props, "name", "Intel XPU")
        p.vram_gib = round(getattr(props, "total_memory", 0) / 1024**3, 1)
    elif backend == "mps":
        p.device = "mps"
        p.device_name = f"Apple {platform.machine()} (MPS)"
        # 统一内存架构：可用显存约等于系统内存的一部分，用于分档
        p.vram_gib = round(p.total_ram_gib * 0.5, 1)
    else:
        p.device = "cpu"
        p.device_name = platform.processor() or platform.machine() or "CPU"

    p.dtype = _pick_dtype(backend, cc_major, dtype)
    p.default_size = _pick_size(backend, p.vram_gib, default_size)
    p.script_chunk_chars = _pick_chunk_chars(backend, p.vram_gib)
    p.notes = _collect_notes(p)
    return p


def main() -> int:
    # Windows 控制台常是 GBK，遇到非 GBK 字符宁可变 ? 也不要崩
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass

    p = detect()
    print("=" * 64)
    print("Qwen3-TTS Studio — 本机适配档位")
    print("=" * 64)
    print(f"| {'项目':<12} | 值 |")
    print(p.summary())
    print("-" * 64)
    for n in p.notes:
        print(f"- {n}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
