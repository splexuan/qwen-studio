/* 验证音色库的紧凑视图 + 收藏置顶。
   量的是「省了多少屏」和「收藏有没有真的置顶并持久化」；
   收藏会写进音色 json，所以最后一个音色测完必须还原。 */
const { chromium } = require("playwright-core");
const BASE = "http://127.0.0.1:" + (process.env.QWENSTUDIO_PORT || "7877");

const results = [];
function check(name, ok, detail = "") {
  results.push({ ok, name, detail });
  console.log(`${ok ? "  OK  " : " FAIL "} ${name}${detail ? "  — " + detail : ""}`);
}

async function openLib(browser, vp) {
  const ctx = await browser.newContext({ viewport: vp || { width: 1440, height: 1000 } });
  const page = await ctx.newPage();
  await page.goto(BASE, { waitUntil: "load" });
  await page.waitForSelector("#l-summary", { state: "attached", timeout: 20000 });
  await page.evaluate(() => { location.hash = "library"; });
  await page.waitForTimeout(800);
  await page.waitForSelector(".vitem", { state: "visible", timeout: 20000 });
  return { ctx, page };
}

const measure = (page) => page.evaluate(() => {
  const items = [...document.querySelectorAll(".vitem")];
  const first = items[0];
  const ref = first?.querySelector(".vitem__ref");
  return {
    n: items.length,
    cardH: first ? Math.round(first.getBoundingClientRect().height) : 0,
    refShown: ref ? getComputedStyle(ref).display !== "none" : null,
    docH: Math.round(document.documentElement.scrollHeight),
    vh: window.innerHeight,
    listH: Math.round(document.querySelector(".vgrid")?.scrollHeight || 0),
    mq: window.matchMedia("(max-width:640px)").matches,
    editDisp: (() => {
      const b = document.querySelector(".vitem--row [data-role=edit]");
      return b ? getComputedStyle(b).display : "(无按钮)";
    })(),
    first: items[0]?.dataset.name || "",
    order: items.slice(0, 3).map((e) => e.dataset.name),
    starOn: [...document.querySelectorAll(".vstar")].map((b) => b.classList.contains("on")),
  };
});

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true });

  // ---- 桌面：卡片 → 紧凑 ----
  const { ctx, page } = await openLib(browser);
  const before = await measure(page);
  console.log(`[卡片] ${before.n} 个 · 单卡 ${before.cardH}px · 整页 ${(before.docH / before.vh).toFixed(1)} 屏`);

  check("卡片视图会展示参考文本", before.refShown === true);

  await page.click("#l-view");
  await page.waitForTimeout(600);
  const after = await measure(page);
  console.log(`[紧凑] ${after.n} 个 · 单行 ${after.cardH}px · 整页 ${(after.docH / after.vh).toFixed(1)} 屏`);

  check("点了「紧凑列表」后行高明显变小", after.cardH < before.cardH * 0.55,
    `${before.cardH}px → ${after.cardH}px`);
  check("紧凑视图不再展示参考文本", after.refShown === false);
  // 24 行 × 50px = 1200px，本来就超过 1000px 的视口；再加上页头、搜索框、筛选行，
  // "一屏看完"是不可能的。真正该比的是「比卡片视图省了多少」。
  // 拿「列表本身」比，不拿整页：页头那几百像素是固定的，会把比例稀释掉
  check("列表高度比卡片视图少三成", after.listH < before.listH * 0.7,
    `列表 ${before.listH}px → ${after.listH}px`);
  check("音色数量不变", after.n === before.n, `${before.n} → ${after.n}`);

  // ---- 收藏：点第一个的星，应该立刻置顶 ----
  const target = await page.evaluate(() => {
    const items = [...document.querySelectorAll(".vitem")];
    const last = items[items.length - 1];
    return { name: last.dataset.name };
  });
  // 挑最后一个来收藏：它原本在最底下，收藏后必须跑到第一
  await page.evaluate((nm) => {
    const card = [...document.querySelectorAll(".vitem")].find((e) => e.dataset.name === nm);
    card.querySelector("[data-role=star]").click();
  }, target.name);
  await page.waitForTimeout(1200);
  const starred = await measure(page);
  check("收藏后立刻置顶到第一位", starred.order[0] === target.name,
    `收藏「${target.name}」，现在的顺序：${starred.order.join(" / ")}`);
  check("星标变成实心（on）", starred.starOn[0] === true);

  // ---- 刷新后：视图偏好 + 收藏都要还在 ----
  await page.reload({ waitUntil: "load" });
  await page.waitForSelector("#l-summary", { state: "attached", timeout: 20000 });
  await page.evaluate(() => { location.hash = "library"; });
  await page.waitForTimeout(1000);
  const reloaded = await measure(page);
  check("刷新后仍停在紧凑视图", reloaded.refShown === false,
    `单行 ${reloaded.cardH}px`);
  check("刷新后收藏顺序还在", reloaded.order[0] === target.name, reloaded.order.join(" / "));

  // ---- 还原：取消收藏，并把视图切回卡片 ----
  await page.evaluate((nm) => {
    const card = [...document.querySelectorAll(".vitem")].find((e) => e.dataset.name === nm);
    card.querySelector("[data-role=star]").click();
  }, target.name);
  await page.waitForTimeout(1000);
  const restored = await measure(page);
  check("取消收藏后星标熄灭", restored.starOn.every((x) => x === false));
  await page.locator("#l-view").click();
  await page.waitForTimeout(600);
  const backCard = await measure(page);
  check("能切回卡片视图", backCard.refShown === true);

  // ---- 手机：紧凑视图的收益最大 ----
  const m = await openLib(browser, { width: 390, height: 844 });
  const mobCard = await measure(m.page);
  await m.page.click("#l-view");
  await m.page.waitForTimeout(700);
  const mob = await measure(m.page);
  console.log(`[手机紧凑] 单行 ${mob.cardH}px · 整页 ${(mob.docH / mob.vh).toFixed(1)} 屏`);
  console.log(`   窄屏媒体查询匹配=${mob.mq}  编辑按钮 display=${mob.editDisp}  列表 ${mob.listH}px`);
  // 不拿「整页几屏」当指标：手机上的页头（标题/搜索/筛选/概览）本身就占一屏多，
  // 它和音色数量无关，会把结论带偏。量列表本身。
  check("手机上列表高度少一半以上", mob.listH < mobCard.listH * 0.5,
    `列表 ${mobCard.listH}px → ${mob.listH}px（整页 ${(mobCard.docH / mobCard.vh).toFixed(1)} → ${(mob.docH / mob.vh).toFixed(1)} 屏）`);

  await browser.close();
  const bad = results.filter((r) => !r.ok);
  console.log(`\n${results.length - bad.length}/${results.length} 通过`);
  process.exit(bad.length ? 1 : 0);
})().catch((e) => { console.error("异常:", e); process.exit(2); });
