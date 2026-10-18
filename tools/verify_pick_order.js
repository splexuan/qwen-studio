/* 验证「选音色」弹窗的排序与音色库一致：收藏的排最前，并带一个星标标记。
   弹窗和音色库是两份过滤代码（rowsNow / visibleVoices），排序逻辑必须共用
   sortVoices()，否则同一个音色在两处位置不同，用户会以为看到两批数据。 */
const { chromium } = require("playwright-core");
const BASE = "http://127.0.0.1:" + (process.env.QWENSTUDIO_PORT || "7877");

const results = [];
function check(name, ok, detail = "") {
  results.push({ ok, name, detail });
  console.log(`${ok ? "  OK  " : " FAIL "} ${name}${detail ? "  — " + detail : ""}`);
}

(async () => {
  const b = await chromium.launch({ channel: "chrome", headless: true });
  const c = await b.newContext({ viewport: { width: 1440, height: 1000 } });
  const p = await c.newPage();
  const errs = [];
  p.on("pageerror", (e) => errs.push(String(e)));

  await p.goto(BASE, { waitUntil: "load" });
  await p.waitForSelector("#c-pick", { state: "attached", timeout: 20000 });
  await p.waitForTimeout(600);

  // 挑最后一个音色收藏 —— 它原本排最后，收藏后必须跑到两处的最前面。
  // 从接口拿而不是读 window.BOOT：BOOT 是脚本作用域里的变量，不挂在 window 上。
  const all = await fetch(BASE + "/api/voices").then((r) => r.json());
  const voices = all.voices || [];
  // 用户自己的收藏是**他的数据**。这套断言要成立（「只有一个星标」「收藏的排第一」），
  // 前提是库里的星标只有本脚本加的那一个 —— 所以先全部取消、记下原本有谁，
  // 跑完再原样恢复。不这么做的话，库里本来就有收藏时这几条永远红着。
  const originallyStarred = voices.filter((v) => v.starred).map((v) => v.name);
  const setStar = async (name, on) => {
    await fetch(`${BASE}/api/voices/${encodeURIComponent(name)}/star`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ starred: on }),
    });
  };
  for (const n of originallyStarred) await setStar(n, false);

  const fresh = (await fetch(BASE + "/api/voices").then((r) => r.json())).voices || [];
  const target = fresh.length ? fresh[fresh.length - 1].name : "";
  check("拿到一个可以用来收藏的音色", !!target, target);

  // 改动走 Node 侧请求，再 reload 让页面按真实路径重新拿数据。
  // （evaluate 里的函数是在浏览器里跑的，拿不到 Node 作用域的变量 —— 之前就栽在这上面。）
  const star = async (on) => {
    await setStar(target, on);
    await p.reload({ waitUntil: "load" });
    await p.waitForSelector("#c-pick", { state: "attached", timeout: 20000 });
    await p.waitForTimeout(600);
  };

  await star(true);

  // ---- 弹窗：收藏的应该排第一并带星 ----
  await p.click("#c-pick");
  await p.waitForSelector("dialog.modal .vrow", { timeout: 8000 });
  await p.waitForTimeout(300);
  const dlg = await p.evaluate(() => {
    const rows = [...document.querySelectorAll("dialog.modal .vrow")];
    return {
      n: rows.length,
      first: rows[0]?.dataset.name || "",
      firstHasStar: !!rows[0]?.querySelector(".vrow__star"),
      starCount: document.querySelectorAll("dialog.modal .vrow__star").length,
    };
  });
  check("弹窗里收藏的音色排第一", dlg.first === target, `第一行是「${dlg.first}」`);
  check("第一行带收藏星标", dlg.firstHasStar);
  check("全列表只有一个星标（只收藏了一个）", dlg.starCount === 1, `${dlg.starCount} 个`);
  check("弹窗行数正确", dlg.n > 1, `${dlg.n} 行`);

  await p.keyboard.press("Escape");
  await p.waitForTimeout(300);

  // ---- 回车选中：应落在最靠上的那个（也就是收藏的那个）----
  await p.click("#c-pick");
  await p.waitForSelector("dialog.modal #__pq", { timeout: 8000 });
  await p.waitForTimeout(250);
  await p.keyboard.press("Enter");
  await p.waitForTimeout(500);
  // 选中的证据看「选音色」那颗按钮本身：它会把选中的名字显示出来。
  // （别去猜 #c-lib-detail 之类 —— 那个 id 是我编的，页面里根本没有。）
  const picked = await p.evaluate(() => ({
    btn: (document.querySelector("#c-pick")?.textContent || "").replace(/\s+/g, " ").trim(),
    dialogOpen: !!document.querySelector("dialog.modal[open]"),
  }));
  check("回车直接选中了最上面那个（常用音色一键可达）",
    picked.btn.includes(target), `按钮显示「${picked.btn}」，弹窗已关闭=${!picked.dialogOpen}`);

  check("控制台没有报错", errs.length === 0, errs.slice(0, 2).join(" | "));

  // ---- 还原：撤掉本脚本的收藏，再把用户原本收藏的那些放回去 ----
  await setStar(target, false);
  for (const n of originallyStarred) await setStar(n, true);
  const after = await fetch(BASE + "/api/voices").then((r) => r.json());
  const restoredSet = (after.voices || []).filter((v) => v.starred)
    .map((v) => v.name).sort().join(",");
  const beforeSet = [...originallyStarred].sort().join(",");
  check("收藏已还原（与测试前一致）", restoredSet === beforeSet,
    `测试前 [${beforeSet}] → 现在 [${restoredSet}]`);

  await b.close();
  const bad = results.filter((r) => !r.ok);
  console.log(`\n${results.length - bad.length}/${results.length} 通过`);
  process.exit(bad.length ? 1 : 0);
})().catch((e) => { console.error("异常:", e); process.exit(2); });
