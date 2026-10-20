/* 一次性验证脚本：音色库 / 剧本工坊 / 合成记录的几处扫视与检索改动。

   要验四件事：
   1. 音色名用强调色（满屏灰卡片里名字要跳得出来）
   2. 音色库的模糊搜索：子串命中、顺序字符命中、和「克隆方式」筛选叠加、
      空态给得清楚、Esc 清空
   3. 剧本工坊的角色音色快捷条：点一下插到光标处（光标在行中间时另起一行）、
      已用的角色淡下去、插入的内容落进草稿
   4. 合成记录的正文被截两行：短悬停预览仍可用；点击正文打开持久详情，
      正文可选中，有「复制全文 / 复制文本参数」，Esc 可关，滚动不受影响

   自给自足：临时造两个音色、往 outputs/history.json 塞两条记录，收尾全撤掉。
   **用独立的浏览器上下文**（不共享 localStorage）—— 否则测试往剧本框里打的字
   会在 unload 时被 beforeunload 存成用户的草稿，把人家写的东西冲掉。

   跑法（在项目根目录）：
     NODE_PATH=<...>/node_modules node tools/verify_library_ux.js
   端口默认 7867，可用 QWENSTUDIO_PORT 覆盖。
*/
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:" + (process.env.QWENSTUDIO_PORT || "7867");
const OUT = "tools/_library_ux.png";
const ROOT = path.resolve(__dirname, "..");
const HIST = path.join(ROOT, "outputs", "history.json");
const V1 = "__验证_甲乙";
const V2 = "__验证_丙丁";
const V3 = "__验证_戊己";
const WAV = process.env.UX_TEST_WAV
  || path.join(ROOT, "voices", fs.readdirSync(path.join(ROOT, "voices"))
    .find((n) => n.toLowerCase().endsWith(".wav") && !n.startsWith("__验证_")) || "sample.wav");

const jget = async (p) => (await fetch(BASE + p)).json();
const jpost = async (p, b) => (await fetch(BASE + p, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(b),
})).json();

// 得**真的**超过两行才会被截。实测 1440 宽的窗口下 `.hrow__text` 有 1086px 宽、
// 一行 13.5px 的汉字能放约 80 个 —— 两行就是 160 字。第一版写了 155 字，
// 刚好卡在边界里没被截到，结果「悬停弹全文」那几条断言全在空测。
// 这里给到三百多字，留足余量。
const LONG = "（验证用）这一段是特意写得很长的正文，用来确认合成记录里的文本会被截成两行，"
  + "并且鼠标移到上面时能弹出完整原文。这一段要足够长 —— 在宽屏上，一行就能放下八十来个汉字，"
  + "只写一两句的话根本触不到第二行的边，浮层也就不会被挂上去，测出来的「通过」是假的。"
  + "所以这里一直往下续，续到它稳稳地超过两行、真的被裁掉为止。"
  + "再补一段同样的话，免得将来窗口更宽、字体更小的时候，这段又缩回两行以内。"
  + "正文本身没有意义，它唯一的用途就是把这一段的高度撑过两行。"
  // 后半段是刻意凑长的：超过 600 字才走得到「列表里只渲染前 600 字」那条路，
  // 超过 700 字才走得到浮层的长文缩字号。两件事都得被这一步踩到，否则等于没测。
  + "以下是重复内容，只为把长度推过六百字和七百字这两个坎 —— "
  + "甲：参考音频要干净，一段两三秒的自然说话胜过十秒带混响的素材。"
  + "乙：参考文本要逐字对准，完整 ICL 模式下写错一个字，出来就是另一种腔调。"
  + "丙：情绪先改标点，把句号换成破折号或者省略号，比堆形容词立竿见影。"
  + "丁：同一段文本连着跑两次本来就不会逐帧一致，想比出差异得跑够五条看均值。"
  + "戊：显存只有六个 G，同一时刻只让一套权重住进去，换规格之前必须先卸载。";
