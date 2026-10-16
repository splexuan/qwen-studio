# -*- coding: utf-8 -*-
r"""把「按条件分组的对照样本」汇总成一个可以逐条点着听的 HTML 页面。

指标现场算（复用 tools/measure_prosody.py），播放地址指向正在运行的服务
（默认 7867）的 `/media/out/`，所以不复制文件、不占新端口。
所有数字（均值、标准差、相对基线的差值）都是现场算的，不写死。

输入是 tools/ab_emotion.py 这类脚本产出的条件 JSON
（每项 {code, note, text, instruct, files:[...]}），可给多个文件，会合并。

    runtime\\python\\python.exe tools\\emotion_report.py tools\\_ab\\_cond.json tools\\_ab\\_cond2.json ^
        --out 情绪对照_悲痛绝望.html --title "悲痛绝望的嘶喊：哪种做法有效" ^
        --lead "同一音色、仅向量，每组 5–6 条" --notes tools\_ab\_cond_notes.txt

`--notes` 是纯文本：空行分段，以 `### ` 开头的行当小标题。
"""
from __future__ import annotations

import html
import importlib.util
import json
import statistics as S
import sys
from pathlib import Path

PORT = "7867"
BASE = f"http://127.0.0.1:{PORT}"

# 键 -> (显示名, 小数位, 单位)
METRICS = [
    ("dur", "时长", 2, "s"),
    ("f0", "基频", 1, "Hz"),
    ("range_st", "音高起伏", 1, "半"),
    ("dyn_db", "能量动态", 1, "dB"),
    ("onset_rate", "起音/秒", 2, ""),
    ("hnr_db", "谐噪比", 1, "dB"),
]


