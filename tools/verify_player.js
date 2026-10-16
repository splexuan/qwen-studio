/* 一次性验证脚本：播放器这套东西（音色库卡片 / 合成记录行 / 结果卡）的进度条与播放行为。
   用 playwright-core 驱动本机 Chrome。

   它盯两件事：
     1. 点「试听」**立刻出声**（不再需要展开后再按一次播放）；
     2. 进度条 / 未播遮罩 / 播放头三者同步，且能暂停、能点波形跳转；
     3. 收起播放器时**声音也停**（否则会留下一个看不见、又按不到暂停的播放）。

   进度断言不写死百分比（记录里可能有 2 秒的、也可能有 2 分钟的），
   一律换算成「实际推进 ≈ 播放时长 / 总时长」。

   跑法（在项目根目录）：
     NODE_PATH=<...>/node_modules node tools/verify_player.js
   端口默认 7867，可用 QWENSTUDIO_PORT 覆盖（测试实例在别的端口时）。
*/
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:" + (process.env.QWENSTUDIO_PORT || "7867");
const OUT = "tools/_player_progress.png";

/* 把 play / pause 的调用记在一个数组里。为什么要动原型：`new Audio()` 不在文档里，
   播放器节点又被整片替换掉，从 DOM 侧根本看不到「那个元素还在响吗」。
   记调用是最直接的证据。 */
const MEDIA_PROBE = () => {
  window.__media = [];
  const P = HTMLMediaElement.prototype.play, Q = HTMLMediaElement.prototype.pause;
  HTMLMediaElement.prototype.play = function () {
    window.__media.push(["play", this.src ? "src" : "none"]);
    return P.apply(this, arguments);
  };
  HTMLMediaElement.prototype.pause = function () {
    window.__media.push(["pause", Math.round((this.currentTime || 0) * 100) / 100]);
    return Q.apply(this, arguments);
  };
};

