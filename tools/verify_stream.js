/* 验证「边生成边播」这条路：开关 → SSE → Web Audio 无缝排程 → 收尾落到结果卡。

   为什么要在 AudioContext 原型上打桩：`AudioBufferSourceNode` 不在文档里，
   从 DOM 侧看不到「到底有没有排进音频管线」。记 start(t) 的调用是最直接的证据，
   而且顺带能验「块与块之间的起始时间是递增且首尾相接的」——这才叫无缝。

   跑法（项目根目录，先起一个验证实例）：
     QWENSTUDIO_PORT=7877 runtime\python\python.exe web/server.py
     NODE_PATH=<...>/node_modules node tools/verify_stream.js
   端口默认 7877，可用 QWENSTUDIO_PORT 覆盖。
*/
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:" + (process.env.QWENSTUDIO_PORT || "7877");
const OUT = "tools/_stream_play.png";

const PROBE = () => {
  window.__pcm = [];      // 每次 AudioBufferSourceNode.start(t) 的 t
  window.__buflen = [];   // 对应 buffer 的时长（秒）
  window.__media = [];    // 老播放器（<audio>）的 play 调用
  const C = window.AudioContext || window.webkitAudioContext;
  if (C) {
    const orig = C.prototype.createBufferSource;
    C.prototype.createBufferSource = function () {
      const src = orig.call(this);
      const st = src.start.bind(src);
      src.start = function (t) {
        try {
          window.__pcm.push(Math.round((t || 0) * 1000) / 1000);
          window.__buflen.push(src.buffer ? src.buffer.duration : 0);
        } catch (_) {}
        return st.apply(this, arguments);
      };
      return src;
    };
  }
  const P = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    window.__media.push(this.src || "none");
    return P.apply(this, arguments);
  };
};