const SHORT = "（验证用）短句。";
const RID_LONG = "verify_ux_long";
const RID_SHORT = "verify_ux_short";

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true, args: ["--mute-audio"] });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });   // 隔离的 localStorage
  const page = await ctx.newPage();

  const fails = [];
  const ok = (label, cond, detail = "") => {
    console.log(`  [${cond ? "OK  " : "FAIL"}] ${label}${detail ? "  → " + detail : ""}`);
    if (!cond) fails.push(label);
  };
  const consoleErrors = [];
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    // 下面有一条**故意**去撞同名保存的用例，浏览器会为那个 409 记一条
    // 「Failed to load resource」——那是我们自己要的结果，不是页面报错。
    // 白名单只放这一条，别的 console.error 一律照旧算失败。
    if (/Failed to load resource.*status of 409/.test(m.text())) return;
    consoleErrors.push(m.text());
  });
  page.on("pageerror", (e) => consoleErrors.push("pageerror: " + e.message));

  const cdp = await ctx.newCDPSession(page);
  await cdp.send("Network.setCacheDisabled", { cacheDisabled: true });

  // 完整加载目标页（先 about:blank，避免被当成同文档的 hash 跳转）
  const goto = async (hash) => {
    await page.goto("about:blank");
    await page.goto(BASE + "/" + hash, { waitUntil: "networkidle" });
  };
  const namesNow = () => page.evaluate(() =>
    [...document.querySelectorAll(".vitem")].map((e) => e.dataset.name));
  const typeQ = async (q) => {
    await page.fill("#l-q", q);
    await page.waitForTimeout(220);
  };

  // ---- 准备：临时音色 + 临时记录 ----
  const lib = await jget("/api/voices");
  if (!lib.voices?.length) { console.log("音色库是空的，无法验证"); process.exit(1); }
  // 「另存为」必须从一个真实存在的音色复制；优先挑有参考文本的那个，
  // 这样即使某个库只留下空文本的音色，也不会在准备阶段误判。
  const src = (lib.voices.find((v) => v.ref_text) || lib.voices[0]).name;
  await jpost("/api/voices/delete", { names: [V1, V2, V3] });
  // 参考文本现在是**必填**项（完整 ICL 没它根本用不了，入口就会拒）。
  // 「另存为」只在源音色自己有 ref_text 时才继承；这里源音色可能是空文本，
  // 所以显式给一段 —— 否则建不出临时音色，整个脚本挂在准备阶段。
  const a = await jpost("/api/voices",
    { name: V1, voice: src, ref_text: "验证用参考文本", xvec_only: true, prompt: false });
  const b = await jpost("/api/voices",
    { name: V2, voice: src, ref_text: "验证用参考文本", xvec_only: false, prompt: false });
  if (a.name !== V1 || b.name !== V2) {
    console.log("临时音色没建起来：" + JSON.stringify([a, b]));
    process.exit(1);
  }

  const histOrig = fs.existsSync(HIST) ? fs.readFileSync(HIST, "utf8") : "[]";
  const mkRec = (id, text) => ({
    id, ts: "2026-01-01 00:00:00", mode: "clone", size: "1.7B", language: "Auto",
    voice: src, instruct: "", text, chars: text.length,
    file: "", duration: 1.0, status: "ok",
  });
  let items = [];
  try { items = JSON.parse(histOrig) || []; } catch (_) { items = []; }
  fs.writeFileSync(HIST, JSON.stringify([mkRec(RID_LONG, LONG), mkRec(RID_SHORT, SHORT), ...items], null, 1),
    "utf8");
  console.log(`[0] 临时音色 ${V1} / ${V2}，临时记录 ${RID_LONG} / ${RID_SHORT} 已就位`);

  try {
    // ======================= 1. 名字的强调色 =======================
    await goto("#library");
    await page.waitForSelector(".vitem", { timeout: 15000 });
    console.log("[1] 音色名的颜色");
    const col = await page.evaluate(() => {
      // 取两个令牌的实测值来对：正文色（名字该用的）和弱化色（参考文本该用的）。
      // 原来这里比的是 --accent，但强调色后来被收紧成**只给色块和图标**用 ——
      // 它对白底只有约 4.5:1，14px 汉字正文看着发虚，所以名字改用 --text + 700 字重。
      // 这条断言要守住的是「名字比参考文本显眼」，不是「名字必须是紫色」。
      const probe = (v) => {
        const s = document.createElement("span");
        s.style.color = `var(${v})`;
        document.body.appendChild(s);
        const c = getComputedStyle(s).color;
        s.remove();
        return c;
      };
      const text = probe("--text"), weak = probe("--text-3");
      const nm = document.querySelector(".vitem__name");
      const rf = document.querySelector(".vitem__ref");
      return {
        name: nm ? getComputedStyle(nm).color : "",
        text, weak,
        weight: nm ? getComputedStyle(nm).fontWeight : "",
        ref: rf ? getComputedStyle(rf).color : "",
      };
    });
    ok("音色名用正文色（不是灰）", col.name === col.text, `${col.name} vs --text ${col.text}`);
    ok("音色名是粗体", Number(col.weight) >= 700, col.weight);
    ok("名字和参考文本的灰不同色", col.name !== col.ref, `${col.name} vs ${col.ref}`);

    // ======================= 2. 模糊搜索 =======================
    console.log("[2] 音色库模糊搜索");
    ok("搜索框在（type=search）",
       await page.$eval("#l-q", (el) => el.tagName === "INPUT" && el.type === "search"));

    const total = (await namesNow()).length;
    await typeQ("甲乙");
    const q1 = await namesNow();
    ok("子串命中：留下甲乙、排除丙丁",
       q1.includes(V1) && !q1.includes(V2), q1.join("、") || "（空）");
    ok("确实筛掉了东西", q1.length < total, `${q1.length} / ${total}`);

    await typeQ("验证甲乙");            // 少了「__」，只能靠顺序字符命中
    const q2 = await namesNow();
    ok("顺序字符命中（「验证甲乙」→「__验证_甲乙」）", q2.includes(V1), q2.join("、") || "（空）");

    const summary = (await page.textContent("#l-summary"))?.trim() || "";
    ok("概要行切成「筛出 N / 共 M」", /^筛出 \d+ \/ 共 \d+ 个音色$/.test(summary), summary);

    await typeQ("zzz_根本没有这个音色");
    const empty = (await page.textContent("#l-grid-host"))?.replace(/\s+/g, "") || "";
    ok("搜不到时给的是「没有名字匹配」而不是「这一档没有」",
       empty.includes("没有名字匹配"), (await page.textContent("#l-grid-host"))?.trim().slice(0, 60));
    ok("空态里有「清空筛选」按钮", await page.$("#l-grid-host [data-role=clearq]") !== null);

    await page.click("#l-grid-host [data-role=clearq]");
    await page.waitForTimeout(220);
    const cleared = await namesNow();
    ok("点「清空筛选」回到全部", cleared.length === total, `${cleared.length} / ${total}`);
    ok("输入框也被清空了", (await page.inputValue("#l-q")) === "");

    // 与「克隆方式」筛选叠加
    await page.click('#l-filter [data-mode="icl"]');
    await page.waitForTimeout(200);
    await typeQ("甲乙");
    const combo = await namesNow();
    ok("叠加：完整 ICL + 「甲乙」→ 一个都没有（甲乙是仅向量）",
       combo.length === 0, combo.join("、") || "（空）");
    await page.click('#l-filter [data-mode="all"]');
    await page.waitForTimeout(200);

    // Esc 清空
    await typeQ("甲乙");
    await page.focus("#l-q");
    await page.keyboard.press("Escape");
    await page.waitForTimeout(220);
    ok("Esc 清空搜索框", (await page.inputValue("#l-q")) === "", await page.inputValue("#l-q"));
    ok("Esc 之后列表也回到全部", (await namesNow()).length === total);

    await page.screenshot({ path: OUT });

    // ======================= 3. 「选音色」＝ 按钮 + 弹窗 =======================
    // 用户提的：音色库会一直长，把整个库罗列在页面上早晚变成一堵墙。
    console.log("[3] 选音色的按钮 + 弹窗");
    await goto("#script");
    await page.waitForSelector("#s-pick", { timeout: 15000 });

    // 页面上不该再有任何「音色清单」
    const laidOut = await page.evaluate(() => ({
      rolechips: document.querySelectorAll(".rolechip, #s-roles").length,
      listed: document.querySelectorAll(".vchip, .vrow").length,
    }));
    ok("剧本页不再把音色罗列出来", laidOut.rolechips === 0 && laidOut.listed === 0,
       `rolechip=${laidOut.rolechips} · 页面上的音色条目=${laidOut.listed}`);

    const setText = (v, caret) => page.evaluate(([val, c]) => {
      const ta = document.querySelector("#s-text");
      ta.value = val;
      ta.focus();
      ta.setSelectionRange(c, c);
    }, [v, caret]);

    // 打开弹窗，搜出来再选 —— 顺便把搜索这条路也走一遍
    const openPicker = async (sel = "#s-pick") => {
      await page.click(sel);
      await page.waitForSelector("dialog.modal .vrow", { timeout: 8000 });
    };

    // 基数从接口现取，别写死 —— 用户随时会往库里加音色
    const libTotal = (await jget("/api/voices")).voices.length;
    await setText("", 0);
    await openPicker();
    const allRows = await page.$$eval("dialog.modal .vrow", (els) => els.length);
    ok("弹窗列出了库里全部音色", allRows === libTotal, `${allRows} 条 / 库里 ${libTotal} 个`);
    const pmText = (await page.textContent("dialog.modal #__pn"))?.trim() || "";
    ok("顶部报出总数", pmText.includes(`共 ${libTotal} 个`), pmText);
    ok("弹窗里有搜索框", await page.$("dialog.modal #__pq") !== null);
    ok("弹窗里有克隆方式筛选", await page.$$eval("dialog.modal [data-mode]", (e) => e.length) === 3);

    await page.fill("dialog.modal #__pq", src);
    await page.waitForTimeout(250);
    const filtered = await page.$$eval("dialog.modal .vrow", (els) => els.map((e) => e.dataset.name));
    ok("搜索能筛，且要的那个排在最前", filtered[0] === src && filtered.length < allRows,
       `筛出 ${filtered.length} / ${allRows}：${filtered.slice(0, 3).join("、")}`);

    await page.keyboard.press("Enter");        // 回车选中第一条
    await page.waitForTimeout(300);
    let val = await page.inputValue("#s-text");
    ok("回车就选中最靠上的那条", val === `[${src}] `, JSON.stringify(val));
    ok("选完弹窗自己关掉", await page.$("dialog.modal") === null);

    // 光标停在行中间 → 另起一行再插
    await setText("先说一句", 4);
    await openPicker();
    await page.fill("dialog.modal #__pq", src);
    await page.waitForTimeout(250);
    await page.click(`dialog.modal .vrow[data-name="${src}"]`);
    await page.waitForTimeout(300);
    val = await page.inputValue("#s-text");
    ok("光标在行中间 → 另起一行再插（不会把那一行切两半）",
       val === `先说一句\n[${src}] `, JSON.stringify(val));

    // 插入的内容要落进草稿（程序赋值不派发 input，必须自己存）
    const draft = await page.evaluate(() => {
      try { return JSON.parse(localStorage.getItem("qs-draft") || "{}")["#s-text"]; }
      catch (_) { return null; }
    });
    ok("插入的内容进了草稿", draft === val, JSON.stringify(draft));

    // 取消不该插进去任何东西
    const beforeCancel = await page.inputValue("#s-text");
    await openPicker();
    await page.click("dialog.modal [data-role=no]");
    await page.waitForTimeout(300);
    ok("点「取消」什么都不插", (await page.inputValue("#s-text")) === beforeCancel);
    await openPicker();
    await page.keyboard.press("Escape");
    await page.waitForTimeout(300);
    ok("按 Esc 关掉也不插", (await page.inputValue("#s-text")) === beforeCancel);
    ok("两次取消后弹窗都关掉了", await page.$("dialog.modal") === null);

    // 解析反馈并进了输入框：**框里标红说「哪一行」，下面说明「为什么」**。
    // （上一轮是「上面一条注意把问题念一遍、下面红行又写一遍」，那才叫重复。）
    // 解析是防抖后再走接口，不能靠固定 sleep 猜它什么时候回来：
    // 机器一忙，700ms 可能只够发出请求。等到「框里出现红行、统计已更新」
    // 这个真实状态再断言。
    await page.waitForFunction(() => {
      const stats = document.querySelector("#s-stats");
      return document.querySelectorAll("#s-hl .hl-line.is-bad").length === 2
        && stats && !stats.classList.contains("hidden")
        && stats.textContent.includes("可合成");
    }, { timeout: 6000 });
    const pv = await page.evaluate(() => ({
      bad: [...document.querySelectorAll("#s-hl .hl-line.is-bad")].map((e) => e.textContent.trim()),
      issues: [...document.querySelectorAll("#s-issues .note")].map((e) => e.textContent.trim()),
      stats: document.querySelector("#s-stats")?.textContent?.replace(/\s+/g, "") || "",
    }));
    ok("有问题的两行在框里标了红", pv.bad.length === 2, pv.bad.join(" | ").slice(0, 60));
    ok("下面的说明只说「为什么」，不把原句再抄一遍",
       pv.issues.length === 1 && pv.issues[0].includes("第 1 行")
       && !pv.issues[0].includes(pv.bad[0] || "\u0000"),
       pv.issues[0]?.slice(0, 60));
    ok("统计也在同一张卡里", /行2/.test(pv.stats) && pv.stats.includes("可合成0"), pv.stats.slice(0, 50));
    await page.screenshot({ path: OUT.replace(".png", "_script_bad.png") });

    await page.fill("#s-text", `[${src}] 这一行格式没问题。`);
    await page.waitForFunction(() => {
      const note = document.querySelector("#s-issues .note");
      return note && note.textContent.includes("每一行都能解析");
    }, { timeout: 6000 });
    const okNote = await page.textContent("#s-issues .note").catch(() => "");
    ok("全都解析得出时给一句肯定", (okNote || "").includes("每一行都能解析"), (okNote || "").trim().slice(0, 40));

    await page.screenshot({ path: OUT.replace(".png", "_script.png") });

    // ======================= 3.5 存音色的两条路 =======================
    console.log("[3.5] 存音色：撞名问覆盖 / 全新名字直接存");
    // 保存音色默认会顺手排一个「抽音色基因」的后台任务，那要加载模型，
    // 而验证实例和用户自己那台共用一块 6 GB 显存 —— 塞不下第二个模型实例。
    // 这里把请求体里的 prompt 改成 false 再放行：名称唯一这条逻辑跟 prompt 无关，
    // 测到的仍然是真后端、真状态码、真错误文案。
    await page.route("**/api/voices", async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      let body = {};
      try { body = JSON.parse(route.request().postData() || "{}"); } catch (_) {}
      body.prompt = false;
      return route.continue({ postData: JSON.stringify(body) });
    });

    await goto("#clone");
    await page.waitForSelector("#c-pick", { timeout: 15000 });

    // 克隆页：页面上只留一个按钮，音色清单搬进弹窗
    const cloneLayout = await page.evaluate(() => ({
      listed: document.querySelectorAll("#c-pane-lib .vchip, #c-pane-lib .vrow").length,
      label: document.querySelector("#c-pick .pickbtn__v")?.textContent || "",
      empty: document.querySelector("#c-pick .pickbtn__v")?.classList.contains("is-empty"),
    }));
    ok("克隆页也不再罗列音色", cloneLayout.listed === 0, `${cloneLayout.listed} 条`);
    ok("按钮上显示当前选中的音色（一开始是空的）",
       cloneLayout.empty === true && cloneLayout.label.includes("选一个"), cloneLayout.label);

    await page.click("#c-pick");
    await page.waitForSelector("dialog.modal .vrow", { timeout: 8000 });
    await page.click(`dialog.modal .vrow[data-name="${src}"]`);
    await page.waitForTimeout(300);
    const picked = await page.evaluate(() => ({
      label: document.querySelector("#c-pick .pickbtn__v")?.textContent || "",
      empty: document.querySelector("#c-pick .pickbtn__v")?.classList.contains("is-empty"),
      detail: document.querySelector("#c-lib-detail")?.textContent || "",
    }));
    ok("选完按钮上就换成那个音色名", picked.label === src && picked.empty === false, picked.label);
    ok("下面的详情跟着这个音色走", picked.detail.includes("克隆方式"), picked.detail.slice(0, 40));

    // 重新打开时，当前那个音色要被标出来
    await page.click("#c-pick");
    await page.waitForSelector("dialog.modal .vrow", { timeout: 8000 });
    ok("弹窗里标出了「当前」那一个",
       (await page.$$eval("dialog.modal .vrow.is-on", (e) => e.length)) === 1
       && (await page.textContent("dialog.modal .vrow.is-on .vrow__n"))?.trim() === src);
    await page.keyboard.press("Escape");
    await page.waitForTimeout(200);

    await page.click("#c-tab-up");    // 「存进音色库」在临时上传那一栏里，默认那栏是收起的
    await page.setInputFiles("#c-file", WAV);
    await page.waitForFunction(() => !document.querySelector("#c-save").disabled, null, { timeout: 60000 });
    // 参考文本是**必填**项（默认走完整 ICL，没它保存会被入口直接拒）。
    // 不填的话第一次保存就只弹一句红字，根本走不到「撞名确认框」那一步 ——
    // 而这一节要测的正是撞名流程，所以先给一段占位文本。
    await page.fill("#c-ref-text", "验证用参考文本");
    ok("参考音频就绪、保存按钮可用", true);

    const saveAs = async (name) => {
      // 存成功之后界面会自己切回「音色库」那一栏（上传栏随之收起），
      // 所以每次都要先把栏切回来 —— 上传状态本身不会因为切栏丢掉。
      await page.click("#c-tab-up");
      await page.waitForSelector("#c-save:visible", { timeout: 8000 });
      await page.click("#c-save");
      await page.waitForSelector("dialog.modal #__nm", { timeout: 10000 });
      await page.fill("dialog.modal #__nm", name);
      await page.click("dialog.modal [data-role=yes]");
    };
    const libNames = async () => (await jget("/api/voices")).voices.map((v) => v.name);
    const before = (await libNames()).length;

    // (a) 撞名 → 取消：不该静默改名，也不该存进去
    await saveAs(V1);
    await page.waitForSelector("dialog.modal .modal__head h2", { timeout: 10000 });
    const dupTitle = (await page.textContent("dialog.modal .modal__head h2")) || "";
    ok("撞名时弹确认框，而不是只丢一句红字", dupTitle.includes("同名音色已存在"), dupTitle);
    await page.click("dialog.modal [data-role=no]");
    await page.waitForTimeout(700);
    const toastNo = (await page.textContent("#toasts")) || "";
    ok("选「取消」→ 说明已存在、没有保存", toastNo.includes("已存在"), toastNo.trim().slice(0, 50));
    const afterNo = await libNames();
    ok("取消后音色数量没变（没被悄悄存成 xxx_2）",
       afterNo.length === before && !afterNo.some((n) => n.startsWith(V1 + "_")),
       `${before} → ${afterNo.length}`);

    // (b) 撞名 → 覆盖：还是只有一个同名音色
    await saveAs(V1);
    await page.waitForSelector("dialog.modal .modal__head h2", { timeout: 10000 });
    await page.click("dialog.modal [data-role=yes]");
    await page.waitForTimeout(1600);
    const toastYes = (await page.textContent("#toasts")) || "";
    ok("选「覆盖」→ 提示确实是覆盖", toastYes.includes("覆盖"), toastYes.trim().slice(0, 50));
    const afterYes = await libNames();
    ok("覆盖后同名音色仍然只有一个",
       afterYes.filter((n) => n === V1).length === 1 && !afterYes.some((n) => n.startsWith(V1 + "_"))
         && afterYes.length === before,
       afterYes.filter((n) => n.startsWith(V1)).join("、"));

    // (c) 全新名字：这条路上不该插任何对话框
    await saveAs(V3);
    await page.waitForTimeout(1600);
    ok("全新名字直接存下来（这次改动没把正常保存弄坏）",
       (await libNames()).includes(V3), (await libNames()).filter((n) => n.startsWith("__验证")).join("、"));

    // ======================= 4. 合成记录正文的持久详情 + 悬停预览 =======================
    console.log("[4] 合成记录正文：持久详情 + 悬停预览");
    await goto("#history");
    await page.waitForSelector(`.hrow[data-id="${RID_LONG}"]`, { timeout: 15000 });

    const clip = await page.evaluate((id) => {
      const p = document.querySelector(`.hrow[data-id="${id}"] .hrow__text`);
      const st = getComputedStyle(p);
      return { clamp: st.webkitLineClamp || st.getPropertyValue("-webkit-line-clamp"), sh: p.scrollHeight, ch: p.clientHeight };
    }, RID_LONG);
    ok("长正文被截成两行", String(clip.clamp) === "2" && clip.sh > clip.ch + 1,
       `line-clamp=${clip.clamp} · ${clip.sh} > ${clip.ch}`);

    const box = await page.evaluate((id) => {
      const r = document.querySelector(`.hrow[data-id="${id}"] .hrow__text`).getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    }, RID_LONG);
    await page.mouse.move(box.x, box.y);
    await page.waitForSelector(".tip.is-on", { timeout: 4000 }).catch(() => {});
    const tip = await page.evaluate((id) => {
      const t = document.querySelector(".tip");
      if (!t) return null;
      const rect = (el) => {
        const r = el.getBoundingClientRect();
        return { l: r.left, t: r.top, r: r.right, b: r.bottom, w: r.width, h: r.height };
      };
      const row = document.querySelector(`.hrow[data-id="${id}"]`);
      // 量**按钮本身**的并集，不是 .hrow__acts 那个容器 ——
      // 它是 display:flex 的块级元素，横向撑满整行，「浮层在它右边」永远不成立。
      const btns = [...row.querySelectorAll(".hrow__acts .btn")].map(rect);
      const acts = {
        l: Math.min(...btns.map((b) => b.l)), t: Math.min(...btns.map((b) => b.t)),
        r: Math.max(...btns.map((b) => b.r)), b: Math.max(...btns.map((b) => b.b)),
      };
      return {
        on: t.classList.contains("is-on"), text: t.textContent,
        pe: getComputedStyle(t).pointerEvents,
        userSelect: getComputedStyle(t).userSelect,
        btnCount: btns.length,
        box: rect(t), row: rect(row), acts,
        inView: (() => { const r = t.getBoundingClientRect();
          return r.left >= 0 && r.top >= 0 && r.bottom <= innerHeight + 1 && r.right <= innerWidth + 1; })(),
      };
    }, RID_LONG);
    ok("悬停后短预览出现", !!tip && tip.on === true);
    ok("浮层里是完整原文", !!tip && tip.text === LONG,
       tip ? `${tip.text.length} 字 vs 期望 ${LONG.length} 字` : "（没有浮层）");
    ok("浮层落在视口里", !!tip && tip.inView);

    // 浮层只做短预览，但必须能接鼠标事件，才能移进去滚长文；
    // 真正要选中、复制时走持久详情。
    ok("浮层可接鼠标事件（能滚长文）", !!tip && tip.pe === "auto", tip?.pe);
    ok("浮层本身不承担选中复制", !!tip && tip.userSelect === "none", tip?.userSelect);
    ok("浮层在操作按钮的右侧，没铺在左边",
       !!tip && tip.btnCount > 0 && tip.box.l >= tip.acts.r,
       `浮层左沿 ${Math.round(tip?.box.l)} · ${tip?.btnCount} 个按钮的右沿 ${Math.round(tip?.acts.r)}`);
    const overlap = !!tip && !(tip.box.r <= tip.acts.l || tip.box.l >= tip.acts.r
      || tip.box.b <= tip.acts.t || tip.box.t >= tip.acts.b);
    ok("浮层不和操作按钮的方块相交", !!tip && !overlap,
       overlap ? `浮层 ${Math.round(tip.box.l)}–${Math.round(tip.box.r)} × `
         + `${Math.round(tip.box.t)}–${Math.round(tip.box.b)}，按钮 ${Math.round(tip.acts.l)}–`
         + `${Math.round(tip.acts.r)} × ${Math.round(tip.acts.t)}–${Math.round(tip.acts.b)}` : "");
    if (tip?.on) await page.screenshot({ path: OUT.replace(".png", "_tip.png") });

    // 鼠标移进浮层后不能立刻消失，否则用户没机会滚动查看。
    if (tip?.on) {
      await page.mouse.move(tip.box.l + 16, tip.box.t + 16);
      await page.waitForTimeout(350);
      ok("鼠标移入浮层后不消失",
         await page.evaluate(() => !!document.querySelector(".tip.is-on")));
    }

    await page.mouse.move(8, 8);
    await page.waitForTimeout(500);
    ok("移开鼠标后短预览收起",
       await page.evaluate(() => !document.querySelector(".tip")?.classList.contains("is-on")));

    // 短到一行放得下的不弹预览
    const sbox = await page.evaluate((id) => {
      const r = document.querySelector(`.hrow[data-id="${id}"] .hrow__text`).getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    }, RID_SHORT);
    await page.mouse.move(sbox.x, sbox.y);
    await page.waitForTimeout(400);
    ok("短正文（一行放得下）不弹浮层",
       await page.evaluate(() => !document.querySelector(".tip")?.classList.contains("is-on")));
    await page.mouse.move(8, 8);

    // ---- 持久详情：点击正文打开，正文可选中，复制入口齐全 ----
    const textBtn = `.hrow[data-id="${RID_LONG}"] [data-role=text]`;
    ok("正文是键盘可聚焦的按钮",
       await page.$eval(textBtn, (el) =>
         el.tagName === "BUTTON" && el.tabIndex >= 0
         && el.getAttribute("aria-label")?.includes("完整正文")));
    ok("记录行有显式「查看全文」入口",
       await page.$(`.hrow[data-id="${RID_LONG}"] [data-role=detail]`) !== null);

    await page.click(textBtn);
    await page.waitForSelector("dialog.modal .histdetail__text", { timeout: 4000 });
    await page.waitForTimeout(120);
    const modal = await page.evaluate((longText) => {
      const d = document.querySelector("dialog.modal");
      const t = d?.querySelector(".histdetail__text");
      if (!d || !t) return null;
      const st = getComputedStyle(t);
      return {
        open: d.open,
        text: t.textContent,
        userSelect: st.userSelect,
        overscroll: st.overscrollBehavior,
        whiteSpace: st.whiteSpace,
        copyAll: !!d.querySelector('[data-role=copy-all]'),
        copyParams: !!d.querySelector('[data-role=copy-params]'),
        focused: document.activeElement === t,
        matches: t.textContent === longText,
      };
    }, LONG);
    ok("点击正文打开持久详情", !!modal && modal.open === true);
    ok("详情里是完整原文", !!modal && modal.matches,
       modal ? `${modal.text.length} 字 vs 期望 ${LONG.length} 字` : "（没有详情）");
    ok("详情正文可选择", !!modal && modal.userSelect === "text", modal?.userSelect);
    ok("详情正文的滚动不向页面穿透",
       !!modal && ["contain", "none"].includes(modal.overscroll), modal?.overscroll);
    ok("详情保留换行", !!modal && modal.whiteSpace === "pre-wrap", modal?.whiteSpace);
    ok("详情有「复制全文」", !!modal && modal.copyAll);
    ok("详情有「复制文本参数」", !!modal && modal.copyParams);
    ok("详情打开后焦点在正文上", !!modal && modal.focused);

    const selected = await page.evaluate(() => {
      const t = document.querySelector("dialog.modal .histdetail__text");
      if (!t) return "";
      const range = document.createRange();
      range.selectNodeContents(t);
      const sel = getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      return sel.toString();
    });
    ok("详情里的全文可以被选中", selected === LONG,
       `${selected.length} 字 vs 期望 ${LONG.length} 字`);

    // 滚轮滚正文时，详情不能自己消失；页面列表也不能被带着滚。
    const scrollBefore = await page.evaluate(() => ({
      body: document.scrollingElement.scrollTop,
      detail: document.querySelector("dialog.modal .histdetail__text").scrollTop,
    }));
    const detailBox = await page.evaluate(() => {
      const r = document.querySelector("dialog.modal .histdetail__text").getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    });
    await page.mouse.move(detailBox.x, detailBox.y);
    await page.mouse.wheel(0, 600);
    await page.waitForTimeout(250);
    const scrollAfter = await page.evaluate(() => ({
      open: !!document.querySelector("dialog.modal")?.open,
      body: document.scrollingElement.scrollTop,
      detail: document.querySelector("dialog.modal .histdetail__text").scrollTop,
    }));
    ok("滚动详情正文时弹窗保持打开", scrollAfter.open === true);
    ok("滚动详情正文不会带动页面列表",
       scrollAfter.body === scrollBefore.body,
       `${scrollBefore.body} → ${scrollAfter.body}`);

    await page.keyboard.press("Escape");
    await page.waitForTimeout(250);
    ok("Esc 关闭持久详情", await page.$("dialog.modal") === null);
    ok("Esc 关闭后焦点回到正文按钮",
       await page.evaluate((sel) => document.activeElement === document.querySelector(sel), textBtn));

    ok("全程无 JS 报错", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | "));
  } finally {
    // ---- 收尾：临时记录原样放回，临时音色删掉 ----
    try { fs.writeFileSync(HIST, histOrig, "utf8"); } catch (_) {}
    // 删三次、每次隔一小会儿：Windows 上文件被占着就删不掉
    //（页面上刚试听过某个音色时，/media/voice 的 FileResponse 还开着那个 wav），
    // 一次删不掉会让「临时音色没清干净」变成随机红 —— 不是代码坏了，是文件锁。
    for (let i = 0; i < 3; i++) {
      try { await jpost("/api/voices/delete", { names: [V1, V2, V3] }); } catch (_) {}
      const l = (await jget("/api/voices")).voices.map((v) => v.name);
      if (!l.some((n) => [V1, V2, V3].includes(n))) break;
      await page.waitForTimeout(600);
    }
    const left = (await jget("/api/voices")).voices.map((v) => v.name);
    const ghost = left.filter((n) => [V1, V2, V3].includes(n) || n.startsWith(V1 + "_"));
    console.log("[5] 已还原 history.json、删掉临时音色");
    if (ghost.length) { console.log("   残留音色：" + ghost.join("、")); fails.push("临时音色没清干净"); }
    await browser.close();
  }

  console.log("\n" + "=".repeat(60));
  if (fails.length) {
    console.log(`${fails.length} 项未通过：`);
    fails.forEach((f) => console.log("   - " + f));
    process.exit(1);
  }
  console.log("全部通过");
  console.log("截图：" + OUT + " / " + OUT.replace(".png", "_script.png"));
})();
