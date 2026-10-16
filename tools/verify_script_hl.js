/* 一次性验证脚本：剧本框的行内高亮（解析预览并进输入框）。

   要验四件事：
   1. 高亮层与 textarea **逐字符一致**（只在原文外面多套了 <span>），
      —— 这是「叠层」方案成立的底线，差一个字就会看到重影
   2. 两层的**排版逐行对齐**：每一行的 y、整个内容的高度、滚动位置
   3. 颜色分得对：角色＝强调色、情感指令＝警示色、台词＝正文色；
      解析不过的行刷红底，而「音色不在库里」**不**刷（那是另一层问题）
   4. 预览并进来了：`#s-preview` 那张卡没有了，统计与问题说明就在框下面；
      打字时高亮层**立刻**跟上（不等那次 240ms 防抖）

   只读：全程只在文本框里打字，不点生成、不写任何文件。

   跑法（在项目根目录）：
     NODE_PATH=<...>/node_modules node tools/verify_script_hl.js
   端口默认 7867，可用 QWENSTUDIO_PORT 覆盖。
*/
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:" + (process.env.QWENSTUDIO_PORT || "7867");
const OUT = "tools/_script_hl.png";

// 第 2 行故意写坏（没有 [角色名]），第 3 行音色不在库里（格式是对的，不该标红），
// 第 4 行故意写长到必须折行，第 5 行带行首缩进，第 6 行行尾带空格，第 7 行空着。
const SCRIPT = [
  "[韩立][压低了声音] 此丹的药力，恐怕不止于此。",
  "先说一句",
  "[不存在的音色] 这一行格式是对的，只是音色不在库里。",
  "[韩立][语气很重，几乎是一字一顿] 这一段要足够长，长到在这宽度的框里必须折成两行以上，"
    + "才能验出折行的行高两层是不是算得一样。",
  "   [韩立] 行首三个空格，照样能解析出来。",
  "[南宫婉] 末尾带一个空格 ",
  "",
  "[南宫婉][轻声] 那便等三日。",
].join("\n");