def load_measure():
    spec = importlib.util.spec_from_file_location(
        "measure_prosody", Path(__file__).with_name("measure_prosody.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.measure


def parse_args(argv):
    opts = {"files": [], "out": None, "title": "情绪对照试听", "lead": "", "notes": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            opts[a[2:]] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
        else:
            opts["files"].append(a)
            i += 1
    return opts


def bar(value, lo, hi, color, nd):
    pct = 0.0 if hi <= lo else max(0.0, min(1.0, (value - lo) / (hi - lo))) * 100
    return (f'<div class="bar"><i style="width:{pct:.0f}%;background:{color}"></i>'
            f'<b>{value:.{nd}f}</b></div>')


def main():
    opts = parse_args(sys.argv[1:])
    if not opts["files"]:
        print(__doc__)
        return
    measure = load_measure()

    groups = []
    for f in opts["files"]:
        data = json.loads(Path(f).read_text(encoding="utf-8"))
        groups.extend(data if isinstance(data, list) else [data])

    stats = []
    for g in groups:
        ms = [measure(Path("outputs") / fn) for fn in g["files"]]
        if ms:
            stats.append((g, ms))
    if not stats:
        print("没有可用的样本。")
        return

    spans = {}
    for key, *_ in METRICS:
        vals = [m[key] for _, ms in stats for m in ms]
        spans[key] = (min(vals), max(vals))

    base_key = stats[0][0]["code"]
    base = {k: [m[k] for m in stats[0][1]] for k, *_ in METRICS}
    base_note = stats[0][0]["note"]

    parts = [f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(opts['title'])}</title>
<style>
  :root {{ --bg:#f6f7fb; --card:#fff; --fg:#1c1d22; --dim:#6b6f7d; --line:#e6e8f0;
           --acc:#6b5cf6; --ok:#17a673; --warn:#e5484d;
           --c1:#6b5cf6; --c2:#f0a020; --c3:#17a673; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#16171c; --card:#1f2027; --fg:#eceef4; --dim:#9aa0b0; --line:#2c2e38;
             --acc:#8b7cff; --ok:#34c98d; --warn:#ff6369;
             --c1:#8b7cff; --c2:#f5b544; --c3:#34c98d; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:26px 18px 64px; background:var(--bg); color:var(--fg);
         font:15px/1.65 "Microsoft YaHei","PingFang SC",system-ui,sans-serif; }}
  .wrap {{ max-width:1120px; margin:0 auto; }}
  h1 {{ font-size:22px; margin:0 0 6px; }}
  .lead {{ color:var(--dim); font-size:13.5px; margin:0 0 20px; }}
  .sec {{ font-size:15.5px; font-weight:700; margin:0 0 10px; }}
  .sec small {{ font-weight:400; color:var(--dim); font-size:12.5px; margin-left:8px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:14px;
           padding:12px 14px; margin:0 0 20px; }}
  table {{ width:100%; border-collapse:collapse; font-size:12.5px;
           font-variant-numeric:tabular-nums; }}
  th,td {{ padding:7px 8px; border-bottom:1px solid var(--line); text-align:right;
           white-space:nowrap; }}
  th:first-child,td:first-child {{ text-align:left; white-space:normal; }}
  th {{ color:var(--dim); font-weight:600; font-size:11.5px; }}
  .mark {{ color:var(--warn); font-weight:700; }}
  .row {{ display:grid; grid-template-columns:1fr 290px; gap:12px; align-items:center;
          padding:8px 2px; border-bottom:1px solid var(--line); }}
  .row:last-child {{ border-bottom:0; }}
  .mline {{ display:grid; grid-template-columns:repeat(6,1fr); gap:7px; }}
  .mx label {{ display:block; font-size:10px; color:var(--dim); }}
  .bar {{ position:relative; height:14px; border-radius:5px; overflow:hidden;
          background:color-mix(in srgb, var(--fg) 8%, transparent); }}
  .bar i {{ position:absolute; inset:0 auto 0 0; border-radius:5px; }}
  .bar b {{ position:absolute; right:4px; top:50%; transform:translateY(-50%);
            font-size:10px; color:var(--fg); text-shadow:0 0 3px var(--card); }}
  audio {{ width:100%; height:32px; }}
  .note {{ background:var(--card); border:1px solid var(--line);
           border-left:3px solid var(--acc); border-radius:12px;
           padding:13px 16px; margin:0 0 14px; font-size:13.5px; }}
  .note h3 {{ margin:0 0 6px; font-size:14px; color:var(--acc); }}
  .note p {{ margin:0; }}
  code {{ background:color-mix(in srgb, var(--fg) 8%, transparent);
          padding:1px 4px; border-radius:4px; }}
</style></head><body><div class="wrap">
<h1>{html.escape(opts['title'])}</h1>
<p class="lead">{opts['lead']}</p>
"""]

    parts.append('<div class="sec">各组均值 ± 标准差<small>比均值，不比单条 —— '
                 '同参数的重复样本本身就差很多</small></div>')
    parts.append('<div class="card"><table><tr><th>条件</th>'
                 + "".join(f"<th>{n} <span style='font-weight:400'>{u}</span></th>"
                           for _, n, _, u in METRICS)
                 + "<th>条数</th></tr>")
    for g, ms in stats:
        parts.append(f"<tr><td>{html.escape(g['code'])} {html.escape(g['note'])}</td>")
        for key, name, nd, unit in METRICS:
            v = [m[key] for m in ms]
            parts.append(f"<td>{S.mean(v):.{nd}f} ± {S.pstdev(v):.{nd}f}</td>")
        parts.append(f"<td>{len(ms)}</td></tr>")
    parts.append("</table>")

    parts.append(
        f'<p class="lead" style="margin:12px 0 0">相对 {html.escape(base_key)}'
        f'（{html.escape(base_note)}）的差值；'
        f'<span class="mark">红色带 *</span> = 差值超过基线自身标准差，值得当真。</p>')
    parts.append('<table><tr><th>条件</th>'
                 + "".join(f"<th>{n}</th>" for _, n, _, _ in METRICS) + "</tr>")
    for g, ms in stats[1:]:
        parts.append(f"<tr><td>{html.escape(g['code'])} {html.escape(g['note'])}</td>")
        for key, name, nd, unit in METRICS:
            v = [m[key] for m in ms]
            d = S.mean(v) - S.mean(base[key])
            sd = S.pstdev(base[key]) if len(base[key]) > 1 else 0
            parts.append(f'<td class="mark">{d:+.{nd}f} *</td>' if abs(d) > sd
                         else f"<td>{d:+.{nd}f}</td>")
        parts.append("</tr>")
    parts.append("</table></div>")

    for g, ms in stats:
        parts.append(f'<div class="sec">{html.escape(g["code"])} '
                     f'{html.escape(g["note"])}'
                     f'<small>指令：{html.escape(g["instruct"] or "（空）")}'
                     f' · {len(ms)} 条</small></div>')
        parts.append('<div class="card"><p class="lead" style="margin:0 0 10px">文本：'
                     f'{html.escape(g["text"])}</p>')
        for fn, m in zip(g["files"], ms):
            parts.append('<div class="row"><div class="mline">')
            for key, name, nd, unit in METRICS:
                lo, hi = spans[key]
                color = {"f0": "var(--c1)", "dyn_db": "var(--c2)",
                         "onset_rate": "var(--c3)"}.get(key, "var(--dim)")
                parts.append(f'<div class="mx"><label>{name}</label>'
                             f'{bar(m[key], lo, hi, color, nd)}</div>')
            parts.append(f'</div><audio controls preload="metadata" '
                         f'src="{BASE}/media/out/{fn}"></audio></div>')
        parts.append("</div>")

    if opts.get("notes") and Path(opts["notes"]).exists():
        parts.append('<div class="sec">结论</div>')
        blocks, head, lines = [], None, []
        for line in Path(opts["notes"]).read_text(encoding="utf-8").splitlines():
            if line.startswith("### "):
                if head or lines:
                    blocks.append((head, lines))
                head, lines = line[4:].strip(), []
            elif line.strip():
                lines.append(html.escape(line))
            elif lines:
                lines.append("")   # 段内空行也算一次换行，保留呼吸感
        if head or lines:
            blocks.append((head, lines))
        for head, lines in blocks:
            body = "<br>".join(lines).rstrip("<br>")
            h = f"<h3>{html.escape(head)}</h3>" if head else ""
            parts.append(f'<div class="note">{h}<p>{body}</p></div>')

    parts.append("</div></body></html>")

    out = opts["out"] or "情绪对照.html"
    Path(out).write_text("\n".join(parts), encoding="utf-8")
    print(f"已生成 {out}")
    print()
    print(f"{'条件':<30}" + "".join(f"{n:>12}" for _, n, _, _ in METRICS))
    print("-" * 102)
    for g, ms in stats:
        line = f"{g['code'] + ' ' + g['note']:<30}"
        for key, name, nd, unit in METRICS:
            v = [m[key] for m in ms]
            line += f"{S.mean(v):>12.{nd}f}"
        print(line)


if __name__ == "__main__":
    main()
