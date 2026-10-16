/* 一次性验证脚本：确认「保存音色 → 后台自动生成基因 → 卡片状态跟着变」这条链路，
   外加「同名音色只能有一个」（同名保存必须 409 拒绝，不许悄悄改名成 xxx_2）。
   用 playwright-core 驱动本机 Chrome，走真实的「新增音色」弹窗（真的上传一个 wav）。

   **会真的建一个临时音色**（名字见下方 NAME），收尾时自动删掉。
   万一中途崩了没删干净，手动去音色库删掉同名项即可。

   跑法（在项目根目录）：
     NODE_PATH=<...>/node_modules node tools/verify_gene.js
   端口默认 7867，可用 QWENSTUDIO_PORT 覆盖。
*/
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:" + (process.env.QWENSTUDIO_PORT || "7867");
const OUT = "tools/_gene_autogen.png";
const NAME = "__浏览器验证_基因";
const WAV = process.env.GENE_TEST_WAV || "C:/Game/qwen-studio/voices/皇帝.wav";

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true, args: ["--mute-audio"] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });

  const fails = [];
  const ok = (label, cond, detail = "") => {
    console.log(`  [${cond ? "OK  " : "FAIL"}] ${label}${detail ? "  → " + detail : ""}`);
    if (!cond) fails.push(label);
  };

  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => consoleErrors.push("pageerror: " + e.message));

  const del = (n) => page.evaluate(async (name) => {
    await fetch("/api/voices/delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names: [name] }),
    });
  }, n).catch(() => {});

  await page.goto(BASE + "/#library", { waitUntil: "networkidle" });
  await page.waitForSelector(".vitem", { timeout: 15000 });
  console.log("[1] 音色库已加载");

  await del(NAME);                      // 先清掉上一轮可能的残留
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForSelector(".vitem", { timeout: 15000 });

  ok("页面加载无 JS 报错", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | "));

  // 打开「新增音色」并上传一个真实 wav
  await page.click("#l-new");
  await page.waitForSelector("dialog.modal #nv-file", { timeout: 8000, state: "attached" });
  await page.setInputFiles("dialog.modal #nv-file", WAV);
  console.log("[2] 已选择参考音频，等待上传完成…");
  await page.waitForSelector("dialog.modal [data-role=yes]:not([disabled])", { timeout: 30000 });
  ok("上传完成、保存按钮可用", true);

  await page.fill("dialog.modal #nv-name", NAME);
  await page.click("dialog.modal [data-role=yes]");
  console.log("[3] 已点保存，等待音色落库…");

  const card = `.vitem[data-name="${NAME}"]`;
  await page.waitForSelector(card, { timeout: 30000 });
  ok("音色已保存并出现在列表里", true);

  // 保存那一刻应当还没有基因（后端是异步排任务）
  const pillAtFirst = (await page.textContent(`${card} .vitem__top`)) || "";
  console.log(`[4] 刚保存时卡片标签：${pillAtFirst.replace(/\s+/g, " ").trim()}`);

  // 等后台任务把基因做完 —— 卡片上的「无基因」应当变成「基因」
  await page.waitForFunction(
    (sel) => {
      const el = document.querySelector(sel + " .vitem__top");
      return el && el.textContent.includes("基因") && !el.textContent.includes("无基因");
    },
    card,
    { timeout: 120000 }
  );
  const pillAfter = (await page.textContent(`${card} .vitem__top`)) || "";
  console.log(`[5] 后台任务完成后：${pillAfter.replace(/\s+/g, " ").trim()}`);
  ok("卡片状态自动从「无基因」变为「基因」", !pillAfter.includes("无基因"));

  // 用接口确认 .pt 真的落盘了
  const hasPrompt = await page.evaluate(async (n) => {
    const r = await fetch("/api/voices");
    const d = await r.json();
    return (d.voices || []).find((v) => v.name === n)?.has_prompt;
  }, NAME);
  ok("接口确认 .pt 已生成（has_prompt）", hasPrompt === true, String(hasPrompt));

  // 同名音色只能有一个：不传 overwrite 时必须被明确拒绝，
  // 而不是像以前那样悄悄存成「xxx_2」——用户会以为那是另一个音色。
  const dup = await page.evaluate(async (n) => {
    const r = await fetch("/api/voices", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: n, voice: "御姐", prompt: false }),
    });
    return { status: r.status, body: await r.json() };
  }, NAME);
  ok("同名音色被拒（409 · code=name_exists）",
     dup.status === 409 && dup.body.code === "name_exists",
     `${dup.status} · ${dup.body.error || ""}`);

  const ghost = await page.evaluate(async (n) => {
    const d = await (await fetch("/api/voices")).json();
    return (d.voices || []).map((v) => v.name).filter((x) => x.startsWith(n + "_"));
  }, NAME);
  ok("没有偷偷生成「…_2」", ghost.length === 0, ghost.join("、"));

  await page.screenshot({ path: OUT });

  ok("全程无 JS 报错", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | "));

  await del(NAME);
  console.log("[6] 已清理临时音色");

  await browser.close();

  console.log("\n" + "=".repeat(60));
  if (fails.length) {
    console.log(`${fails.length} 项未通过：`);
    fails.forEach((f) => console.log("   - " + f));
    process.exit(1);
  }
  console.log("全部通过");
  console.log("截图：" + OUT);
})();
