/* 验证指令框下方的情绪提示（#c-instr-tip）。
   行为：填了指令就出现；如果填的是「情绪名」则用警示色并给出改写建议。
   顺带钉住「指令预设候选项已被删掉」这件事（用户明确要求删，别再被加回来）。
   只读：只在文本框里打字，不点生成、不写任何文件。 */
const { chromium } = require("playwright-core");

const PORT = process.env.QWENSTUDIO_PORT || "7867";
const BASE = `http://127.0.0.1:${PORT}`;

const results = [];
function check(name, ok, detail) {
  results.push(ok);
  console.log(`  [${ok ? "OK  " : "FAIL"}] ${name}${detail ? "  → " + detail : ""}`);
}

(async () => {
  const browser = await chromium.launch({ channel: "chrome" });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e.message)));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Network.setCacheDisabled", { cacheDisabled: true });
  await page.goto("about:blank");
  await page.goto(BASE + "/#clone", { waitUntil: "networkidle" });
  // 等首屏把音色拉回来（选音色现在是「按钮 → 弹窗」，页面上不再铺芯片）
  await page.waitForSelector("#c-pick", { timeout: 20000 });

  const tip = page.locator("#c-instr-tip");
  const visible = () => tip.isVisible();
  const text = () => tip.textContent();
  const isWarn = () => tip.evaluate((el) => el.classList.contains("hint--warn"));
  const type = async (v) => { await page.fill("#c-instruct", v); await page.waitForTimeout(200); };

  console.log("[1] 指令为空：不该有提示");
  await type("");
  check("空指令时隐藏", (await visible()) === false);

  console.log("[2] 填「情绪名」：提示 + 警示色 + 改写建议");
  await type("悲痛绝望的大喊");
  check("提示出现", (await visible()) === true);
  check("用了警示色", (await isWarn()) === true);
  const t2 = await text();
  check("点明这是情绪名", t2.includes("情绪名"), t2.slice(0, 30) + "…");
  check("给出标点建议", t2.includes("——！") || t2.includes("标点"));

  console.log("[3] 换成描述句：提示仍在，但不再报警");
  await type("绝望的嘶喊：音量拉到最大接近破音，声音发抖带着哭腔，语速时快时慢");
  check("提示仍在", (await visible()) === true);
  check("换回普通提示色", (await isWarn()) === false);
  const t3 = await text();
  check("给出加强建议", t3.includes("标点"), t3.slice(0, 34) + "…");

  console.log("[4] 全程与「克隆方式」无关（换了模式也照样提示）");
  await page.locator('label[for="c-xvec"]').click();  // 切到完整 ICL
  await page.waitForTimeout(250);
  check("切到完整 ICL 后提示不变", (await visible()) === true);
  await page.locator('label[for="c-xvec"]').click();
  await page.waitForTimeout(250);

  console.log("[5] 清空 + 预设候选项确实已撤掉");
  await type("");
  check("清空后隐藏", (await visible()) === false);
  // 指令预设整块（PRESETS_* 常量 / .presets / .pchip / #c|d|o-presets）是用户明确要求
  // 删掉的 —— 情绪怎么写因人因戏而异，摆一排候选项只会把人往模板上带。
  // 留这几条断言，免得将来收拾代码时又被顺手加回来。
  check("克隆页没有 #c-presets", (await page.locator("#c-presets").count()) === 0);
  check("整个页面没有 .presets 容器", (await page.locator(".presets").count()) === 0);
  check("没有 .pchip 候选项", (await page.locator(".pchip").count()) === 0);
  // 查源码而不是 window：那几个符号本来就包在 IIFE 里，window 上有没有都查不出东西，
  // 那样的断言写出来只会「通过」，等于没测。
  const src = await (await fetch(BASE + "/")).text();
  check("源码里没有预设的残留（PRESETS_ / renderPresets / .presets / .pchip）",
        !/PRESETS_|renderPresets|class="presets"|pchip/.test(src));
  await type("语速放慢，句尾往下压，气声多一点。");
  check("手写指令照样带出提示", (await visible()) === true);
  check("普通描述句不报警", (await isWarn()) === false);

  console.log("[6] 全程无 JS 报错");
  check("无 JS 报错", errors.length === 0, errors.slice(0, 2).join(" | "));

  await page.locator("#c-instruct").scrollIntoViewIfNeeded();
  await page.locator(".card").nth(2).screenshot({ path: "tools/_instr_tip.png" });
  await browser.close();

  const bad = results.filter((x) => !x).length;
  console.log("=".repeat(64));
  console.log(bad === 0 ? `结果：全部通过（${results.length} 项）` : `结果：${bad} 项未通过`);
  process.exit(bad === 0 ? 0 : 1);
})().catch((e) => { console.error("脚本异常：", e.message); process.exit(2); });
