"""音频后处理自检：直接跑函数，不碰模型。

    runtime\\python\\python.exe tools\\verify_postproc.py

构造几段已知形状的音频，逐项核对「去长静音 / 音量归一化 / 淡入淡出」
是否按预期生效。不依赖 GPU，也不生成任何 outputs/ 文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import postproc as pp  # noqa: E402

SR = 24000
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)


def tone(sec: float, freq: float = 220.0, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(SR * sec), dtype=np.float32) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(sec: float) -> np.ndarray:
    return np.zeros(int(SR * sec), dtype=np.float32)


def rms_db(x: np.ndarray) -> float:
    r = float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if len(x) else 0.0
    return 20.0 * np.log10(max(r, 1e-9))


print("1) 去长静音")
# 0.8 秒声音 + 1.5 秒停顿 + 0.8 秒声音，首尾各 1 秒静音
x = np.concatenate([silence(1.0), tone(0.8), silence(1.5), tone(0.8), silence(1.0)])
out, cut = pp.remove_long_silences(x, SR, mid_ms=500, edge_ms=100, keep_ms=500)
want = 0.1 + 0.8 + 0.5 + 0.8 + 0.1
check("长度收敛到「首尾各 0.1s + 中间压到 0.5s」",
      abs(len(out) / SR - want) < 0.12, f"实测 {len(out)/SR:.2f}s / 预期 ≈{want:.2f}s")
check("报告的裁剪量自洽", abs(cut - (len(x) - len(out)) / SR) < 0.01, f"{cut}s")

out2, cut2 = pp.remove_long_silences(np.concatenate([tone(1.0)]), SR, mid_ms=500)
check("没有长停顿时几乎不动", abs(len(out2) / SR - 1.0) < 0.05 and cut2 < 0.05,
      f"{len(out2)/SR:.2f}s")

out3, _ = pp.remove_long_silences(np.concatenate([tone(0.4), silence(0.05), tone(0.4)]), SR,
                                  mid_ms=500)
check("短停顿（50ms）不被压", abs(len(out3) / SR - 0.85) < 0.06, f"{len(out3)/SR:.2f}s")

out4, cut4 = pp.remove_long_silences(silence(1.0), SR)
check("全静音不返回空数组", len(out4) == len(silence(1.0)) and cut4 == 0.0,
      f"len={len(out4)}")

print("2) 音量归一化")
quiet = tone(2.0, amp=0.02)                        # 约 -37 dBFS
out, gain = pp.normalize_loudness(quiet, SR, target_db=-20.0)
got = rms_db(out)
check("小音量被拉到 -20 dBFS 附近", abs(got + 20.0) < 1.2, f"实测 {got:.1f} dBFS")
check("增益方向与量级正确", 15.0 < gain < 19.0, f"{gain:+.1f} dB")

loud = tone(2.0, amp=0.8)
out, gain = pp.normalize_loudness(loud, SR, target_db=-20.0)
check("大音量被压下来", abs(rms_db(out) + 20.0) < 1.2, f"{rms_db(out):.1f} dBFS")
check("增益为负", gain < -10, f"{gain:+.1f} dB")

# 峰值保护：一串很尖的脉冲，放大后必须不削波
spiky = np.concatenate([tone(1.0, amp=0.005), np.array([0.9, -0.9], np.float32)])
out, _g = pp.normalize_loudness(spiky, SR, target_db=-10.0)
check("峰值被压在 0.97 以内", float(np.abs(out).max()) <= 0.9701,
      f"peak={float(np.abs(out).max()):.3f}")

print("3) 淡入淡出与留白")
x = tone(1.0, amp=0.5)
out = pp.fade_and_pad(x, SR, fade_sec=0.1, pad_sec=0.1)
check("两端各补 0.1s", abs(len(out) / SR - 1.2) < 0.01, f"{len(out)/SR:.2f}s")
check("开头是纯静音", abs(float(out[0])) < 1e-6 and abs(float(out[int(0.05*SR)])) < 1e-6)
# 220 Hz 正弦在个别采样点上本来就可能是 0，所以比较包络峰值而不是瞬时值
body = out[int(0.12 * SR): int(0.5 * SR)]          # 跳过留白与淡入段
check("主体没有衰减", float(np.abs(body).max()) > 0.45, f"peak={float(np.abs(body).max()):.2f}")
check("末尾是纯静音", abs(float(out[-1])) < 1e-6)
# 淡入段（留白之后 0.1 秒）逐块取峰值，应当单调上升
env = [float(np.abs(out[int((0.1 + i * 0.02) * SR): int((0.1 + (i + 1) * 0.02) * SR)]).max())
       for i in range(5)]
check("淡入包络单调上升", all(env[i] <= env[i + 1] + 1e-6 for i in range(4)),
      " → ".join(f"{v:.2f}" for v in env))

short = pp.fade_and_pad(tone(0.12, amp=0.5), SR)
check("极短音频不会被淡没", float(np.abs(short).max()) > 0.1, f"peak={float(np.abs(short).max()):.2f}")

print("4) 组合流程")
x = np.concatenate([silence(1.2), tone(1.0, amp=0.04), silence(1.8), tone(1.0, amp=0.04), silence(1.0)])
out, st = pp.apply(x, SR, pp.options({}))
check("apply 默认三项全开", st["silence"] and st["normalize"] and st["fade"])
check("时长明显收缩", len(out) / SR < 4.0, f"{len(x)/SR:.1f}s → {len(out)/SR:.1f}s")
check("响度接近目标", abs(rms_db(out[int(0.2*SR):int(-0.2*SR)]) + 20.0) < 3.0,
      f"{rms_db(out[int(0.2*SR):int(-0.2*SR)]):.1f} dBFS")
check("无削波", float(np.abs(out).max()) <= 0.9701, f"peak={float(np.abs(out).max()):.3f}")
check("无 NaN", bool(np.isfinite(out).all()))
check("summary 可读", len(pp.summary(st)) > 2, pp.summary(st))

out, st = pp.apply(x, SR, pp.options({"postprocess": False}))
check("postprocess=false 原样返回", len(out) == len(x) and not st["applied"])

out, st = pp.apply(np.array([np.nan, 0.1, np.inf, -0.2], np.float32), SR, pp.options({}))
check("NaN/inf 被清掉", bool(np.isfinite(out).all()), str(out[:4]))

# ---------------------------------------------------------------------------
# 5. 拿真实生成音频抽一遍 —— 正弦波只能证明代码没写错，证明不了「有用」
# ---------------------------------------------------------------------------
print("5) 真实音频抽样（outputs/ 里最近 8 条，处理前 → 处理后）")
import soundfile as sf  # noqa: E402

files = sorted((ROOT / "outputs").glob("*.wav"),
               key=lambda p: p.stat().st_mtime, reverse=True)[:8]
if not files:
    print("  --   outputs/ 里没有 wav，跳过")
else:
    rows = []
    ok = True
    for f in files:
        try:
            x, sr = sf.read(str(f), dtype="float32")
        except Exception as e:  # noqa: BLE001
            print(f"  --   {f.name} 读不出来（{e}），跳过")
            continue
        if getattr(x, "ndim", 1) > 1:
            x = x.mean(axis=1)
        y, st = pp.apply(x, sr, pp.options({}))
        before, after = len(x) / sr, len(y) / sr
        peak = float(np.abs(y).max()) if len(y) else 0.0
        # 有声部分的响度前后各是多少
        def voiced_db(a):
            db, _ = pp.frame_db(a, sr)
            v = db[db > pp.SILENCE_DB]
            return float(np.median(v)) if len(v) else -180.0
        b_db, a_db = voiced_db(x), voiced_db(y)
        rows.append((f.name, before, after, st["cut_secs"], st["gain_db"], b_db, a_db))
        if not (len(y) > 0 and np.isfinite(y).all() and peak <= 0.9701
                and after <= before + 0.25 and abs(a_db + 20.0) < 4.0):
            ok = False
            print(f"  FAIL {f.name}: {before:.2f}s→{after:.2f}s peak={peak:.2f} "
                  f"响度 {b_db:.1f}→{a_db:.1f}")
    for name, b, a, cut, g, b_db, a_db in rows:
        print(f"  {name[:34]:<34} {b:5.2f}s → {a:5.2f}s   剪 {cut:4.1f}s   "
              f"增益 {g:+5.1f} dB   有声响度 {b_db:6.1f} → {a_db:6.1f} dBFS")
    check("每条都：非空 / 无 NaN / 不削波 / 不长于原始", ok)
    if rows:
        avg_cut = sum(r[3] for r in rows) / len(rows)
        moved = sum(1 for r in rows if abs(r[6] + 20.0) < abs(r[5] + 20.0))
        print(f"  --   平均每条剪掉 {avg_cut:.2f}s 静音；{moved}/{len(rows)} 条的响度更接近 −20 dBFS")

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：" + "、".join(FAILS))
    sys.exit(1)
print("✅ 全部通过")
