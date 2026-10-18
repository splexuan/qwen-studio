/* 验证：克隆页「不限字数 + 预估 + 多份候选 + 真进度」的界面。

   不碰 GPU：/api/jobs 与 /api/out/promote 全部在浏览器网络层截住，
   伪造一份「已生成 2 份候选」的完成态和一份「第 2/5 段」的进行态。
   （整页 JS 跑在 IIFE 里，从外面既拿不到 paintResult 也改不了 post，
   所以只能从网络层下手 —— 见 tools/verify_postprocess_ui.js 同样的做法。）

   跑法（先起服务，别杀用户自己那个）：
     QWENSTUDIO_PORT=7877 runtime\python\python.exe web/server.py
     NODE_PATH=<...>/node_modules node tools/verify_takes_ui.js
*/
const { chromium } = require("playwright-core");
const fs = require("fs");
const path = require("path");

const BASE = "http://127.0.0.1:" + (process.env.QWENSTUDIO_PORT || "7877");
const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`${ok ? "  OK  " : " FAIL "} ${name}${detail ? "  — " + detail : ""}`);
}

const OUT = path.join(__dirname, "..", "outputs");
const SAMPLE = fs.readdirSync(OUT).find((f) => f.endsWith(".wav")) || "none.wav";

/* 候选是伪造的 URL，但文件得是真的 —— 否则播放器去 decode 会 404，
   控制台一红，最后一个「没有报错」的断言就变成永远失败的噪音。
   顺手也把 /media/cand 这条路由真跑一遍。 */
const CAND_DIR = path.join(__dirname, "..", "web", "_cand", "fakejob");
fs.mkdirSync(CAND_DIR, { recursive: true });
for (const n of ["1.wav", "2.wav"]) {
  fs.copyFileSync(path.join(OUT, SAMPLE), path.join(CAND_DIR, n));
}

