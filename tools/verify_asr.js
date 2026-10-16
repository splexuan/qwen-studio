/* 一次性验证脚本：确认「参考文本」弹窗里的「自动转写」按钮真的能用。
   用 playwright-core 驱动本机 Chrome，进音色库 → 点某音色的「参考文本」→
   断言弹窗里出现了可点的「自动转写」按钮 → 点它 → 等文本填进来。

   **只读**：收尾点「取消」关闭弹窗，不会写回音色库。

   跑法（在项目根目录）：
     NODE_PATH=<...>/node_modules node tools/verify_asr.js
   端口默认 7867，可用 QWENSTUDIO_PORT 覆盖。
*/
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:" + (process.env.QWENSTUDIO_PORT || "7867");
const OUT = "tools/_asr_reftext.png";
const WHO = "御姐";   // 这个音色的参考文本是真值：「小弟弟，有兴趣陪姐姐喝一杯吗？」

(async () => {
  const browser = await chromium.launch({
    channel: "chrome",
    headless: true,
    args: ["--mute-audio"],
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });

  const fails = [];
  const ok = (label, cond, detail = "") => {
    console.log(`  [${cond ? "OK  " : "FAIL"}] ${label}${detail ? "  → " + detail : ""}`);
    if (!cond) fails.push(label);
  };

  await page.goto(BASE + "/#library", { waitUntil: "networkidle" });
  await page.waitForSelector(".vitem", { timeout: 15000 });
  console.log("[1] 音色库已加载");

  const card = `.vitem[data-name="${WHO}"]`;
  await page.waitForSelector(card, { timeout: 8000 });
  const refShown = await page.textContent(`${card} .vitem__ref`);
  ok("卡片展示了真实参考文本（不是字数）", /陪姐姐喝一杯/.test(refShown || ""),
     (refShown || "").slice(0, 30));

  // 打开「参考文本」弹窗
  await page.click(`${card} [data-role=text]`);
  await page.waitForSelector("dialog.modal", { timeout: 8000 });

  // 弹窗里的槽位 + 按钮
  const btn = await page.$("dialog.modal #rt-asr-slot button");
  ok("弹窗里挂出了转写按钮（#rt-asr-slot button）", !!btn);
  if (!btn) { console.log("\n无法继续：按钮不存在"); await browser.close(); process.exit(1); }

  const label = (await btn.textContent()) || "";
  const disabled = await btn.isDisabled();
  const title = (await btn.getAttribute("title")) || "";
  console.log(`[2] 按钮文案「${label}」 disabled=${disabled}`);
  if (title) console.log(`    title: ${title}`);
  ok("按钮可用（未禁用）", !disabled, label);
  ok("文案是「自动转写」而非不可用提示", label.includes("自动转写"));

  // 预填充：textarea 应带出已有参考文本
  const prefilled = await page.inputValue("dialog.modal #__rt");
  ok("弹窗预填了现有参考文本", /陪姐姐喝一杯/.test(prefilled), prefilled.slice(0, 30));

  // 清空后点转写，看它能不能把文本填回来
  await page.click("dialog.modal [data-role=clear]");
  const cleared = await page.inputValue("dialog.modal #__rt");
  ok("「清空」按钮生效", cleared.trim() === "", JSON.stringify(cleared));

  await btn.click();
  console.log("[3] 已点击「自动转写」，等待识别…");
  // 等文本被填回来（最多 60s，首次要加载识别模型）
  await page.waitForFunction(
    () => {
      const ta = document.querySelector("dialog.modal #__rt");
      return ta && ta.value.trim().length > 0;
    },
    { timeout: 60000 }
  );

  const got = (await page.inputValue("dialog.modal #__rt")).trim();
  const note = (await page.textContent("dialog.modal #__rt-note")) || "";
  console.log(`[4] 识别结果：${got}`);
  console.log(`    提示：${note.trim().slice(0, 80)}`);

  ok("转写把文本填了进来", got.length > 0, `${got.length} 字`);
  ok("结果大体正确（含「陪姐姐喝一杯」）", /陪姐姐喝一杯/.test(got), got);
  ok("给出了「请核对」类提示", /核对|同音字|标点/.test(note), note.trim().slice(0, 40));

  await page.screenshot({ path: OUT });

  // 只读收尾：取消，不保存
  await page.click("dialog.modal [data-role=no]");
  await page.waitForTimeout(300);
  const closed = await page.$("dialog.modal");
  ok("「取消」已关闭弹窗（未写库）", !closed);

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
