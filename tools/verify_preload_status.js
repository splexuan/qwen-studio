/* 验证：预加载进行中打开页面，顶栏会不会自己从「加载中」变成「已就绪」。
   以前不会 —— 状态只在「点生成」或「进系统设置」时才刷新，用户就看着一个过期的
   「加载中」不动。这里全程不点任何按钮，只等。

   跑法（必须先起服务，脚本要赶上预加载那 30 秒）：
     QWENSTUDIO_PORT=7877 runtime\python\python.exe web/server.py
     NODE_PATH=<...>/node_modules QWENSTUDIO_PORT=7877 node tools/verify_preload_status.js
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
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await ctx.newPage();
  const errs = [];
  page.on("console", (m) => { if (m.type() === "error") errs.push(m.text()); });
  page.on("pageerror", (e) => errs.push(String(e)));

  // 数一下 /api/model 的请求次数：轮询该在加载结束后停掉，不该一直打
  let modelHits = 0;
  page.on("request", (r) => {
    if (r.url().includes("/api/model")) modelHits += 1;
  });

  await page.goto(BASE, { waitUntil: "load" });
  await page.waitForSelector("#tb-model", { timeout: 20000 });

  const read = () => page.evaluate(() => ({
    text: (document.querySelector("#tb-model")?.textContent || "").trim(),
    dot: document.querySelector("#tb-dot")?.className || "",
  }));

  const first = await read();
  console.log(`[0] 打开时顶栏：${JSON.stringify(first.text)}  dot=${first.dot}`);

  // 没赶上预加载（服务起太久了）也能跑，只是证明不了「自动变化」这一段
  const caughtLoading = first.text.includes("加载中");
  check("打开页面时正处在预加载中（否则测不到自动刷新）", caughtLoading,
    caughtLoading ? first.text : "预加载已结束，本次跳过自动刷新断言");

  // 全程不点任何按钮，只等它自己变
  let final = first;
  const t0 = Date.now();
  for (let i = 0; i < 120; i++) {
    final = await read();
    if (!final.text.includes("加载中")) break;
    await page.waitForTimeout(500);
  }
  const waited = ((Date.now() - t0) / 1000).toFixed(1);
  console.log(`[1] ${waited}s 后顶栏：${JSON.stringify(final.text)}  dot=${final.dot}`);

  if (caughtLoading) {
    check("顶栏自己从「加载中」变成了确定状态（全程没点任何按钮）",
      !final.text.includes("加载中") && final.text.length > 0, final.text);
    check("状态点不再是 busy", !final.dot.includes("busy"), final.dot);
    // 两种情况都算通过，但必须说清是哪种：
    //   显存够 →「克隆 · 1.7B」；显存被别的程序占着 → 加载失败 →「未加载」。
    // 真正要防的是「卡在加载中不放」，那两种都不该出现。
    const settled = /1\.7B/.test(final.text) || final.text.includes("未加载");
    check("落地状态是明确的（已就绪或明确未加载，不是卡住）", settled, final.text);
    if (!/1\.7B/.test(final.text)) {
      console.log("      ↑ 本次是「加载失败」路径（显存被占），不是成功路径 —— " +
                  "但同样证明了失败时状态会清掉、不会永远显示加载中");
    }
  }

  // 结束之后不该再打 /api/model（除非用户自己触发）
  const hitsAtDone = modelHits;
  await page.waitForTimeout(4000);
  check("加载结束后轮询已停（4 秒内没有多余请求）", modelHits === hitsAtDone,
    `${hitsAtDone} → ${modelHits} 次`);

  check("控制台没有报错", errs.length === 0, errs.slice(0, 2).join(" | "));

  await page.screenshot({ path: "tools/_preload_status.png" });

  const bad = results.filter((r) => !r.ok);
  console.log(`\n${results.length - bad.length}/${results.length} 通过`);
  await browser.close();
  process.exit(bad.length ? 1 : 0);
})().catch((e) => { console.error("脚本异常：", e); process.exit(2); });