const LONG_300 = "长".repeat(300);
const LONG_3000 = "文".repeat(3000);

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
  const page = await ctx.newPage();
  const errs = [];
  page.on("console", (m) => { if (m.type() === "error") errs.push(m.text()); });
  page.on("pageerror", (e) => errs.push(String(e)));

  await page.goto(BASE, { waitUntil: "load" });
  await page.waitForSelector("#c-est", { timeout: 20000 });

  // --- 1. 空框时是引导语，不是 0 段 ---
  const empty = await page.evaluate(() => document.querySelector("#c-est").textContent);
  check("空框时是引导语", /不限字数/.test(empty), empty.slice(0, 40));

  // --- 2. 填了字就出预估 ---
  await page.fill("#c-text", LONG_300);
  await page.waitForTimeout(120);
  const est = await page.evaluate(() => ({
    text: document.querySelector("#c-est").innerText.replace(/\s+/g, " "),
    count: document.querySelector("#c-count").textContent,
  }));
  check("300 字给出段数", /\d+\s*段/.test(est.text), est.text.slice(0, 90));
  check("300 字给出音频时长与生成耗时",
    /音频约/.test(est.text) && /生成约/.test(est.text), est.text.slice(0, 90));
  check("字数胶囊照旧", est.count.includes("300"), est.count);

  // --- 3. 生成份数控件 ---
  const takes = await page.evaluate(() => {
    const box = document.querySelector("#c-result [data-role=takes]");
    if (!box) return null;
    const bs = [...box.querySelectorAll(".seg__b")];
    return {
      n: bs.length,
      labels: bs.map((b) => b.textContent.trim()),
      on: bs.filter((b) => b.classList.contains("is-on")).map((b) => b.dataset.n),
    };
  });
  check("有生成份数控件", !!takes, JSON.stringify(takes));
  check("份数按钮 1–4", takes && takes.n === 4, takes && JSON.stringify(takes.labels));
  check("默认选 1 份", takes && takes.on.join(",") === "1", takes && takes.on.join(","));

  // --- 4. 选多份时把「边生成边播」灰掉（而不是留着偷偷不生效） ---
  await page.click("#c-result [data-role=takes] .seg__b[data-n='3']");
  await page.waitForTimeout(80);
  const off = await page.evaluate(() => {
    const s = document.querySelector("#c-result [data-role=streamon]");
    return { disabled: s.disabled, dim: s.closest(".streamsw").classList.contains("is-off"),
             title: s.closest(".streamsw").title };
  });
  check("选 3 份后流式开关被禁用", off.disabled === true, JSON.stringify(off));
  check("并且给出了原因", /多份候选/.test(off.title || ""), off.title);

  await page.click("#c-result [data-role=takes] .seg__b[data-n='1']");
  await page.waitForTimeout(80);
  const on = await page.evaluate(() =>
    !document.querySelector("#c-result [data-role=streamon]").disabled);
  check("选回 1 份后流式开关恢复", on);

  // --- 5. 超过旧的 2000 字上限不再被拦 ---
  // 先挑一个音色（克隆页没有音色不能提交）
  await page.click("#c-pick");
  await page.waitForSelector(".vrow", { timeout: 8000 });
  await page.click(".vrow");
  await page.waitForTimeout(200);

  await page.fill("#c-text", LONG_3000);
  await page.waitForTimeout(150);
  const est3k = await page.evaluate(() =>
    document.querySelector("#c-est").innerText.replace(/\s+/g, " "));
  check("3000 字仍能给出预估（没被拒）", /\d+\s*段/.test(est3k), est3k.slice(0, 90));

  // 选 2 份，让 payload 里带 n
  await page.click("#c-result [data-role=takes] .seg__b[data-n='2']");

  let sent = null;
  let jobPolls = 0;
  let mode = "candidates";
  await page.route("**/api/jobs", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    try { sent = JSON.parse(route.request().postData() || "{}"); } catch (_) { sent = null; }
    jobPolls = 0;
    await route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ ok: true, job: "fakejob" }),
    });
  });
  await page.route("**/api/jobs/fakejob", async (route) => {
    jobPolls++;
    if (mode === "progress" && jobPolls <= 3) {
      return route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify({
          ok: true, state: "running", progress: 0.4, pieces: 5, piece: 2,
          message: "第 2/5 段",
        }),
      });
    }
    if (mode === "progress") {
      return route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify({
          ok: true, state: "done", progress: 1,
          message: "音频 3.0s，耗时 2.0s，RTF 0.67",
          result: { file: SAMPLE, url: "/media/out/" + encodeURIComponent(SAMPLE),
                    duration: 2.0, level: "ok", took: 2.0 },
        }),
      });
    }
    return route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        ok: true, state: "done", progress: 1,
        message: "生成了 2 份候选 —— 听过之后挑一条留下，其余自动丢弃。",
        result: {
          takes: 2, level: "ok", took: 6.0,
          candidates: [
            { index: 1, file: "1.wav", url: "/media/cand/fakejob/1.wav",
              duration: 3.4, level: "ok", postprocess: { applied: true } },
            { index: 2, file: "2.wav", url: "/media/cand/fakejob/2.wav",
              duration: 3.1, level: "ok", postprocess: { applied: true } },
          ],
        },
      }),
    });
  });

  await page.evaluate(() => document.querySelector("#c-go").click());
  await page.waitForTimeout(1600);

  check("payload 抓到了", !!sent, sent ? "" : "提交没触发");
  if (sent) {
    check("3000 字原样提交（前端不再拦字数）",
      (sent.text || "").length === 3000, String((sent.text || "").length));
    check("payload 带 n=2", Number(sent.n) === 2, String(sent.n));
  }

  // --- 6. 候选列表 ---
  const cand = await page.evaluate(() => {
    const box = document.querySelector("#c-result [data-role=cands]");
    const rows = [...box.querySelectorAll(".cand")];
    return {
      hidden: box.classList.contains("hidden"),
      n: rows.length,
      nums: rows.map((r) => r.querySelector(".cand__n").textContent.trim()),
      hasTry: rows.every((r) => r.querySelector("[data-role=try]")),
      hasKeep: rows.every((r) => r.querySelector("[data-role=keep]")),
      badge: document.querySelector("#c-result [data-role=badge]").textContent.trim(),
      metricsHidden: document.querySelector("#c-result [data-role=metrics]")
        .classList.contains("hidden"),
      params: document.querySelector("#c-result [data-role=params]").innerText
        .replace(/\s+/g, " "),
    };
  });
  check("候选列表露出来了", !cand.hidden && cand.n === 2, JSON.stringify({ n: cand.n }));
  check("每行有序号", cand.nums.join(",") === "1,2", cand.nums.join(","));
  check("每行都有「试听」「选这条」", cand.hasTry && cand.hasKeep);
  check("徽章写「2 份待挑」", cand.badge.includes("待挑"), cand.badge);
  check("还没挑选时没有「文件 / 时长」指标行", cand.metricsHidden);
  check("参数表照常显示", /本次参数/.test(cand.params), cand.params.slice(0, 60));

  // --- 7. 点「试听」把候选塞进播放器 ---
  await page.click("#c-result .cand[data-i='2'] [data-role=try]");
  await page.waitForTimeout(400);
  const tried = await page.evaluate(() => ({
    rowOn: document.querySelector("#c-result .cand[data-i='2']").classList.contains("is-on"),
    // buildPlayer 里的 <audio> 是用 new Audio() 建的，不在 DOM 里 ——
    // 能读到 URL 的地方只有下载链接的 href
    href: (document.querySelector("#c-result [data-role=playerbox] [data-role=dl]") || {}).href || "",
    shown: !document.querySelector("#c-result [data-role=playerbox]").classList.contains("hidden"),
  }));
  check("试听把该行标成当前", tried.rowOn);
  check("播放器指向该候选", /\/media\/cand\/fakejob\/2\.wav/.test(tried.href), tried.href);
  check("播放器露出来了", tried.shown);

  // --- 8. 点「选这条」→ 调 promote 并重画成普通结果卡 ---
  let promoteBody = null;
  await page.route("**/api/out/promote", async (route) => {
    try { promoteBody = JSON.parse(route.request().postData() || "{}"); } catch (_) { promoteBody = null; }
    await route.fulfill({
      status: 200, contentType: "application/json",
      // 用真存在的那份 wav 当「转正后的文件」—— 编一个不存在的名字，播放器
      // 去 decode 会 404，最后那条「没有控制台报错」就永远红着。
      body: JSON.stringify({
        ok: true, file: SAMPLE, url: "/media/out/" + encodeURIComponent(SAMPLE),
        duration: 3.1, level: "ok", took: 6.0, pick: 2, takes: 2,
        message: "已采用第 2 份（其余 1 份已丢弃）",
        postprocess: { applied: true, silence: true, normalize: true, fade: true,
                       cut_secs: 0.1, gain_db: 2.0, mid_ms: 500, target_db: -20 },
      }),
    });
  });
  await page.click("#c-result .cand[data-i='2'] [data-role=keep]");
  await page.waitForTimeout(700);

  check("promote 收到了 job", promoteBody && promoteBody.job === "fakejob",
    JSON.stringify(promoteBody));
  check("promote 收到的是第 2 份", promoteBody && Number(promoteBody.index) === 2,
    promoteBody && String(promoteBody.index));

  const after = await page.evaluate(() => ({
    candsHidden: document.querySelector("#c-result [data-role=cands]").classList.contains("hidden"),
    badge: document.querySelector("#c-result [data-role=badge]").textContent.trim(),
    metrics: document.querySelector("#c-result [data-role=metrics]").innerText
      .replace(/\s+/g, " "),
    // buildPlayer 里的 <audio> 是用 new Audio() 建的，不在 DOM 里 ——
    // 能读到 URL 的地方只有下载链接的 href
    href: (document.querySelector("#c-result [data-role=playerbox] [data-role=dl]") || {}).href || "",
  }));
  check("挑选后候选列表收起来", after.candsHidden);
  check("徽章回到「已完成」", after.badge === "已完成", after.badge);
  check("指标行出现文件名", after.metrics.includes(SAMPLE), after.metrics.slice(0, 80));
  check("播放器指向正式文件", after.href.includes("/media/out/"), after.href);

  // --- 9. 真进度：后端给 pieces/piece 时进度条不再是时间缓动 ---
  mode = "progress";
  await page.click("#c-result [data-role=takes] .seg__b[data-n='1']");
  await page.evaluate(() => document.querySelector("#c-go").click());
  await page.waitForTimeout(900);
  const run = await page.evaluate(() => ({
    stage: document.querySelector("#c-result [data-role=stage]").textContent.trim(),
    pct: document.querySelector("#c-result [data-role=pct]").textContent.trim(),
    msg: document.querySelector("#c-result [data-role=msg]").textContent.trim(),
    width: document.querySelector("#c-result [data-role=bar] i").style.width,
  }));
  check("进行态显示「第 2/5 段」", /第 2\/5 段/.test(run.stage), run.stage);
  check("百分比取的是后端真值 40%", run.pct === "40%", run.pct);
  check("进度条宽度跟着真值", run.width === "40%", run.width);
  check("附带了剩余时间预估", /已等待/.test(run.msg), run.msg);

  await page.waitForTimeout(1600);   // 让它在 done 之后停稳

  check("没有控制台报错", errs.length === 0, errs.slice(0, 2).join(" | "));

  fs.rmSync(CAND_DIR, { recursive: true, force: true });
  await browser.close();
  const bad = results.filter((r) => !r.ok);
  console.log(`\n${bad.length ? "❌" : "✅"} ${results.length - bad.length}/${results.length} 通过`);
  process.exit(bad.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
