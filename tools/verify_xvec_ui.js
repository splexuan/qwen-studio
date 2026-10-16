/* 一次性验证脚本：确认「音色的克隆方式」在界面上看得见、而且真的生效。

   要验三件事：
   1. 每张卡片都标出「仅向量」还是「完整 ICL」
   2. 列表能按克隆方式筛
   3. 克隆页选中音色时，「只用说话人向量」复选框跟着音色走
      （早先它不跟着走，会把音色存的设置悄悄覆盖掉）

   自给自足：自己造两个临时音色（一个仅向量、一个完整 ICL），
   音频借用库里现成的第一个音色，不依赖用户的音色是哪种设置。
   收尾两个都删掉。

   跑法（在项目根目录）：
     NODE_PATH=<...>/node_modules node tools/verify_xvec_ui.js
   端口默认 7867，可用 QWENSTUDIO_PORT 覆盖。

   注意：导航一律走「about:blank → 目标 URL」的完整加载。
   只改 hash 是同文档导航，page.goto 可能等不到 load 事件。
*/
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:" + (process.env.QWENSTUDIO_PORT || "7867");
const OUT = "tools/_xvec_ui.png";
const XV = "__验证_仅向量";
const IC = "__验证_完整ICL";

const jget = async (p) => (await fetch(BASE + p)).json();
const jpost = async (p, b) => (await fetch(BASE + p, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(b),
})).json();

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true, args: ["--mute-audio"] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });

  const fails = [];
  const ok = (label, cond, detail = "") => {
    console.log(`  [${cond ? "OK  " : "FAIL"}] ${label}${detail ? "  → " + detail : ""}`);
    if (!cond) fails.push(label);
  };
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => consoleErrors.push("pageerror: " + e.message));

  // index.html 走 FileResponse（带 Last-Modified/ETag），不关缓存的话
  // 反复加载同一个 URL 可能拿到上一份，测出来的就不是刚改过的代码。
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Network.setCacheDisabled", { cacheDisabled: true });

  // 完整加载目标页（先 about:blank，避免被当成同文档的 hash 跳转）
  const goto = async (hash) => {
    await page.goto("about:blank");
    await page.goto(BASE + "/" + hash, { waitUntil: "networkidle" });
  };

  // 准备：借库里第一个音色的音频，造两个只有克隆方式不同的临时音色。
  // prompt=false 跳过基因抽取，免得白等模型加载。
  const lib = await jget("/api/voices");
  if (!lib.voices?.length) { console.log("音色库是空的，无法验证"); process.exit(1); }
  const src = lib.voices[0].name;
  await jpost("/api/voices/delete", { names: [XV, IC] });
  const a = await jpost("/api/voices", { name: XV, voice: src, xvec_only: true, prompt: false });
  const b = await jpost("/api/voices", { name: IC, voice: src, xvec_only: false, prompt: false });
  console.log(`[0] 基于「${src}」造了两个临时音色：${a.name}(仅向量) / ${b.name}(完整 ICL)`);
  ok("两个临时音色都建好了且未排基因任务", a.name === XV && b.name === IC && !a.job && !b.job);

  await goto("#library");
  await page.waitForSelector(".vitem", { timeout: 15000 });
  console.log("[1] 音色库已加载");

  // 1) 卡片上看得见
  const pills = await page.evaluate(() =>
    [...document.querySelectorAll(".vitem")].map((el) => {
      const top = el.querySelector(".vitem__top")?.textContent || "";
      return { name: el.dataset.name, icl: top.includes("完整 ICL"), xvec: top.includes("仅向量") };
    }));
  console.log(`    共 ${pills.length} 张卡片`);
  ok("每张卡片都标了克隆方式", pills.every((p) => p.icl || p.xvec),
     pills.filter((p) => !p.icl && !p.xvec).map((p) => p.name).join("、"));
  ok(`「${XV}」标为仅向量`, pills.find((p) => p.name === XV)?.xvec === true,
     JSON.stringify(pills.find((p) => p.name === XV)));
  ok(`「${IC}」标为完整 ICL`, pills.find((p) => p.name === IC)?.icl === true,
     JSON.stringify(pills.find((p) => p.name === IC)));

  // 2) 筛选
  const filterText = (await page.textContent("#l-filter"))?.replace(/\s+/g, " ").trim();
  console.log(`[2] 筛选项：${filterText}`);
  const namesNow = () => page.evaluate(() =>
    [...document.querySelectorAll(".vitem")].map((e) => e.dataset.name));

  await page.click('#l-filter [data-mode="icl"]');
  await page.waitForTimeout(250);
  const iclOnly = await namesNow();
  ok("筛「完整 ICL」含完整 ICL、不含仅向量",
     iclOnly.includes(IC) && !iclOnly.includes(XV), iclOnly.join("、"));

  await page.click('#l-filter [data-mode="xvec"]');
  await page.waitForTimeout(250);
  const xvecOnly = await namesNow();
  ok("筛「仅向量」含仅向量、不含完整 ICL",
     xvecOnly.includes(XV) && !xvecOnly.includes(IC), `${xvecOnly.length} 个`);

  await page.click('#l-filter [data-mode="all"]');
  await page.waitForTimeout(250);
  const all = await namesNow();
  ok("筛「全部」恢复所有音色", all.includes(XV) && all.includes(IC), `${all.length} 个`);

  await page.screenshot({ path: OUT });

  // 3) 克隆页复选框跟着音色走
  // 选音色现在是「按钮 → 弹窗」（音色库会一直长，罗列撑不住），所以走弹窗挑。
  await goto("#clone");
  await page.waitForSelector("#c-pick", { timeout: 15000 });
  const pickVia = async (name) => {
    await page.click("#c-pick");
    await page.waitForSelector("dialog.modal .vrow", { timeout: 8000 });
    await page.fill("dialog.modal #__pq", name);
    await page.waitForTimeout(200);
    await page.click(`dialog.modal .vrow[data-name="${name}"]`);
    await page.waitForTimeout(300);
  };

  const readState = () => page.evaluate(() => ({
    checked: document.querySelector("#c-xvec").checked,
    hint: document.querySelector("#c-ref-hint").textContent,
    detail: document.querySelector("#c-lib-detail")?.textContent || "",
  }));

  await pickVia(IC);
  const iclState = await readState();
  console.log(`[3] 选中「${IC}」：复选框 checked=${iclState.checked}`);
  ok("选中完整 ICL 音色时复选框自动取消勾选", iclState.checked === false);
  ok("参考文本提示同步成完整 ICL 的说法", /完整 ICL/.test(iclState.hint), iclState.hint.slice(0, 24));
  ok("详情行标出了克隆方式", /克隆方式/.test(iclState.detail) && /完整 ICL/.test(iclState.detail));

  await pickVia(XV);
  const xvecState = await readState();
  console.log(`[4] 选中「${XV}」：复选框 checked=${xvecState.checked}`);
  ok("选中仅向量音色时复选框自动勾上", xvecState.checked === true);
  ok("提示同步回仅向量的说法", /只用说话人向量/.test(xvecState.hint), xvecState.hint.slice(0, 24));

  ok("全程无 JS 报错", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | "));

  await jpost("/api/voices/delete", { names: [XV, IC] });
  console.log("[5] 已清理两个临时音色");

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