// 逐行 y 的对齐另用一段**不折行**的文本量 —— 折了行之后，
// 「第 i 行的 y = 顶边 + 内边距 + i×行高」这个式子本身就不成立了
//（折行的那一行要占两行的高度），拿它当基准只会测出假失败。
const SHORT = [
  "[韩立][压低了声音] 第一行。",
  "[南宫婉] 第二行。",
  "",
  "[韩立] 第四行（前面空了一行）。",
  "[南宫婉][轻声] 第五行。",
  "[韩立] 第六行。",
].join("\n");

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
  await cdp.send("DOM.enable");
  await cdp.send("CSS.enable");

  // 问浏览器「这个节点实际用的是哪个字体文件」。粗体必须是**真 Bold 字面**
  // （雅黑 Bold / 苹方 Semibold），不能是合成粗体 —— 合成粗体是把笔画描粗一圈，
  // 中文会糊。光看 computed 的 font-weight 分辨不出来，得问平台字体。
  const usedFonts = async (selector) => {
    const { root } = await cdp.send("DOM.getDocument", { depth: -1 });
    const { nodeId } = await cdp.send("DOM.querySelector", { nodeId: root.nodeId, selector });
    const { fonts } = await cdp.send("CSS.getPlatformFontsForNode", { nodeId });
    return (fonts || []).map((f) => ({ name: f.postScriptName || f.familyName, glyphs: f.glyphCount }));
  };

  await page.goto("about:blank");
  await page.goto(BASE + "/#script", { waitUntil: "networkidle" });
  await page.waitForSelector("#s-hl", { timeout: 15000 });

  console.log("[1] 解析预览已经并进输入框");
  ok("没有 #s-preview 那张卡了", await page.$("#s-preview") === null);
  ok("文本为空时统计块是收起的", await page.$eval("#s-stats", (el) => el.classList.contains("hidden")));
  const sameCard = await page.evaluate(() => {
    const box = document.querySelector("#s-box");
    const card = box.closest(".card");
    return !!card && card.contains(document.querySelector("#s-stats"))
      && card.contains(document.querySelector("#s-issues"));
  });
  ok("统计与问题说明都在输入框那张卡里", sameCard);

  await page.fill("#s-text", SCRIPT);
  await page.waitForTimeout(700);            // 等那次防抖 + 接口回来
  ok("有内容之后统计块出现", await page.$eval("#s-stats", (el) => !el.classList.contains("hidden")));
  const statsText = (await page.textContent("#s-stats"))?.replace(/\s+/g, " ") || "";
  ok("统计项齐全", ["行", "可合成", "角色", "带情感", "字数"].every((k) => statsText.includes(k)),
     statsText.trim());
  const issueText = (await page.textContent("#s-issues"))?.replace(/\s+/g, " ") || "";
  // 框里用一个红底回答「哪一行」，这里回答「为什么」—— 两边说的不是同一件事，不算重复
  ok("说清了第 2 行为什么不行", issueText.includes("第 2 行") && issueText.includes("缺少"),
     issueText.slice(0, 70));
  ok("另有一条提示音色不在库里（跟第 2 行那条分开）", issueText.includes("不在库里"),
     issueText.slice(-60));

  console.log("[2] 高亮层与正文逐字符一致");
  const fidelity = await page.evaluate(() => {
    const ta = document.querySelector("#s-text");
    const hl = document.querySelector("#s-hl");
    const lineDivs = [...hl.querySelectorAll(".hl-line")];
    return {
      hlText: hl.textContent,
      taText: ta.value,
      lines: ta.value.split("\n").length,
      divs: lineDivs.length,
      // 每行的纯文本也要对得上（把 span 拆掉之后）
      perLine: lineDivs.map((d) => d.textContent),
    };
  });
  // 注意是**按行拼**再比：textContent 会把块级元素之间的换行吃掉，
  // 直接比 textContent 永远少几个字符，那是个假失败
  ok("整段文本一致（只多了 span，没多字没少字）",
     fidelity.perLine.join("\n") === fidelity.taText,
     fidelity.perLine.join("\n") === fidelity.taText ? `${fidelity.taText.length} 字`
       : `高亮 ${fidelity.perLine.join("\n").length} / 正文 ${fidelity.taText.length}`);
  ok("行数一一对应（含末尾的空行）",
     fidelity.divs === fidelity.lines, `${fidelity.divs} 层 / ${fidelity.lines} 行`);
  const srcLines = SCRIPT.split("\n");
  ok("逐行也对得上（空行、行首缩进、行尾空格都原样）",
     srcLines.every((l, i) => fidelity.perLine[i] === l),
     srcLines.map((l, i) => (fidelity.perLine[i] === l ? "" : `第${i + 1}行`)).filter(Boolean).join("、"));

  console.log("[3] 两层的排版逐行对齐");
  const measure = () => page.evaluate(() => {
    const ta = document.querySelector("#s-text");
    const hl = document.querySelector("#s-hl");
    const cs = getComputedStyle(ta);
    const padTop = parseFloat(cs.paddingTop);
    const lineH = parseFloat(cs.lineHeight);
    const taTop = ta.getBoundingClientRect().top;
    const got = [...hl.querySelectorAll(".hl-line")]
      .map((d) => +d.getBoundingClientRect().top.toFixed(1));
    // 不折行的前提下，第 i 行的 y = 顶边 + 上内边距 + i×行高
    const want = got.map((_, i) => +(taTop + padTop + i * lineH).toFixed(1));
    return {
      want, got, lineH, padTop,
      hlH: hl.scrollHeight, taH: ta.scrollHeight,
      sameFont: getComputedStyle(hl).font === cs.font,
      samePad: getComputedStyle(hl).padding === cs.padding,
      sameWs: getComputedStyle(hl).whiteSpace === cs.whiteSpace,
    };
  });

  await page.fill("#s-text", SHORT);
  await page.waitForTimeout(600);
  const g1 = await measure();
  const off = g1.want.map((w, i) => +Math.abs(w - g1.got[i]).toFixed(1));
  const worst = Math.max(...off);
  console.log(`    不折行：行高 ${g1.lineH}px · 上内边距 ${g1.padTop}px · 各行 y 最大偏差 ${worst}px`);
  ok("每一行都落在同一 y 上（≤1px）", worst <= 1,
     off.map((d, i) => (d > 1 ? `第${i + 1}行偏 ${d}px` : "")).filter(Boolean).join("、"));
  ok("空行也占了一行的高度（textarea 里空行是占位的）",
     Math.abs((g1.got[3] - g1.got[2]) - g1.lineH) <= 1,
     `第 2→4 行间距 ${(g1.got[3] - g1.got[2]).toFixed(1)} vs 行高 ${g1.lineH}`);

  await page.fill("#s-text", SCRIPT);
  await page.waitForTimeout(600);
  const g2 = await measure();
  ok("有折行时两层的内容总高仍然一致", Math.abs(g2.hlH - g2.taH) <= 1,
     `高亮层 ${g2.hlH} / 正文 ${g2.taH}`);
  ok("字体、内边距、换行规则三项一致",
     g2.sameFont && g2.samePad && g2.sameWs,
     `font=${g2.sameFont} padding=${g2.samePad} white-space=${g2.sameWs}`);

  // 用户定的是「只染字色、不加底色」+「字号跟别的文本框对齐」。
  // 染字色就得盯对比度：直接拿 --accent / --warn（分别只有 4.5:1 / 4.65:1）
  // 当 14px 汉字的前景色会发虚，所以用的是各自压深一档的 --ink-*（约 6.6:1）。
  console.log("[4] 配色与字号");
  const col = await page.evaluate(() => {
    const probe = (v, prop) => {
      const s = document.createElement("span");
      s.style[prop] = `var(${v})`;
      document.body.appendChild(s);
      const c = getComputedStyle(s)[prop === "color" ? "color" : "backgroundColor"];
      s.remove();
      return c;
    };
    const row = document.querySelector("#s-hl .hl-line");
    const role = row.querySelector(".hl-role");
    const inst = row.querySelector(".hl-inst");
    const hl = document.querySelector("#s-hl");
    const ta = document.querySelector("#s-text");
    const csTa = getComputedStyle(ta);
    // 页面上别的文本框（另一个视图里的也行，computed style 一样拿得到）当基准
    const other = document.querySelector(".textarea");
    return {
      roleFg: getComputedStyle(role).color, roleBg: getComputedStyle(role).backgroundColor,
      instFg: getComputedStyle(inst).color, instBg: getComputedStyle(inst).backgroundColor,
      bodyFg: getComputedStyle(hl).color, surface: probe("--surface", "background"),
      accent: probe("--accent", "color"), warn: probe("--warn", "color"),
      inkRole: probe("--ink-role", "color"), inkInst: probe("--ink-inst", "color"),
      roleWeight: getComputedStyle(role).fontWeight,
      instWeight: getComputedStyle(inst).fontWeight,
      size: csTa.fontSize, family: csTa.fontFamily, lineH: csTa.lineHeight,
      otherSize: other ? getComputedStyle(other).fontSize : "",
      roleText: role.textContent, instText: inst.textContent,
    };
  });
  const lum = (rgb) => {
    const m = (rgb.match(/[\d.]+/g) || []).slice(0, 3).map(Number)
      .map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * m[0] + 0.7152 * m[1] + 0.114 * m[2];
  };
  const ratio = (a, b) => {
    const l1 = lum(a), l2 = lum(b);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  };
  const rRole = ratio(col.roleFg, col.surface);
  const rInst = ratio(col.instFg, col.surface);
  const rBody = ratio(col.bodyFg, col.surface);
  console.log(`    角色 ${col.roleText} ${col.roleFg} = ${rRole.toFixed(2)}:1`
    + ` · 情感 ${col.instText} ${col.instFg} = ${rInst.toFixed(2)}:1`
    + ` · 台词 = ${rBody.toFixed(2)}:1`);
  ok("角色对输入框底色 ≥ 6:1（不能用 --accent 那种刚过 4.5 的）", rRole >= 6,
     `${rRole.toFixed(2)}:1 · --accent 只有 ${ratio(col.accent, col.surface).toFixed(2)}:1`);
  ok("情感对输入框底色 ≥ 6:1", rInst >= 6,
     `${rInst.toFixed(2)}:1 · --warn 只有 ${ratio(col.warn, col.surface).toFixed(2)}:1`);
  ok("台词对输入框底色 ≥ 12:1（主体要最清楚）", rBody >= 12, `${rBody.toFixed(2)}:1`);
  ok("三种颜色互不相同（角色 ≠ 情感 ≠ 台词）",
     col.roleFg !== col.instFg && col.roleFg !== col.bodyFg && col.instFg !== col.bodyFg,
     `${col.roleFg} / ${col.instFg} / ${col.bodyFg}`);
  ok("**不刷底色**（用户要的是只染字色）",
     col.roleBg === "rgba(0, 0, 0, 0)" && col.instBg === "rgba(0, 0, 0, 0)",
     `${col.roleBg} / ${col.instBg}`);
  ok("两个标签都是粗体", col.roleWeight === "700" && col.instWeight === "700",
     `${col.roleWeight} / ${col.instWeight}`);

  const boldish = (n) => /bold|semibold|heavy|black/i.test(n);
  const nm = (list) => list.map((f) => f.name).join("、") || "（无）";
  const fRole = await usedFonts("#s-hl .hl-role");
  // 对照行必须挑**不含加粗子节点**的那一行 —— 这个接口会把子节点的字体一起算进来，
  // 直接问 .hl-line 会连里面那两个粗体标签的 Bold 字面一并返回（第一版就栽在这儿）。
  const plainSel = await page.evaluate(() => {
    const rows = [...document.querySelectorAll("#s-hl .hl-line")];
    const i = rows.findIndex((d) => !d.querySelector(".hl-role, .hl-inst"));
    return i >= 0 ? `#s-hl .hl-line:nth-of-type(${i + 1})` : "";
  });
  const fPlain = plainSel ? await usedFonts(plainSel) : [];
  console.log(`    加粗标签实际用字：${nm(fRole)}`);
  console.log(`    没加粗的行实际用字：${nm(fPlain)}`);
  // 合成粗体只会**复用常规字面**再描粗一圈，所以「实际用的是 Bold 字面」这一条
  // 就足以把合成粗体排除掉 —— 光看 computed 的 font-weight 分辨不出来。
  ok("粗体落在**真 Bold 字面**上（合成粗体会把中文描糊）",
     fRole.length > 0 && fRole.some((f) => boldish(f.name)), nm(fRole));
  ok("没加粗的行仍是常规字面（对照）",
     fPlain.length > 0 && fPlain.every((f) => !boldish(f.name)), nm(fPlain));

  // 用户的原话：「我觉得是文字小的问题」。剧本框原来是 12.5px 等宽，
  // 而页面上别的文本框是 14.5px —— 确实是全页最小的字。
  console.log(`    字号 ${col.size}（别的文本框 ${col.otherSize}）· 行高 ${col.lineH}`);
  ok("字号跟页面上别的文本框一致", col.size === col.otherSize, `${col.size} vs ${col.otherSize}`);
  ok("用的是正文那套无衬线字体，不再是等宽（等宽栈对中文没有字形）",
     !/mono/i.test(col.family), col.family.slice(0, 60));

  console.log("[5] 行状态");
  const bad = await page.evaluate(() => {
    const rows = [...document.querySelectorAll("#s-hl .hl-line")];
    return {
      badIdx: rows.map((d, i) => (d.classList.contains("is-bad") ? i + 1 : 0)).filter(Boolean),
      badText: rows.filter((d) => d.classList.contains("is-bad")).map((d) => d.textContent),
      errSoft: (() => {
        const s = document.createElement("div");
        s.style.background = "var(--err-soft)";
        document.body.appendChild(s);
        const c = getComputedStyle(s).backgroundColor;
        s.remove();
        return c;
      })(),
      badBg: (() => {
        const d = rows.find((x) => x.classList.contains("is-bad"));
        return d ? getComputedStyle(d).backgroundColor : "";
      })(),
      isBadBorder: (() => {
        const d = rows.find((x) => x.classList.contains("is-bad"));
        return d ? getComputedStyle(d).boxShadow : "";
      })(),
    };
  });
  ok("解析不过的那一行刷了红底", bad.badIdx.length === 1 && bad.badIdx[0] === 2,
     `标红第 ${bad.badIdx.join("、")} 行`);
  ok("红底用的是 --err-soft", bad.badBg === bad.errSoft, bad.badBg);
  ok("左边还压了一道红边（光靠底色在浅色主题下太淡）", /rgb\(/.test(bad.isBadBorder), bad.isBadBorder);
  ok("「音色不在库里」那一行**不**标红（那一行格式是对的）", bad.badIdx.length === 1,
     bad.badText.join(" | ").slice(0, 40));

  console.log("[6] 打字时高亮层立刻跟上（不等防抖）");
  await page.fill("#s-text", "[韩立][轻声] 立刻就该上色");
  const instant = await page.evaluate(() => {
    const hl = document.querySelector("#s-hl");
    return { text: hl.textContent, spans: hl.querySelectorAll(".hl-role, .hl-inst").length };
  });
  ok("填进去的瞬间两层就一致了", instant.text === "[韩立][轻声] 立刻就该上色", instant.text);
  ok("而且当场就上了色", instant.spans === 2, `${instant.spans} 个着色的括号`);

  console.log("[7] 滚动同步");
  await page.fill("#s-text", [...Array(40).keys()].map((i) => `[韩立] 第 ${i + 1} 行台词。`).join("\n"));
  await page.waitForTimeout(500);
  await page.evaluate(() => { document.querySelector("#s-text").scrollTop = 120; });
  await page.waitForTimeout(150);
  const sc = await page.evaluate(() => {
    const ta = document.querySelector("#s-text"), hl = document.querySelector("#s-hl");
    return { ta: ta.scrollTop, hl: hl.scrollTop, hlw: hl.getBoundingClientRect().width,
             taw: ta.clientWidth };
  });
  ok("滚起来两层一起走", sc.hl === sc.ta && sc.ta > 0, `正文 ${sc.ta} / 高亮 ${sc.hl}`);
  ok("高亮层宽度跟着正文的可写宽度（滚动条占掉的那部分要扣）",
     Math.abs(sc.hlw - sc.taw) <= 1, `${sc.hlw} vs ${sc.taw}`);

  console.log("[8] 输入法拼字期间把两层换过来");
  const comp = await page.evaluate(() => {
    const ta = document.querySelector("#s-text"), box = document.querySelector("#s-box");
    const fire = (t) => ta.dispatchEvent(new CompositionEvent(t, { bubbles: true }));
    fire("compositionstart");
    const on = { cls: box.classList.contains("is-composing"),
                 taColor: getComputedStyle(ta).color,
                 hlVis: getComputedStyle(document.querySelector("#s-hl")).visibility };
    fire("compositionend");
    const off = { cls: box.classList.contains("is-composing"),
                  taColor: getComputedStyle(ta).color };
    return { on, off, text: getComputedStyle(document.body).color };
  });
  ok("拼字时交出文字颜色（不然看不见自己在打什么）",
     comp.on.cls && comp.on.taColor !== "rgba(0, 0, 0, 0)", comp.on.taColor);
  ok("拼字时把高亮层收起来（否则两层叠着画成重影）", comp.on.hlVis === "hidden", comp.on.hlVis);
  ok("拼完换回去", !comp.off.cls && comp.off.taColor === "rgba(0, 0, 0, 0)", comp.off.taColor);

  await page.fill("#s-text", SCRIPT);
  await page.waitForTimeout(600);
  await page.locator("#view-script .card").first().screenshot({ path: OUT });

  // 高光底色是半透明的，浅色主题里合适、深色里未必 —— 两套都得量一遍
  console.log("[9] 换深色主题再量一次对比度");
  const darkM = await page.evaluate(() => {
    document.documentElement.dataset.theme = "dark";
    const probe = (v, prop) => {
      const s = document.createElement("span");
      s.style[prop] = `var(${v})`;
      document.body.appendChild(s);
      const c = getComputedStyle(s)[prop === "color" ? "color" : "backgroundColor"];
      s.remove();
      return c;
    };
    const row = document.querySelector("#s-hl .hl-line");
    return {
      roleFg: getComputedStyle(row.querySelector(".hl-role")).color,
      roleBg: getComputedStyle(row.querySelector(".hl-role")).backgroundColor,
      instFg: getComputedStyle(row.querySelector(".hl-inst")).color,
      instBg: getComputedStyle(row.querySelector(".hl-inst")).backgroundColor,
      bodyFg: getComputedStyle(document.querySelector("#s-hl")).color,
      surface: probe("--surface", "background"),
    };
  });
  const dRole = ratio(darkM.roleFg, darkM.surface);
  const dInst = ratio(darkM.instFg, darkM.surface);
  console.log(`    深色下：角色 ${dRole.toFixed(2)}:1 · 情感 ${dInst.toFixed(2)}:1`
    + ` · 台词 ${ratio(darkM.bodyFg, darkM.surface).toFixed(2)}:1`);
  ok("深色主题下角色 ≥ 6:1", dRole >= 6, `${dRole.toFixed(2)}:1`);
  ok("深色主题下情感 ≥ 6:1", dInst >= 6, `${dInst.toFixed(2)}:1`);

  ok("全程无 JS 报错", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | "));

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
