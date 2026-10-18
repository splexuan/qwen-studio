"""验证 synthesize() 的后处理挂钩：用假音频代替模型输出，不加载权重。

    runtime\\python\\python.exe tools\\verify_postproc_hook.py

为什么单独一个脚本：这里要 import core（连带 torch），十几秒起步；
而 verify_postproc.py 是纯 DSP 自检，毫秒级。混在一起会把「改一行 DSP 就
跑一下」的成本抬到没法忍。这个脚本只在改动 core.synthesize 的返回结构
或后处理接入位置时才需要跑。
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import core  # noqa: E402

SR = 24000
FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)


def fake() -> tuple[np.ndarray, int]:
    """1s 静音 + 1s 小音量音 + 1.5s 静音 + 1s 小音量音 + 1s 静音。"""
    t = np.arange(int(SR * 1.0), dtype=np.float32) / SR
    tone = (0.03 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    z = lambda s: np.zeros(int(SR * s), np.float32)
    return np.concatenate([z(1.0), tone, z(1.5), tone, z(1.0)]), SR


core._gen_design = lambda text, lang, instr: fake()

print("1) 默认（后处理全开）")
wav, sr, status, level, pp = core.synthesize({
    "mode": "design", "text": "测试文本", "language": "Chinese",
})
check("返回 5 元组", sr == SR and level == "ok")
check("音频被压短", len(wav) / sr < 4.5, f"{len(wav)/sr:.2f}s（原始 5.5s）")
check("pp.applied", pp.get("applied") is True)
check("剪掉≈2.6s", abs(pp.get("cut_secs", 0) - 2.6) < 0.3, f"{pp.get('cut_secs')}s")
check("括号里有实际增益", abs(pp.get("gain_db", 0)) > 10, f"{pp.get('gain_db')} dB")
check("状态行第二行讲清后处理", "后处理：" in status, status.splitlines()[1] if len(status.splitlines()) > 1 else status)
check("不削波", float(np.abs(wav).max()) <= 0.9701, f"{float(np.abs(wav).max()):.3f}")

print("2) postprocess=false")
wav2, _, status2, _, pp2 = core.synthesize({
    "mode": "design", "text": "测试文本", "language": "Chinese", "postprocess": False,
})
check("音频原样", abs(len(wav2) / sr - 5.5) < 0.05, f"{len(wav2)/sr:.2f}s")
check("pp.applied=false", pp2.get("applied") is False)
check("状态行不写后处理", "后处理" not in status2, status2.splitlines()[-1])

print("3) 只关归一化")
wav3, _, _, _, pp3 = core.synthesize({
    "mode": "design", "text": "测试文本", "language": "Chinese",
    "pp_normalize": False, "pp_fade": False,
})
check("只剪静音", pp3["silence"] and not pp3["normalize"] and not pp3["fade"],
      str({k: pp3[k] for k in ("silence", "normalize", "fade")}))
check("没有补留白（结尾是原始静音）", len(wav3) / sr < 4.0, f"{len(wav3)/sr:.2f}s")

print("4) 越界参数被夹住")
_, _, _, _, pp4 = core.synthesize({
    "mode": "design", "text": "测试文本", "language": "Chinese",
    "pp_mid_ms": 99999, "pp_target_db": 999,
})
check("mid_ms 夹到 3000", pp4["mid_ms"] == 3000, str(pp4["mid_ms"]))
check("target_db 夹到 -6", abs(pp4["target_db"] + 6) < 1e-6, str(pp4["target_db"]))

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：" + "、".join(FAILS))
    sys.exit(1)
print("✅ 全部通过")
