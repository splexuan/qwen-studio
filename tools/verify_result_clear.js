/* 验证：开始新任务时，结果区必须被清空。
   不清的话，上一轮的播放器 / 参数表 / 下载按钮会和这一轮的进度条同屏 ——
   分不清哪个属于这次，点了下载拿到的还是上一轮的文件。

   跑法（项目根目录，先起一个验证实例）：
     QWENSTUDIO_PORT=7877 runtime\python\python.exe web/server.py
     NODE_PATH=<...>/node_modules QWENSTUDIO_PORT=7877 node tools/verify_result_clear.js
*/
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:" + (process.env.QWENSTUDIO_PORT || "7877");
const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`${ok ? "  OK  " : " FAIL "} ${name}${detail ? "  — " + detail : ""}`);
}

const STATE = () => {
  const r = document.querySelector("#o-result");
  const vis = (sel) => {
    const el = r?.querySelector(sel);
    return !!el && !el.classList.contains("hidden");
  };
  return {
    badge: r?.querySelector("[data-role=badge]")?.textContent || "",
    player: vis("[data-role=playerbox]"),
    metrics: vis("[data-role=metrics]"),
    params: vis("[data-role=params]"),
    acts: vis("[data-role=acts]"),
    run: vis("[data-role=run]"),
    idle: vis("[data-role=idle]"),
    href: r?.querySelector("[data-role=dl]")?.getAttribute("href") || "",
    audioPaused: r?.querySelector("audio")?.paused ?? null,
  };
};

const waitDone = async (page, timeout = 240000) => {
  // 注意 waitForFunction 的签名是 (fn, arg, options) —— 超时必须放第三个参数，
  // 放第二个会被当成 arg，实际仍用默认的 30 秒，长任务就会「等了个寂寞」。
  await page.waitForFunction(() => {
    const b = document.querySelector("#o-result [data-role=badge]")?.textContent || "";
    return b.includes("已完成") || b.includes("失败") || b.includes("已取消");
  }, null, { timeout }).catch(() => {});
};

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  // 独立上下文：共用 localStorage 的话，测试往输入框里打的字会被存成用户草稿
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await ctx.newPage();
  const errs = [];
  page.on("console", (m) => { if (m.type() === "error") errs.push(m.text()); });
  page.on("pageerror", (e) => errs.push(String(e)));

  await page.goto(BASE, { waitUntil: "load" });
  await page.waitForSelector("#c-result [data-role=badge]", { timeout: 30000 });
  await page.evaluate(() => { location.hash = "official"; });
  await page.waitForTimeout(500);

  // ---- 第一单：跑完，结果区应当有东西 ----
  await page.fill("#o-text", "第一次生成，用于检查结果区。");
  await page.click("#o-go");
  await waitDone(page);
  const s1 = await page.evaluate(STATE);
  check("第一单完成后有播放器", s1.player, s1.badge);
  check("第一单完成后有参数表", s1.params);
  check("第一单完成后有下载按钮（且指向真实文件）", !!s1.href, s1.href.split("/").pop());
  check("第一单完成后进度区收起", !s1.run);
  const href1 = s1.href;

  // ---- 第二单：点下去的一瞬间，上一轮的东西必须没了 ----
  await page.fill("#o-text", "第二次生成，开始时结果区应该已经清空。");
  await page.click("#o-go");
  // 只等一小会儿：要抓的是「进行中」那个状态，不是完成态
  await page.waitForTimeout(600);
  const s2 = await page.evaluate(STATE);

  check("开始新任务后播放器立刻消失", !s2.player);
  check("开始新任务后参数表立刻消失", !s2.params);
  check("开始新任务后下载按钮立刻消失", !s2.acts);
  check("开始新任务后进度区出现", s2.run);
  check("开始新任务后空态不露出来", !s2.idle);
  check("上一轮的音频已暂停", s2.audioPaused === true || s2.audioPaused === null,
    `paused=${s2.audioPaused}`);
  check("上一轮的下载链接已划掉", s2.href === "", s2.href || "（空）");

  // ---- 第二单跑完，结果要换成新的 ----
  await waitDone(page);
  const s3 = await page.evaluate(STATE);
  check("第二单完成后播放器回来", s3.player);
  check("第二单完成后是新的文件", s3.href !== href1 && !!s3.href,
    `${href1.split("/").pop()} → ${s3.href.split("/").pop()}`);
  check("控制台没有报错", errs.length === 0, errs.slice(0, 2).join(" | "));

  await page.screenshot({ path: "tools/_result_clear.png" });

  const bad = results.filter((r) => !r.ok);
  console.log(`\n${results.length - bad.length}/${results.length} 通过`);
  await browser.close();
  process.exit(bad.length ? 1 : 0);
})().catch((e) => { console.error("脚本异常：", e); process.exit(2); });
