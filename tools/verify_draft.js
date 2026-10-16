/* 一次性验证脚本：输入框草稿 —— 切视图、刷新之后，写进去的文本（连换行）原样还在。

   **只读**：不写音色库、不生成、不碰服务端数据；折腾的是无头浏览器**自己的**
   localStorage（每跑一次都是全新上下文），所以不会影响你浏览器里的草稿。

   跑法（在项目根目录）：
     NODE_PATH=<...>/node_modules node tools/verify_draft.js
   端口默认 7867，可用 QWENSTUDIO_PORT 覆盖。
*/
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:" + (process.env.QWENSTUDIO_PORT || "7867");

/* 故意挑一串「容易被顺手归一化掉」的东西：空行、行首缩进、行尾空格、全角空格。
   草稿要求的是**一字不动**，所以拿它当基准。 */
const CLONE_TEXT = "第一行\n\n第三行有点长，后面还跟着标点——！\n  缩进行（行首两个空格）\n末行带全角空格：　 ";
const SCRIPT_TEXT = "[韩立][压低了声音] 此丹的药力，恐怕不止于此。\n[南宫婉] 你是说……服下之后会有异动？\n\n[韩立] 三日内必有反应。";
const INSTRUCT = "压低声音，语速偏慢。\n喘气声别太重；句尾别往上挑。";

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true, args: ["--mute-audio"] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
  // 单文件前端带 ETag/Last-Modified，刷新会走缓存 —— 测「刷新后还在」必须关掉
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Network.setCacheDisabled", { cacheDisabled: true });

  const fails = [];
  const ok = (label, cond, detail = "") => {
    console.log(`  [${cond ? "OK  " : "FAIL"}] ${label}${detail ? "  → " + detail : ""}`);
    if (!cond) fails.push(label);
  };
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => consoleErrors.push("pageerror: " + e.message));

  const val = (sel) => page.inputValue(sel);
  const nav = async (v) => { await page.click(`#nav [data-view=${v}]`); await page.waitForTimeout(150); };
  const load = async () => {
    await page.goto("about:blank");
    await page.goto(BASE + "/#clone", { waitUntil: "networkidle" });
    await page.waitForSelector("#c-text", { timeout: 15000 });
  };
  const show = (s) => JSON.stringify(s).slice(0, 46);

  await load();
  console.log("[1] 页面已加载");

  // ---- 写进去 ----
  await page.fill("#c-text", CLONE_TEXT);
  await page.fill("#c-instruct", INSTRUCT);
  await nav("script");
  await page.fill("#s-text", SCRIPT_TEXT);
  await page.waitForTimeout(400);            // 预览是 240ms 防抖
  console.log(`[2] 三个框都写好了（${CLONE_TEXT.length} / ${INSTRUCT.length} / ${SCRIPT_TEXT.length} 字）`);

  // ---- 切视图来回：当前会话内本来就不该丢 ----
  await nav("library");
  await nav("clone");
  ok("切到别的视图再回来，文本还在（连换行）",
     (await val("#c-text")) === CLONE_TEXT, show(await val("#c-text")));

  // ---- 刷新：这一条才是草稿的意义 ----
  await load();
  const t = await val("#c-text"), i = await val("#c-instruct"), s = await val("#s-text");
  ok("刷新后 #c-text 一字不差（含空行 / 行首缩进 / 行尾空格）", t === CLONE_TEXT, show(t));
  ok("刷新后换行确实还在", (t.match(/\n/g) || []).length === 4, `${(t.match(/\n/g) || []).length} 个换行`);
  ok("刷新后 #c-instruct 一字不差", i === INSTRUCT, show(i));
  ok("刷新后 #s-text 一字不差", s === SCRIPT_TEXT, show(s));

  const count = ((await page.textContent("#c-count")) || "").trim();
  ok("字数按草稿重新算过（不是 0 字）", /^\d+ 字$/.test(count) && !/^0 字/.test(count), count);

  await nav("script");
  await page.waitForTimeout(500);
  const lines = ((await page.textContent("#s-count")) || "").trim();
  ok("剧本行数按草稿重新解析过", lines === "3 行", lines);

  // ---- 清空 = 清草稿 ----
  await nav("clone");
  await page.fill("#c-text", "");
  await load();
  ok("手动清空后刷新，不会又把旧文本拽回来", (await val("#c-text")) === "", show(await val("#c-text")));
  ok("  · 同一份草稿里别的字段没被连带清掉", (await val("#c-instruct")) === INSTRUCT);

  // ---- 程序赋值（示例按钮）也留得住 ----
  // 「示例」是直接 `el.value = ...`，不会派发 input 事件 —— 靠卸载前统一冲刷兜住
  await page.click("#c-sample");
  const sample = await val("#c-text");
  await load();
  ok("点「示例」后刷新，示例文本还在（程序赋值也存了）",
     (await val("#c-text")) === sample && sample.length > 10, show(await val("#c-text")));

  ok("全程无 JS 报错", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | "));
  await page.screenshot({ path: "tools/_draft_restore.png" });

  await browser.close();
  console.log("\n" + "=".repeat(60));
  if (fails.length) {
    console.log(`${fails.length} 项未通过：`);
    fails.forEach((f) => console.log("   - " + f));
    process.exit(1);
  }
  console.log("全部通过");
})();
