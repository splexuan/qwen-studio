/* 验证：克隆页「不限字数 + 预估 + 真进度」的界面。

   不碰 GPU：/api/jobs 在浏览器网络层截住，伪造一份「第 2/5 段」的进行态
   和一份完成态。
   （整页 JS 跑在 IIFE 里，从外面既拿不到 paintResult 也改不了 post，
   所以只能从网络层下手 —— 见 tools/verify_postprocess_ui.js 同样的做法。）

   跑法（先起服务，别杀用户自己那个）：
     QWENSTUDIO_PORT=7877 runtime\python\python.exe web/server.py
     NODE_PATH=<...>/node_modules node tools/verify_split_ui.js
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

  // --- 3. 结果卡上不该再有「生成份数」这类控件 ---
  const noTakes = await page.evaluate(() => ({
    takes: document.querySelectorAll("#c-result [data-role=takes], #c-result [data-role=cands]").length,
    text: document.querySelector("#c-result").innerText,
  }));
  check("结果卡没有「生成份数」控件", noTakes.takes === 0, String(noTakes.takes));
  check("结果卡文案里没有「候选」", !/候选|份数/.test(noTakes.text));

  // 流式开关始终可用（不再有「选多份就灰掉」的分支）
  const streamOK = await page.evaluate(() => {
    const s = document.querySelector("#c-result [data-role=streamon]");
    return { disabled: s.disabled, title: s.closest(".streamsw").title };
  });
  check("流式开关可用", streamOK.disabled === false, JSON.stringify(streamOK));

  // --- 4. 超过旧的 2000 字上限不再被拦 ---
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
  check("3000 字预估的段数 > 1（会自动切段）",
    (parseInt((est3k.match(/(\d+)\s*段/) || [])[1], 10) || 0) > 1, est3k.slice(0, 60));

  let sent = null;
  let jobPolls = 0;
  await page.route("**/api/jobs", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    try { sent = JSON.parse(route.request().postData() || "{}"); } catch (_) { sent = null; }
    jobPolls = 0;
    await route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ ok: true, job: "fakejob" }),
    });
  });
  // 前 3 次轮询给「第 2/5 段」的进行态，之后给完成态
  await page.route("**/api/jobs/fakejob", async (route) => {
    jobPolls++;
    if (jobPolls <= 3) {
      return route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify({
          ok: true, state: "running", progress: 0.4, pieces: 5, piece: 2,
          message: "第 2/5 段",
        }),
      });
    }
    return route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        ok: true, state: "done", progress: 1,
        message: "音频 3.0s，耗时 2.0s，RTF 0.67",
        result: { file: SAMPLE, url: "/media/out/" + encodeURIComponent(SAMPLE),
                  duration: 2.0, level: "ok", took: 2.0 },
      }),
    });
  });

  await page.evaluate(() => document.querySelector("#c-go").click());
  await page.waitForTimeout(900);

  check("payload 抓到了", !!sent, sent ? "" : "提交没触发");
  if (sent) {
    check("3000 字原样提交（前端不再拦字数）",
      (sent.text || "").length === 3000, String((sent.text || "").length));
    check("payload 里没有 n（不再有多份候选）", sent.n === undefined, String(sent.n));
  }

  // --- 5. 真进度：后端给 pieces/piece 时进度条不再是时间缓动 ---
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

  await page.waitForTimeout(1800);   // 让它在 done 之后停稳

  const done = await page.evaluate(() => ({
    badge: document.querySelector("#c-result [data-role=badge]").textContent.trim(),
    metrics: document.querySelector("#c-result [data-role=metrics]").innerText
      .replace(/\s+/g, " "),
    href: (document.querySelector("#c-result [data-role=playerbox] [data-role=dl]") || {}).href || "",
  }));
  check("完成后徽章是「已完成」", done.badge === "已完成", done.badge);
  check("指标行出现文件名", done.metrics.includes(SAMPLE), done.metrics.slice(0, 80));
  check("播放器指向正式文件", done.href.includes("/media/out/"), done.href);

  check("没有控制台报错", errs.length === 0, errs.slice(0, 2).join(" | "));

  await browser.close();
  const bad = results.filter((r) => !r.ok);
  console.log(`\n${bad.length ? "❌" : "✅"} ${results.length - bad.length}/${results.length} 通过`);
  process.exit(bad.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
