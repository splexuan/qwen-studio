/* 验证：输出后处理的界面（折叠组、默认值、持久化、提交的 payload）。

   只验「前端把话说清了、把参数发出去了」，不验音频本身 ——
   那是 tools/verify_postproc.py 的事。

   跑法（先起服务）：
     QWENSTUDIO_PORT=7877 runtime\python\python.exe web/server.py
     NODE_PATH=<...>/node_modules QWENSTUDIO_PORT=7877 node tools/verify_postprocess_ui.js
*/
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:" + (process.env.QWENSTUDIO_PORT || "7877");
const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`${ok ? "  OK  " : " FAIL "} ${name}${detail ? "  — " + detail : ""}`);
}

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  // 独立上下文：共用 localStorage 会把这次改的勾选留成下次的起点
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await ctx.newPage();
  const errs = [];
  page.on("console", (m) => { if (m.type() === "error") errs.push(m.text()); });
  page.on("pageerror", (e) => errs.push(String(e)));

  await page.goto(BASE, { waitUntil: "load" });
  await page.waitForSelector("#c-adv details", { timeout: 20000 });

  // --- 1. 四页都长出了这一组 ---
  for (const [view, prefix] of [["clone", "c"], ["design", "d"], ["official", "o"], ["script", "s"]]) {
    await page.evaluate((v) => { location.hash = v; }, view);
    await page.waitForTimeout(150);
    const has = await page.evaluate((p) => {
      const box = document.querySelector(`#${p}-adv`);
      const det = box && [...box.querySelectorAll("details")]
        .find((d) => d.querySelector("summary")?.textContent.includes("输出后处理"));
      return !!det && det.querySelectorAll("[data-pp]").length === 4;
    }, prefix);
    check(`${view} 页有后处理折叠组（3 勾 + 1 数字框）`, has);
  }

  await page.evaluate(() => { location.hash = "clone"; });
  await page.waitForTimeout(150);

  // --- 2. 默认值来自服务端 ---
  const def = await page.evaluate(() => {
    const get = (k) => document.querySelector(`#c-adv [data-pp="${k}"]`);
    return {
      silence: get("silence").checked,
      normalize: get("normalize").checked,
      fade: get("fade").checked,
      mid: get("mid_ms").value,
    };
  });
  check("默认三项全不勾", !def.silence && !def.normalize && !def.fade, JSON.stringify(def));
  check("默认阈值 500", String(def.mid) === "500", String(def.mid));

  // --- 3. 改动会持久化 ---
  await page.evaluate(() => {
    const el = document.querySelector('#c-adv [data-pp="normalize"]');
    el.checked = true;
    el.dispatchEvent(new Event("change", { bubbles: true }));
  });
  const stored = await page.evaluate(() => JSON.parse(localStorage.getItem("qs.pp") || "{}"));
  check("开启归一化写进 localStorage", stored.normalize === true, JSON.stringify(stored));

  // --- 4. 越界手输被纠正 ---
  await page.evaluate(() => {
    const el = document.querySelector('#c-adv [data-pp="mid_ms"]');
    el.value = "99999";
    el.dispatchEvent(new Event("change", { bubbles: true }));
  });
  const mid = await page.evaluate(() =>
    document.querySelector('#c-adv [data-pp="mid_ms"]').value);
  check("阈值上限夹到 3000", String(mid) === "3000", String(mid));

  // --- 5. 刷新后保留（没被服务端默认值重置） ---
  await page.reload({ waitUntil: "load" });
  // 折叠组默认是收起的，元素 attached 就够了 —— 不必为了读值把它展开
  await page.waitForSelector("#c-adv [data-pp]", { state: "attached", timeout: 20000 });
  const after = await page.evaluate(() => ({
    normalize: document.querySelector('#c-adv [data-pp="normalize"]').checked,
    mid: document.querySelector('#c-adv [data-pp="mid_ms"]').value,
  }));
  check("刷新后仍是开", after.normalize === true);
  check("刷新后阈值仍是 3000", String(after.mid) === "3000", String(after.mid));

  // --- 6. 提交的 payload 里带上了这四个字段 ---
  // 用「声音设计」页验：它只要文本 + 描述就能提交，不必先挑音色。
  // 请求在浏览器层截住（整页 JS 跑在 IIFE 里，从外面改不到 post），
  // 顺便把 /api/jobs/{id} 伪造成一条带后处理统计的完成态 —— 这样不碰 GPU
  // 也能验到结果卡那一列到底长什么样。
  const fs = require("fs");
  const path = require("path");
  const outDir = path.join(__dirname, "..", "outputs");
  const sample = fs.readdirSync(outDir).find((f) => f.endsWith(".wav")) || "none.wav";

  let sent = null;
  await page.route("**/api/jobs", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    try { sent = JSON.parse(route.request().postData() || "{}"); } catch (_) { sent = null; }
    await route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ ok: true, job: "fakejob" }),
    });
  });
  await page.route("**/api/jobs/fakejob", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({
      ok: true, state: "done", progress: 1,
      message: "音频 3.0s，耗时 1.0s，RTF 0.33\n后处理：剪掉 1.2s 静音，响度 +3.2 dB，首尾渐变",
      result: {
        file: sample, url: "/media/out/" + encodeURIComponent(sample),
        duration: 3.0, level: "ok", took: 1.0, headline: "音频 3.0s，耗时 1.0s，RTF 0.33",
        postprocess: { applied: true, silence: true, normalize: true, fade: true,
          cut_secs: 1.2, gain_db: 3.2, mid_ms: 3000, target_db: -20 },
      },
    }),
  }));

  await page.evaluate(() => { location.hash = "design"; });
  await page.waitForTimeout(200);
  await page.fill("#d-text", "界面验证用的一句话。");
  await page.fill("#d-instruct", "三十岁上下的女声，偏低，语速稳。");
  await page.evaluate(() => document.querySelector("#d-go").click());
  await page.waitForTimeout(1500);

  if (sent) {
    check("payload 带 pp_silence=false（默认关）", sent.pp_silence === false, String(sent.pp_silence));
    check("payload 带 pp_normalize=true（刚开启的）", sent.pp_normalize === true, String(sent.pp_normalize));
    check("payload 带 pp_fade=false（默认关）", sent.pp_fade === false, String(sent.pp_fade));
    check("payload 带 pp_mid_ms=3000", String(sent.pp_mid_ms) === "3000", String(sent.pp_mid_ms));
  } else {
    check("payload 抓到了", false, "提交没触发（按钮被禁用或表单校验没过）");
  }

  // --- 7. 结果卡参数表把「设置」和「实际效果」都画出来 ---
  const card = await page.evaluate(() => {
    const el = document.querySelector("#d-result [data-role=params]");
    return el ? el.innerText.replace(/\s+/g, " ") : "";
  });
  check("参数表有「后处理 归一化」", /后处理 归一化/.test(card), card.slice(0, 200));
  check("参数表有「剪掉 1.2 秒静音」", /剪掉 1\.2 秒静音/.test(card));
  check("参数表有「响度 +3.2 dB」", /响度 \+3\.2 dB/.test(card));

  const alerts = await page.evaluate(() => {
    const el = document.querySelector("#d-result [data-role=alerts]");
    return el ? el.innerText.replace(/\s+/g, " ") : "";
  });
  check("状态区保留了「后处理：…」那一行", /后处理：剪掉 1\.2s 静音/.test(alerts), alerts.slice(0, 180));

  check("没有控制台报错", errs.length === 0, errs.slice(0, 2).join(" | "));

  await browser.close();
  const bad = results.filter((r) => !r.ok);
  console.log(`\n${bad.length ? "❌" : "✅"} ${results.length - bad.length}/${results.length} 通过`);
  process.exit(bad.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
