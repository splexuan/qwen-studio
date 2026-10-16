/* 一次性验证脚本：声音设计的「存进音色库」。

   要验三件事：
   1. 生成之后，结果卡底部出现「存进音色库」（**只有声音设计有** ——
      克隆页自己就带一个，官方音色是内置的，剧本工坊一次生成好几个角色）
   2. 点开时默认名字来自描述的第一段，并且把「多长 / 参考文本多少字」摆出来；
      时长超出建议区间要给出提醒
   3. 存出来的音色真的进库，参考文本**逐字**等于生成时用的那段文字，
      走完整 ICL（那段文字就是它自己念的，没必要退成仅向量）

   **生成那一步是打桩的**（拦 /api/jobs，回一个写好的结果）：真跑一次声音设计要
   加载模型占 6 GB 显存，而用户自己那台可能正在用。保存那一段走的是真后端、真落库。
   同理，/api/voices 的请求体里把 prompt 改成 false，不排抽取音色的后台任务。

   自给自足：临时音色用完删掉；参考音频借 outputs/ 里现成的生成结果，不新造文件。

   跑法（在项目根目录）：
     NODE_PATH=<...>/node_modules node tools/verify_design_save.js
   端口默认 7867，可用 QWENSTUDIO_PORT 覆盖。
*/
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:" + (process.env.QWENSTUDIO_PORT || "7867");
const OUT = "tools/_design_save.png";
const NAME = "__验证_设计存音色";
const DESIGN_TEXT = "这句话是声音设计生成的，存成音色之后它就成了这段参考音频的参考文本。";
const DESIGN_DESC = "三十五岁上下的女声，嗓音清亮带一点沙，语速偏慢，句尾轻轻收住";

