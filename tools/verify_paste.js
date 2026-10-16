/* 一次性验证脚本：**真·粘贴**（走剪贴板 + Ctrl+V，不是 `fill()`）之后，
   每个文本框里的换行符都还在。

   为什么要专门盯它：`fill()` 是直接赋 value，绕过了浏览器的粘贴路径，
   所以「单行 `<input>` 把换行压成空格」这种问题用 fill 测**根本测不出来**。
   曾有过两处参考文本用的是单行 input（克隆页的 / 新增音色弹窗的），
   用户粘一段带换行的参考文本进去，换行就没了 —— 而完整 ICL 模式要求逐字对齐，
   这等于把参考文本悄悄改错。

   后来用户又回来说「复制上变成一整段了」，而上面那两处早就改成 textarea 了。
   查到的机制在 [6]：**有些来源的换行不是 `\n`，而是 U+000B（Word 的软换行）/
   U+000C / U+0085 / U+2028 / U+2029**，浏览器不归一它们，而它们在 textarea 里
   **根本不渲染成换行**（固定宽下只占一行，肉眼看就是一整段）。
   （用户那次最后确认是他自己那份来源就一行，但这类字符确实存在、确实看不见，
   所以修复照留。）这里既钉「换行不能掉」，也钉「隐形分隔符必须还原成真换行」。

   **只读**：只在无头浏览器自己的页面里打字，弹窗一律点「取消」，不写音色库。

   跑法（在项目根目录）：
     NODE_PATH=<...>/node_modules node tools/verify_paste.js
   端口默认 7867，可用 QWENSTUDIO_PORT 覆盖。
*/
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:" + (process.env.QWENSTUDIO_PORT || "7867");
const TXT = "第一行\n第二行\n第三行";        // 两处换行，粘进去后必须还是两个
const SOFT = /[\u000b\u000c\u0085\u2028\u2029]/;

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true, args: ["--mute-audio"] });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 980 } });
  await ctx.grantPermissions(["clipboard-read", "clipboard-write"], { origin: BASE });
  const page = await ctx.newPage();

  const fails = [];
  const ok = (label, cond, detail = "") => {
    console.log(`  [${cond ? "OK  " : "FAIL"}] ${label}${detail ? "  → " + detail : ""}`);
    if (!cond) fails.push(label);
  };
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => consoleErrors.push("pageerror: " + e.message));

  const nav = async (v) => { await page.click(`#nav [data-view=${v}]`); await page.waitForTimeout(150); };
  const writeClip = (t) => page.evaluate(async (s) => {
    await navigator.clipboard.write([new ClipboardItem({ "text/plain": new Blob([s], { type: "text/plain" }) })]);
  }, t);
  const pasteNow = async (sel) => {
    await page.click(sel, { force: true });
    await page.keyboard.press("Control+A");
    await page.keyboard.press("Control+V");
    await page.waitForTimeout(90);
  };

  /* 真粘贴：写剪贴板 → 全选 → Ctrl+V → 读回值，数换行。返回标签名给后面断言用 */
  const pasteInto = async (sel, label) => {
    const el = await page.$(sel);
    if (!el) { ok(`${label} 存在`, false, sel); return null; }
    await page.evaluate((t) => navigator.clipboard.writeText(t), TXT);
    await pasteNow(sel);
    const v = await page.inputValue(sel);
    const tag = await page.evaluate((s) => document.querySelector(s).tagName, sel);
    const n = (v.match(/\n/g) || []).length;
    ok(`${label}（${tag}）粘贴后换行还在`, n === 2 && v === TXT,
       `${n}/2 个换行 · ${JSON.stringify(v)}`);
    return tag;
  };

  await page.goto(BASE + "/#clone", { waitUntil: "networkidle" });
  await page.waitForSelector("#c-text", { timeout: 15000 });
  console.log("[1] 克隆页");
  await pasteInto("#c-text", "正文");
  await pasteInto("#c-instruct", "情绪指令");
  await page.click("#c-tab-up");
  await page.waitForTimeout(150);
  const tagCloneRef = await pasteInto("#c-ref-text", "参考文本（临时上传）");
  await page.screenshot({ path: "tools/_paste_reftext.png" });   // 截图：粘进去的三行还分着行

  console.log("[2] 声音设计 / 官方音色 / 剧本工坊");
  await nav("design");
  await pasteInto("#d-text", "正文");
  await pasteInto("#d-instruct", "音色描述");
  await nav("official");
  await pasteInto("#o-text", "正文");
  await pasteInto("#o-instruct", "情感指令");
  await nav("script");
  await pasteInto("#s-text", "剧本全文");

  console.log("[3] 音色库的两个弹窗");
  await nav("library");
  await page.waitForSelector(".vitem");
  await page.click(".vitem [data-role=text]");
  await page.waitForSelector("dialog.modal #__rt");
  const tagModal = await pasteInto("dialog.modal #__rt", "「参考文本」弹窗");
  await page.click("dialog.modal [data-role=no]");       // 只读收尾
  await page.waitForTimeout(200);
  await page.click("#l-new");
  await page.waitForSelector("dialog.modal #nv-text");
  const tagNv = await pasteInto("dialog.modal #nv-text", "新增音色弹窗的参考文本");

  console.log("[4] 参考文本一律不能用单行 input");
  // 单行 <input> 是浏览器层面就把换行压掉，任何 JS 都补不回来 —— 直接钉住标签名
  ok("新增音色弹窗的参考文本是 TEXTAREA", tagNv === "TEXTAREA", String(tagNv));
  ok("克隆页的参考文本是 TEXTAREA", tagCloneRef === "TEXTAREA", String(tagCloneRef));
  ok("「参考文本」弹窗仍是 TEXTAREA", tagModal === "TEXTAREA", String(tagModal));

  console.log("[5] 音色卡片上的参考文本按真换行渲染");
  await page.click("dialog.modal [data-role=no]");        // 关掉新增音色弹窗
  await page.waitForTimeout(200);
  const ws = await page.evaluate(() => {
    const el = document.querySelector(".vitem__ref");
    return el ? getComputedStyle(el).whiteSpace : null;
  });
  ok("卡片参考文本是 pre-line（换行不再被折成空格）", ws === "pre-line", String(ws));

  console.log("[6] 隐形分隔符必须还原成真换行（U+000B 就是 Word 的软换行）");
  const LONG = "这是一段够长的文本，中间夹一个不渲染成换行的分隔符。".repeat(4);
  const withSep = (sep) => LONG.slice(0, 55) + sep + LONG.slice(55);

  await nav("clone");
  const pasteSep = async (label, sep) => {
    await writeClip(withSep(sep));
    await pasteNow("#c-text");
    const v = await page.inputValue("#c-text");
    const left = (v.match(new RegExp(SOFT.source, "g")) || []).length;
    const n = (v.match(/\n/g) || []).length;
    ok(`${label} 粘进来被还原成真换行`, left === 0 && n === 1, `残留隐形符=${left} · 换行=${n}`);
  };

  await pasteSep("U+000B（Word 软换行）", "\u000b");
  await page.screenshot({ path: "tools/_paste_softbreak.png" });   // 截图：隐形分隔符已还原成真换行
  await pasteSep("U+000C（分页符）", "\u000c");
  await pasteSep("U+0085（NEL）", "\u0085");
  await pasteSep("U+2028（行分隔符）", "\u2028");
  await pasteSep("U+2029（段分隔符）", "\u2029");

  console.log("[7] 干净的文本不能被搅动");
  await writeClip(TXT);
  await pasteNow("#c-text");
  const clean = await page.inputValue("#c-text");
  ok("干净的 \\n 原样进来", clean === TXT, JSON.stringify(clean));

  console.log("[8] 修复之前存下的旧草稿，回填时也要把隐形分隔符换掉");
  // 先落到一个「不绑 #c-text」的页面再改 localStorage：留在克隆页上写，
  // 跳走时的 beforeunload 会用空值把草稿冲掉。
  await page.goto("about:blank");
  await page.goto(BASE + "/#api", { waitUntil: "networkidle" });
  await page.evaluate((t) => localStorage.setItem("qs-draft", JSON.stringify({ "#c-text": t })),
                      "第一行\u000b第二行\u2028第三行");
  await page.goto("about:blank");
  await page.goto(BASE + "/#clone", { waitUntil: "networkidle" });
  await page.waitForSelector("#c-text", { timeout: 15000 });
  await page.waitForTimeout(300);
  const back = await page.inputValue("#c-text");
  ok("旧草稿里的隐形分隔符在回填时被还原", back === "第一行\n第二行\n第三行", JSON.stringify(back));

  ok("全程无 JS 报错", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | "));

  await browser.close();
  console.log("\n" + "=".repeat(60));
  if (fails.length) {
    console.log(`${fails.length} 项未通过：`);
    fails.forEach((f) => console.log("   - " + f));
    process.exit(1);
  }
  console.log("全部通过");
})();
