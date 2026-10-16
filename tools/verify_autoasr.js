/* 一次性验证脚本：**参考文本的自动转写**——新增音色、裁剪音色两处都不用手点。

   规矩（`autoTranscribe()` 里那两条）：
   ① 音频一就绪、选区一改就自己转，不用点「自动转写」；
   ② **只覆盖「上一次是我们填进去的」内容** —— 用户自己写/改过的，一个字都不动。

   **会写库，但只动自己造的临时音色**：中途建一个 `__验证_裁剪重转`（裁剪那条必须真裁，
   否则验不了），跑完在 `finally` 里删掉。既有的音色一个都不碰。

   跑法（在项目根目录）：
     NODE_PATH=<...>/node_modules node tools/verify_autoasr.js
   端口默认 7867，可用 QWENSTUDIO_PORT 覆盖。
*/
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:" + (process.env.QWENSTUDIO_PORT || "7867");
const OUT = "tools/_autoasr.png";
const WHO = "御姐";                       // 它的音频说的是「小弟弟，有兴趣陪姐姐喝一杯吗？」
const SRC = BASE + "/media/voice/" + encodeURIComponent(WHO);
const TEMP = "__验证_裁剪重转";
const HIT = /陪姐姐喝一杯/;

const jpost = async (p, body) => {
  const r = await fetch(BASE + p, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
};
const voices = async () => (await (await fetch(BASE + "/api/voices")).json()).voices || [];
const drop = async (names) => { try { await jpost("/api/voices/delete", { names }); } catch (_) {} };

(async () => {
  const fails = [];
  const ok = (label, cond, detail = "") => {
    console.log(`  [${cond ? "OK  " : "FAIL"}] ${label}${detail ? "  → " + detail : ""}`);
    if (!cond) fails.push(label);
  };

  const browser = await chromium.launch({ channel: "chrome", headless: true, args: ["--mute-audio"] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => consoleErrors.push("pageerror: " + e.message));

  await drop([TEMP]);                      // 上一轮万一没清干净
  let created = false;
  try {
    /* ---------------- ① 新增音色：音频一就绪就自己转 ---------------- */
    await page.goto(BASE + "/#library", { waitUntil: "networkidle" });
    await page.waitForSelector(".vitem", { timeout: 15000 });
    await page.click("#l-new");
    await page.waitForSelector("dialog.modal #nv-url", { timeout: 8000 });
    console.log("[1] 新增音色弹窗已打开");

    const nvBtn = await page.$("dialog.modal #nv-asr-slot button");
    ok("弹窗里仍有「自动转写」按钮（自动只是省一步，不是删掉入口）", !!nvBtn);
    if (nvBtn) ok("  · 按钮可用", !(await nvBtn.isDisabled()));

    await page.fill("dialog.modal #nv-url", SRC);
    await page.click("dialog.modal #nv-url-go");
    console.log("[2] 已从链接拉取参考音频，**一次都没点转写按钮**");

    // 关键断言：不点按钮，文本自己填进来
    await page.waitForFunction(
      () => {
        const ta = document.querySelector("dialog.modal #nv-text");
        return ta && ta.value.trim().length > 0;
      },
      { timeout: 90000 }
    );
    const auto1 = (await page.inputValue("dialog.modal #nv-text")).trim();
    ok("音频就绪后参考文本**自动**填好了", HIT.test(auto1), auto1);
    // 按钮应该在跑完后自己恢复，而不是卡在「转写中…」
    const labelBack = ((await nvBtn.textContent()) || "").trim();
    ok("按钮从「转写中…」恢复回原样", labelBack === "自动转写", labelBack);

    /* 手写的绝不能被自动流程覆盖 */
    await page.fill("dialog.modal #nv-text", "我自己写的，别动我");
    await page.click("dialog.modal #nv-editor [data-role=all]");   // 改选区 → 触发一次 poke
    await page.waitForTimeout(3000);                               // 等它跑完（含防抖 900ms）
    const kept = (await page.inputValue("dialog.modal #nv-text")).trim();
    ok("手写过的文本，改选区也不会被自动转写覆盖", kept === "我自己写的，别动我", kept);

    await page.screenshot({ path: OUT });
    await page.click("dialog.modal [data-role=no]");               // 只读收尾
    await page.waitForTimeout(300);
    ok("「取消」已关闭弹窗（没写出音色）", !(await page.$("dialog.modal")));
    ok("  · 音色库里没多出东西", !(await voices()).some((v) => v.name === TEMP));

    /* ---------------- ② 裁剪音色：裁完自动按新音频重转 ---------------- */
    console.log("[3] 造一个临时音色来裁（跑完删掉）");
    const up = await jpost("/api/upload/url", { url: SRC });
    await jpost("/api/voices", { name: TEMP, upload: up.name, ref_text: "", xvec_only: false, prompt: false });
    created = true;
    const before = (await voices()).find((v) => v.name === TEMP) || {};
    ok("临时音色建好了，参考文本一开始是空的", !before.chars, JSON.stringify(before).slice(0, 90));

    // 只改 hash 是同文档导航，列表不会重建 —— 先落 about:blank 再进来，强制重新拉音色
    await page.goto("about:blank");
    await page.goto(BASE + "/#library", { waitUntil: "networkidle" });
    await page.waitForSelector(`.vitem[data-name="${TEMP}"]`, { timeout: 8000 });
    await page.click(`.vitem[data-name="${TEMP}"] [data-role=trim]`);
    await page.waitForFunction(
      () => {
        const el = document.querySelector(".vitem .ed-len");
        return el && /^[\d.]+s$/.test(el.textContent.trim());
      },
      { timeout: 15000 }
    );
    console.log("[4] 裁剪编辑器已就绪，点「保存裁剪」（整段，只验证转写那一段）");
    await page.click(".vitem [data-role=save]");

    await page.waitForFunction(
      () => [...document.querySelectorAll("#toasts .toast")]
        .some((t) => /参考文本已自动重转/.test(t.textContent)),
      { timeout: 90000 }
    );
    const after = (await voices()).find((v) => v.name === TEMP) || {};
    ok("裁剪后参考文本被自动写回（没点任何转写按钮）", after.chars > 0, `chars=${after.chars}`);
    ok("  · 写回的内容与音频对得上", HIT.test(after.ref_text || ""), (after.ref_text || "").slice(0, 40));
    await page.screenshot({ path: OUT.replace(".png", "_trim.png") });
  } finally {
    await drop([TEMP]);
    created = false;
    await browser.close();
  }

  const left = (await voices()).some((v) => v.name === TEMP);
  console.log(`[5] 临时音色已清理：${left ? "否（残留了！）" : "是"}`);
  if (left) fails.push("临时音色残留");

  console.log("\n" + "=".repeat(60));
  if (fails.length) {
    console.log(`${fails.length} 项未通过：`);
    fails.forEach((f) => console.log("   - " + f));
    process.exit(1);
  }
  console.log("全部通过");
  console.log("截图：" + OUT);
})();