const jget = async (p) => (await fetch(BASE + p)).json();
const jpost = async (p, b) => (await fetch(BASE + p, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(b),
})).json();

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true, args: ["--mute-audio"] });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });  // 隔离的 localStorage
  const page = await ctx.newPage();

  const fails = [];
  const ok = (label, cond, detail = "") => {
    console.log(`  [${cond ? "OK  " : "FAIL"}] ${label}${detail ? "  → " + detail : ""}`);
    if (!cond) fails.push(label);
  };
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => consoleErrors.push("pageerror: " + e.message));

  const cdp = await ctx.newCDPSession(page);
  await cdp.send("Network.setCacheDisabled", { cacheDisabled: true });

  await jpost("/api/voices/delete", { names: [NAME] });

  // 借 outputs/ 里一条现成的生成结果当「生成产物」。它是不是声音设计出的不重要 ——
  // 存音色只关心那个文件在不在 outputs/ 下、读不读得出音频。
  const hist = await jget("/api/history");
  const donor = (hist.history || []).find((h) => h.file_exists);
  if (!donor) { console.log("outputs/ 里没有可用的生成结果，无法验证"); await browser.close(); process.exit(1); }
  console.log(`[0] 借用现成结果 ${donor.file} 当这次的「生成产物」`);

  // ---- 打桩：生成那一步 ----
  let fake = { mode: "design", duration: 5.0 };
  await page.route("**/api/jobs", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    return route.fulfill({ status: 200, contentType: "application/json",
      body: JSON.stringify({ ok: true, job: "fake_job" }) });
  });
  await page.route("**/api/jobs/fake_job", async (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({
      ok: true, state: "done", progress: 1.0, message: `耗时 3.2s · RTF 0.66 · 音频 ${fake.duration}s`,
      result: { url: "/media/out/" + encodeURIComponent(donor.file), file: donor.file,
                duration: fake.duration, level: "ok" },
    }),
  }));
  // 抽音色基因要加载模型 —— 改掉请求体里的 prompt 再放行，别去挤显存。
  // （名称唯一 / 参考文本这些逻辑跟 prompt 无关，测的仍是真后端。）
  await page.route("**/api/voices", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    let body = {};
    try { body = JSON.parse(route.request().postData() || "{}"); } catch (_) {}
    body.prompt = false;
    return route.continue({ postData: JSON.stringify(body) });
  });

  const goto = async (hash) => {
    await page.goto("about:blank");
    await page.goto(BASE + "/" + hash, { waitUntil: "networkidle" });
  };
  const waitDone = (prefix) => page.waitForFunction((p) => {
    const b = document.querySelector(`#${p}-result [data-role=badge]`);
    return b && /完成/.test(b.textContent);
  }, prefix, { timeout: 15000 });

  const genDesign = async () => {
    await goto("#design");
    await page.fill("#d-text", DESIGN_TEXT);
    await page.fill("#d-instruct", DESIGN_DESC);
    await page.click("#d-go");
    await waitDone("d");
  };
  // 克隆页要先选一个音色（现在是「按钮 → 弹窗」），否则点生成只会吐一句
  // 「先选一个音色」、任务根本不会发出去
  const genClone = async () => {
    await goto("#clone");
    await page.waitForSelector("#c-pick", { timeout: 15000 });
    await page.click("#c-pick");
    await page.waitForSelector("dialog.modal .vrow", { timeout: 8000 });
    await page.click("dialog.modal .vrow");
    await page.fill("#c-text", DESIGN_TEXT);
    await page.click("#c-go");
    await waitDone("c");
  };

  try {
    // ================= 1. 声音设计：按钮出现 =================
    console.log("[1] 声音设计生成完之后");
    await genDesign();
    const hasBtn = await page.$("#d-result [data-role=savevoice]") !== null;
    ok("结果卡底部出现「存进音色库」", hasBtn);
    const btnText = (await page.textContent("#d-result [data-role=acts]"))?.replace(/\s+/g, " ").trim() || "";
    ok("旁边说明了存下来的用途", btnText.includes("语音克隆"), btnText.slice(0, 60));
    await page.screenshot({ path: OUT });

    // ================= 2. 弹窗内容 =================
    console.log("[2] 点开后的弹窗");
    await page.click("#d-result [data-role=savevoice]");
    await page.waitForSelector("dialog.modal #__nm", { timeout: 8000 });
    const name1 = await page.inputValue("dialog.modal #__nm");
    ok("默认名字取自描述的第一段", name1 === "三十五岁上下的女声", JSON.stringify(name1));
    const body1 = (await page.textContent("dialog.modal .modal__body"))?.replace(/\s+/g, " ") || "";
    ok("报出参考文本字数（就是刚写的那段）",
       body1.includes(`${DESIGN_TEXT.length}`), body1.slice(0, 80));
    ok("时长在建议区间内时不报警", await page.$("dialog.modal .hint--warn") === null);

    // 超长时要提醒（只是提醒，不拦）
    await page.click("dialog.modal [data-role=no]");
    await page.waitForTimeout(200);
    fake = { mode: "design", duration: 30.0 };
    await genDesign();
    await page.click("#d-result [data-role=savevoice]");
    await page.waitForSelector("dialog.modal #__nm", { timeout: 8000 });
    const warn = (await page.textContent("dialog.modal .modal__body"))?.replace(/\s+/g, " ") || "";
    ok("时长超出建议区间时给出提醒",
       await page.$("dialog.modal .hint--warn") !== null, warn.slice(40, 120));

    // ================= 3. 真存一次 =================
    console.log("[3] 真存一次");
    fake = { mode: "design", duration: 5.0 };
    await page.click("dialog.modal [data-role=no]");
    await page.waitForTimeout(200);
    await genDesign();
    await page.click("#d-result [data-role=savevoice]");
    await page.waitForSelector("dialog.modal #__nm", { timeout: 8000 });
    await page.fill("dialog.modal #__nm", NAME);
    await page.click("dialog.modal [data-role=yes]");
    await page.waitForTimeout(1500);

    const voices = (await jget("/api/voices")).voices;
    const made = voices.find((v) => v.name === NAME);
    ok("音色已进库", !!made, made ? `时长 ${made.duration}s · ${made.chars} 字` : "没找到");
    ok("参考文本逐字等于生成时用的那段文字", made && made.ref_text === DESIGN_TEXT,
       made ? JSON.stringify(made.ref_text.slice(0, 24)) + `（${made.chars} 字）` : "");
    ok("走的是完整 ICL（文字是它自己念的，没必要退成仅向量）", made && made.xvec_only === false,
       String(made?.xvec_only));
    const toastText = (await page.textContent("#toasts")) || "";
    ok("给了「已存成音色」的确认", toastText.includes("已存成音色"), toastText.trim().slice(-30));

    // 参考音频确实是那条生成结果，不是别的。
    // **比解码后的波形，不比字节** —— 存音色会按自己的格式重写一遍
    // （重采样 + 改采样格式），字节必然不同，拿字节比只会得到一个假失败。
    const same = await page.evaluate(async ([n, f]) => {
      const dec = async (u) => {
        const ab = await (await fetch(u)).arrayBuffer();
        const c = new (window.AudioContext || window.webkitAudioContext)();
        const buf = await c.decodeAudioData(ab.slice(0));
        c.close();
        return buf;
      };
      const a = await dec("/media/voice/" + encodeURIComponent(n));
      const b = await dec("/media/out/" + encodeURIComponent(f));
      if (Math.abs(a.duration - b.duration) > 0.01) {
        return { eq: false, note: `时长 ${a.duration.toFixed(3)}s vs ${b.duration.toFixed(3)}s` };
      }
      const x = a.getChannelData(0), y = b.getChannelData(0);
      if (x.length !== y.length) return { eq: false, note: `${x.length} vs ${y.length} 个采样` };
      let max = 0;
      for (let i = 0; i < x.length; i += 97) max = Math.max(max, Math.abs(x[i] - y[i]));
      return { eq: max < 0.01, note: `${a.duration.toFixed(3)}s · ${x.length} 采样 · 最大采样差 ${max.toFixed(5)}` };
    }, [NAME, donor.file]);
    ok("存下来的参考音频就是那条生成产物", same.eq, same.note);

    // ================= 4. 别的模式不给这个按钮 =================
    console.log("[4] 其它模式不该出现这个按钮");
    fake = { mode: "clone", duration: 5.0 };
    await genClone();
    await page.waitForTimeout(300);
    ok("语音克隆的结果卡没有「存进音色库」", await page.$("#c-result [data-role=savevoice]") === null);
    ok("结果卡的动作区整个是隐藏的",
       await page.$eval("#c-result [data-role=acts]", (el) => el.classList.contains("hidden")));

    // 结果文件被清理掉了要给一句能看懂的话，而不是一句「失败」
    const gone = await jpost("/api/voices", { name: NAME, out: "根本没有这个文件.wav" });
    ok("指向不存在的生成结果 → 说清是哪一步",
       gone.ok === false && /不在 outputs/.test(gone.error || ""), gone.error);

    ok("全程无 JS 报错", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | "));
  } finally {
    try { await jpost("/api/voices/delete", { names: [NAME] }); } catch (_) {}
    const left = (await jget("/api/voices")).voices.map((v) => v.name);
    console.log("[5] 已删掉临时音色");
    if (left.includes(NAME)) { console.log("   残留：" + NAME); fails.push("临时音色没清干净"); }
    await browser.close();
  }

  console.log("\n" + "=".repeat(60));
  if (fails.length) {
    console.log(`${fails.length} 项未通过：`);
    fails.forEach((f) => console.log("   - " + f));
    process.exit(1);
  }
  console.log("全部通过");
  console.log("截图：" + OUT);
})();