(async () => {
  const browser = await chromium.launch({
    channel: "chrome",
    headless: true,
    args: ["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
  await page.addInitScript(MEDIA_PROBE);

  const fails = [];
  const ok = (label, cond, detail = "") => {
    console.log(`  [${cond ? "OK  " : "FAIL"}] ${label}${detail ? "  → " + detail : ""}`);
    if (!cond) fails.push(label);
  };
  const marks = () => page.evaluate(() => window.__media.map((m) => m[0]));
  const parseDur = (t) => {
    const m = /\/\s*(\d+):(\d\d)/.exec(t || "");
    return m ? Number(m[1]) * 60 + Number(m[2]) : 0;
  };
  const snapshot = (sel) => page.evaluate((s) => {
    const w = document.querySelector(s);
    if (!w) return null;
    const headEl = w.querySelector("[data-role=head]");
    const waveEl = w.querySelector("[data-role=wave]");
    // left 用的是 clamp(1px, p%, calc(100% - 1px))，字面值解析不了 —— 读**计算后**的像素值
    const headPx = parseFloat(getComputedStyle(headEl).left) || 0;
    return {
      prog: parseFloat(w.querySelector("[data-role=prog]").style.width) || 0,
      rest: parseFloat(w.querySelector("[data-role=rest]").style.left) || 0,
      headPct: (headPx / (waveEl.clientWidth || 1)) * 100,
      time: w.querySelector("[data-role=time]").textContent,
      icon: w.querySelector("[data-role=picon]").innerHTML,
      hasProgEl: !!w.querySelector(".wpl-prog"),
      hasRestEl: !!w.querySelector(".wpl-rest"),
    };
  }, sel);
  const ICON_PAUSE = /M7 5h4v14H7z/;   // 播放键变成「两根竖条」= 正在播

  // ================= 合成记录行 =================
  await page.goto(BASE + "/#history", { waitUntil: "networkidle" });
  await page.waitForSelector(".hrow", { timeout: 15000 });
  console.log("[1] 合成记录已加载");

  await page.click(".hrow [data-role=play]");   // 点「试听」
  await page.waitForSelector(".hrow .wpl", { timeout: 8000 });
  await page.waitForFunction(
    () => !!document.querySelector(".hrow .wpl canvas") &&
          document.querySelector(".hrow .wpl canvas").width > 2,
    { timeout: 15000 }
  );

  const fresh = await snapshot(".hrow .wpl");
  const openedMarks = await marks();
  console.log("[2] 刚点开：", JSON.stringify(fresh));
  ok("进度元素存在（.wpl-prog）", fresh.hasProgEl);
  ok("未播遮罩元素存在（.wpl-rest）", fresh.hasRestEl);
  ok("点「试听」自己就触发了 play（没人去按播放键）",
     openedMarks.filter((m) => m === "play").length >= 1, openedMarks.join(","));
  ok("播放键已变暂停键", ICON_PAUSE.test(fresh.icon), fresh.icon);

  // 进度按真实时间走：等 2.5s，看推进量对不对得上总时长
  // （总时长要等到 loadedmetadata 才有，所以从等待后的快照里读，别读刚点开那一瞬）
  const WAIT_MS = 2500;
  await page.waitForTimeout(WAIT_MS);
  const t1 = await snapshot(".hrow .wpl");
  const total = parseDur(t1.time);
  console.log(`[3] 播了 ${WAIT_MS / 1000}s（总长 ${total}s）：prog=${t1.prog.toFixed(2)}%`);
  ok("读到了音频总时长（否则下一条断言是空的）", total > 0, t1.time);
  ok("进度条在走", t1.prog > 0.2, `prog=${t1.prog}%`);

  // 「进度跟真实时间对得上」要**比速率，不是比从点开算起的偏移量**。
  // 无头 Chrome 里音频时钟本身只跑到实时的九成出头，再加上解码起步的零点几秒，
  // 拿「点了多久」当基准会稳定差十几个百分点（实测 36% vs 期望 50%，三次都一样），
  // 那是环境不是功能。取两次快照算斜率，起步延迟和时钟误差就被一起约掉了 ——
  // 而且真出问题（比如进度条只走一半速度）照样抓得住。
  const RATE_MS = 1200;
  await page.waitForTimeout(RATE_MS);
  const t1b = await snapshot(".hrow .wpl");
  const rate = (t1b.prog - t1.prog) / (RATE_MS / 1000);
  const want = total ? 100 / total : 0;
  console.log(`    速率 ${rate.toFixed(2)}%/s，按总长应为 ${want.toFixed(2)}%/s`);
  ok("推进速率与总时长对得上（±25%）",
     want > 0 && Math.abs(rate - want) / want < 0.25,
     `${rate.toFixed(2)}%/s vs ${want.toFixed(2)}%/s`);
  ok("未播遮罩同步左移", Math.abs(t1b.rest - t1b.prog) < 0.5, `rest=${t1b.rest}%`);
  ok("播放头同步移动", Math.abs(t1b.headPct - t1b.prog) < 2,
     `head≈${t1b.headPct.toFixed(2)}% vs prog=${t1b.prog.toFixed(2)}%`);
  ok("时间文本在走", /^\d+:\d\d \/ \d+:\d\d$/.test(t1b.time) && !/^0:00/.test(t1b.time), t1b.time);

  // 暂停：进度原地保留，不清零、也不再走
  await page.click(".hrow .wpl .pbtn");
  await page.waitForTimeout(500);
  const t2 = await snapshot(".hrow .wpl");
  const pauseMarks = await marks();
  console.log(`[4] 暂停后：prog=${t2.prog.toFixed(2)}%，媒体事件=${pauseMarks.join(",")}`);
  ok("暂停确实调了 pause", pauseMarks[pauseMarks.length - 1] === "pause");
  // 基准是**暂停前那一瞬**的 t1b，不是更早的 t1 —— 中间为了量速率又放过 1.2s，
  // 拿 t1 比会把「正常播放推进的量」误判成「暂停后还在走」。
  ok("暂停后进度原地保留（不清零、也不再走）",
     Math.abs(t2.prog - t1b.prog) < 1, `${t1b.prog.toFixed(2)}% → ${t2.prog.toFixed(2)}%`);
  ok("暂停后播放键变回播放三角", !ICON_PAUSE.test(t2.icon));

  // 点波形跳转
  const box = await page.locator(".hrow .wpl [data-role=wave]").boundingBox();
  await page.mouse.click(box.x + box.width * 0.75, box.y + box.height / 2);
  await page.waitForTimeout(300);
  const t3 = await snapshot(".hrow .wpl");
  console.log("[5] 点击波形 75% 处：", JSON.stringify({ prog: t3.prog }));
  ok("点击波形可跳转（约 75%）", Math.abs(t3.prog - 75) < 8, `prog=${t3.prog}%`);

  await page.screenshot({ path: OUT });

  // 结果卡的播放器也带进度元素
  const resHas = await page.evaluate(() => !!document.querySelector("[data-role=playerbox] .wpl-prog"));
  ok("生成结果卡的播放器同样带进度元素", resHas);

  // ---- 收起播放器必须「连声音一起停」----
  // 点「试听」现在是**一下就开始播**，节点一撤那个 Audio 再没人 pause，
  // 就会留下一个听得见、却按不到暂停的播放 —— 这条必须钉住。
  const box2 = await page.locator(".hrow .wpl [data-role=wave]").boundingBox();
  await page.mouse.click(box2.x + 2, box2.y + box2.height / 2);   // 回到开头附近
  await page.waitForTimeout(200);
  await page.click(".hrow .wpl .pbtn");                          // 接着播
  const beforeCollapse = await marks();
  await page.waitForTimeout(200);

  await page.click(".hrow [data-role=play]");                     // 再点「试听」= 收起
  await page.waitForTimeout(300);
  const afterCollapse = await marks();
  const gone = await page.evaluate(() => !document.querySelector(".hrow .wpl"));
  console.log(`[6] 收起：媒体事件 ${beforeCollapse.join(",")} → ${afterCollapse.join(",")}`);
  ok("收起前确实在播（用于对照）", beforeCollapse[beforeCollapse.length - 1] === "play");
  ok("收起后播放器节点已移除", gone);
  ok("收起的同时声音也停了（末尾是 pause）",
     afterCollapse[afterCollapse.length - 1] === "pause");

  // ================= 音色库卡片 =================
  // hash 只是同文档导航，可能永远不触发 load —— 先回空白页再进
  await page.goto("about:blank");
  await page.goto(BASE + "/#library", { waitUntil: "networkidle" });
  await page.waitForSelector(".vitem [data-role=play]", { timeout: 15000 });
  const libMarks0 = (await marks()).length;
  await page.click(".vitem [data-role=play]");
  await page.waitForSelector(".vitem .wpl", { timeout: 8000 });
  await page.waitForTimeout(1000);

  const lib = await snapshot(".vitem .wpl");
  const libMarks = (await marks()).slice(libMarks0);
  console.log(`[7] 音色库卡片试听 1s 后：prog=${lib.prog.toFixed(2)}% icon=${ICON_PAUSE.test(lib.icon)}`);
  ok("音色库卡片：点「试听」自动开始播放", libMarks.includes("play"), libMarks.join(","));
  ok("  · 播放键已变暂停键", ICON_PAUSE.test(lib.icon), lib.icon);
  ok("  · 进度条在走", lib.prog > 0.2, `prog=${lib.prog}%`);

  await page.screenshot({ path: "tools/_library_audition.png" });
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