const results = [];
function check(name, ok, detail = "", soft = false) {
  results.push({ name, ok: ok || soft, soft, detail });
  const tag = ok ? "  OK  " : (soft ? " NOTE " : " FAIL ");
  console.log(`${tag} ${name}${detail ? "  — " + detail : ""}`);
}

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  // 独立上下文：共用 localStorage 的话，测试往输入框里打的字会被 beforeunload 存成用户草稿
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await ctx.newPage();
  const errs = [];
  page.on("console", (m) => { if (m.type() === "error") errs.push(m.text()); });
  page.on("pageerror", (e) => errs.push(String(e)));

  await page.addInitScript(PROBE);
  await page.goto(BASE, { waitUntil: "load" });
  // 页面有 hash 路由（gotoView 会写 location.hash），切页用 hash 最稳，
  // 不依赖 gotoView 是否暴露在 window 上。等的是「右栏结果卡已经渲染出来」。
  await page.waitForSelector("#c-result [data-role=badge]", { timeout: 30000 });

  // ---- 1. 开关只在支持流式的页面出现 ----
  await page.evaluate(() => { location.hash = "official"; });
  await page.waitForTimeout(500);

  const oSw = await page.$("#o-result [data-role=streamon]");
  check("官方精品页有「边生成边播」开关", !!oSw);

  await page.evaluate(() => { location.hash = "clone"; });
  await page.waitForTimeout(400);
  const cSw = !!(await page.$("#c-result [data-role=streamon]"));
  check("克隆页也有开关", cSw);

  await page.evaluate(() => { location.hash = "script"; });
  await page.waitForTimeout(400);
  const scriptSw = !!(await page.$("#s-result [data-role=streamon]"));
  check("剧本工坊**没有**开关（后端不支持，不给人假选项）", !scriptSw);

  // ---- 2. 默认关，勾上以后记住 ----
  await page.evaluate(() => { location.hash = "official"; });
  await page.waitForTimeout(400);
  const defOff = await page.isChecked("#o-result [data-role=streamon]");
  check("默认不勾（不主动出声）", !defOff);
  await page.check("#o-result [data-role=streamon]");
  await page.waitForTimeout(150);
  const remembered = await page.evaluate(() => localStorage.getItem("qs-stream"));
  check("勾选状态写进 localStorage", remembered === "1", `qs-stream=${remembered}`);

  // ---- 3. 预热一单，把「首次加载权重 + 捕获 CUDA graph」那十几秒先消耗掉 ----
  // 不预热的话，长文本那一单会在首块之后立刻把缓冲耗光（RTF 掉到 1 附近），
  // Web Audio 只能重排，量出来的「块间缝隙」是真实存在的、但不是常态。
  // 真实使用里模型是驻留的，所以基线必须从热态取。
  await page.fill("#o-text", "预热。");
  await page.click("#o-go");
  await page.waitForFunction(() => {
    const b = document.querySelector("#o-result [data-role=badge]")?.textContent || "";
    return b.includes("已完成") || b.includes("失败");
  }, null, { timeout: 180000 }).catch(() => {});
  // waitForFunction 的签名是 (fn, arg, options)：超时放第二个会被当成 arg，
  // 实际仍用默认 30 秒 —— 长文本那一单就会被误判成「没跑完」。
  // 等这一单的声音放完，避免和下一单的排程混在一起
  await page.waitForFunction(
    () => {
      const r = document.querySelector("#o-result");
      const a = r?.querySelector("audio");
      return !a || (a.paused && !a.disabled);
    },
    null, { timeout: 30000 }).catch(() => {});
  await page.waitForTimeout(500);
  // 预热那一单用的是另一个 AudioContext（它自己的时间轴从 0 开始）。
  // 探针是全局数组，不清空就会把两套时间轴混在一起比，比出假的「重叠」。
  await page.evaluate(() => {
    (window.__pcm || []).length = 0;
    (window.__buflen || []).length = 0;
    (window.__media || []).length = 0;
  });

  // ---- 4. 生成：真的边生成边播 ----
  // 文本刻意写长：要的是「生成结束时音频还在响」，否则镜像根本没机会出现，
  // 也就测不到「进度条是不是真的跟着声音走」。
  await page.fill("#o-text",
    "边生成边播测试。这句话比较长，目的是让生成结束的时候音频还在响，" +
    "这样才看得出来结果卡的播放器是不是真的跟着声音在走。再来一句凑够十秒。");
  await page.click("#o-go");

  let sawStreamStage = false, sawBuffer = false, maxBacklog = 0;
  for (let i = 0; i < 600; i++) {
    const st = await page.evaluate(() => {
      const r = document.querySelector("#o-result");
      const stage = r?.querySelector("[data-role=stage]")?.textContent || "";
      const msg = r?.querySelector("[data-role=msg]")?.textContent || "";
      const badge = r?.querySelector("[data-role=badge]")?.textContent || "";
      const pcms = (window.__pcm || []).length;
      return { stage, msg, badge, pcms };
    });
    if (st.stage.includes("边生成边播")) sawStreamStage = true;
    const m = st.msg.match(/缓冲\s*([\d.]+)\s*秒/);
    if (m) {
      const b = parseFloat(m[1]);
      if (b > 0) sawBuffer = true;
      maxBacklog = Math.max(maxBacklog, b);
    }
    if (st.badge.includes("已完成") || st.badge.includes("失败")) break;
    await page.waitForTimeout(250);
  }

  check("进度区显示「边生成边播…」", sawStreamStage);
  check("显示了正数的播放缓冲（真的排进了 Web Audio）", sawBuffer, `峰值 ${maxBacklog.toFixed(2)}s`);

  const audio = await page.evaluate(() => ({
    starts: window.__pcm || [],
    lens: window.__buflen || [],
    media: window.__media || [],
  }));
  check("排了多块（不是整段一次性塞进去）", audio.starts.length >= 2,
    `${audio.starts.length} 块`);

  // 排程正确性：块不能重叠（重叠就是两块同时响）。至于「块之间有没有空档」，
  // 那取决于 RTF —— 缓冲见底时的等待是性能问题，不是排程 bug，所以单独一条、
  // 且只作提示（这台机器桌面占用一多，1.7B 的 RTF 就会从 0.8 掉到 3.4）。
  let overlap = 0, worstGap = 0;
  for (let i = 1; i < audio.starts.length; i++) {
    const end = audio.starts[i - 1] + audio.lens[i - 1];
    if (audio.starts[i] < end - 0.02) overlap++;
    worstGap = Math.max(worstGap, audio.starts[i] - end);
  }
  check("块与块不重叠（不会两个块同时响）", overlap === 0, `${overlap} 处重叠`);
  check("排程无断点（要求 RTF<1）", worstGap <= 0.02,
    worstGap > 0.02 ? `最长等待 ${(worstGap * 1000).toFixed(0)}ms` : "0ms", true);

  const verdict = await page.evaluate(() => {
    const r = document.querySelector("#o-result");
    return {
      badge: r?.querySelector("[data-role=badge]")?.textContent || "",
      dl: !!r?.querySelector("[data-role=dl]"),
      player: !r?.querySelector("[data-role=playerbox]")?.classList.contains("hidden"),
      params: (r?.querySelector("[data-role=params]")?.textContent || "").includes("本次参数"),
    };
  });
  check("收尾后结果卡是「已完成」", verdict.badge.includes("已完成"), verdict.badge);
  check("结果卡带播放器与下载", verdict.player && verdict.dl);

  // ---- 4. 镜像：结果卡出现时，播放器显示的是「正在播到这里」，不是 0:00 ----
  // 真实触发要「生成完时声音还没播完」，而这取决于当时显存够不够（1.7B 峰值 4.28 GB，
  // 桌面占用一多 RTF 就从 0.8 掉到 3.4，声音早播完了）。所以这里直接驱动那块 UI：
  // 用 __qsResult 拿控制器，手动喂一个位置，看它是否真的画成镜像态。
  const mir = await page.evaluate(() => {
    const r = document.querySelector("#o-result .card")?.__qsResult;
    if (!r || !r.player) return { skip: true };
    const btn = r.root.querySelector("[data-role=play]");
    const prog = r.root.querySelector("[data-role=prog]");
    const time = r.root.querySelector("[data-role=time]");
    r.player.mirror(6.5, 14);
    const on = { disabled: btn.disabled, pct: parseFloat(prog.style.width) || 0, time: time.textContent };
    r.player.mirror(12, 14);
    const later = parseFloat(prog.style.width) || 0;
    r.player.stopMirror(true);
    const off = { disabled: btn.disabled, pct: parseFloat(prog.style.width) || 0 };
    return { on, later, off };
  });
  if (mir.on) {
    check("镜像时播放按钮锁住（否则会开出第二条音轨）", mir.on.disabled === true);
    check("镜像进度按位置画，不是 0%", mir.on.pct > 40 && mir.on.pct < 55,
      `${mir.on.pct.toFixed(0)}%`);
    check("镜像时间不是 0:00 起", !/^0:00 \//.test(mir.on.time.trim()), mir.on.time);
    check("位置往后走，进度条跟着走", mir.later > mir.on.pct,
      `${mir.on.pct.toFixed(0)}% → ${mir.later.toFixed(0)}%`);
    check("退出镜像后按钮解锁", mir.off.disabled === false);
  } else {
    check("取到结果卡控制器（镜像断言的前提）", false, "没拿到 __qsResult");
  }

  // 真实播放这一路上的两条硬约束，不受性能影响，照测（audio 是上面那次采样）
  check("结果卡没有二次自动播放（流式已经播过了）", audio.media.length === 0,
    `${audio.media.length} 次 play`);
  check("声音只走 Web Audio 一条通道（<audio> 始终没播）",
    await page.evaluate(() => {
      const a = document.querySelector("#o-result audio");
      return !a || a.paused;
    }));

  check("结果卡带「本次参数」表", verdict.params);
  check("控制台没有报错", errs.length === 0, errs.slice(0, 2).join(" | "));

  await page.screenshot({ path: OUT });

  const bad = results.filter((r) => !r.ok);
  console.log(`\n${results.length - bad.length}/${results.length} 通过`);
  await browser.close();
  process.exit(bad.length ? 1 : 0);
})().catch((e) => {
  console.error("脚本异常：", e);
  process.exit(2);
});
