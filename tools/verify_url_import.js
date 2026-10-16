/* 一次性验证脚本：确认「新增音色 → 粘贴音频直链 → 拉取 → 继续裁剪 / 保存」这条链路。
   用 playwright-core 驱动本机 Chrome。

   拉取的源用的是**服务自己**的 /media/voice/御姐（一个真实 wav）——
   不依赖外网，却会真的走完「HTTP 下载 → 落盘 → 解码 → 回报时长」。

   **只读**：UI 那段最后点「取消」关掉弹窗，不写音色库。
   （会往 web/_uploads/ 留几个临时文件 —— 和界面上传的行为一致，那是临时目录。）

   跑法（在项目根目录）：
     NODE_PATH=<...>/node_modules node tools/verify_url_import.js
   端口默认 7867，可用 QWENSTUDIO_PORT 覆盖。
*/
const { chromium } = require("playwright-core");

const BASE = "http://127.0.0.1:" + (process.env.QWENSTUDIO_PORT || "7867");
const OUT = "tools/_url_import.png";
const WHO = "御姐";                                  // 拉取的源音色（真实 wav）
const SRC = BASE + "/media/voice/" + encodeURIComponent(WHO);

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true, args: ["--mute-audio"] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });

  const fails = [];
  const ok = (label, cond, detail = "") => {
    console.log(`  [${cond ? "OK  " : "FAIL"}] ${label}${detail ? "  → " + detail : ""}`);
    if (!cond) fails.push(label);
  };

  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => consoleErrors.push("pageerror: " + e.message));

  const api = (path, body) => page.evaluate(async ([p, b]) => {
    const r = await fetch(p, b === undefined ? undefined : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
    });
    return { status: r.status, data: await r.json().catch(() => null) };
  }, [path, body]);

  await page.goto(BASE + "/#library", { waitUntil: "networkidle" });
  await page.waitForSelector(".vitem", { timeout: 15000 });
  console.log("[1] 音色库已加载");

  // ---- 1. 文档对账：新接口必须在 api_docs 里登记过 ----
  const docs = (await api("/api/docs")).data;
  ok("页面加载无 JS 报错", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | "));
  ok("GET /api/docs 对账一致", docs?.audit?.consistent === true,
     `${docs?.audit?.declared} 声明 / ${docs?.audit?.real} 实际`);
  ok("  · 文档里声明了 POST /api/upload/url",
     (docs?.endpoints || []).some((e) => e.method === "POST" && e.path === "/api/upload/url"));

  // ---- 2. 正常路径：拉取 → 同一套字段 ----
  const truth = await page.evaluate(async (n) => {
    const d = await (await fetch("/api/voices")).json();
    return (d.voices || []).find((v) => v.name === n) || null;
  }, WHO);
  ok(`  音色库里能查到源音色「${WHO}」`, !!truth, truth ? `${truth.duration}s` : "没有");

  const r1 = await api("/api/upload/url", { url: SRC });
  const up = r1.data || {};
  ok("POST /api/upload/url 返回 ok", r1.status === 200 && up.ok === true, up.error || "");
  ok("  · 与 /api/upload 同字段（name/original/duration/url）",
     !!(up.name && up.original && up.duration > 0 && up.url), JSON.stringify(up).slice(0, 120));
  ok("  · 时长与源音频一致", Math.abs((up.duration || 0) - (truth?.duration || 0)) < 0.05,
     `${up.duration} vs ${truth?.duration}`);
  ok("  · 落盘在临时目录、可回放", /^\/media\/upload\//.test(up.url || ""), up.url || "");

  const riff = await page.evaluate(async (u) => {
    const b = await (await fetch(u)).arrayBuffer();
    return { n: b.byteLength, head: String.fromCharCode(...new Uint8Array(b.slice(0, 4))) };
  }, up.url);
  ok("  · 回放拿到的是真 wav", riff.head === "RIFF" && riff.n > 1000, `${riff.n} bytes, ${riff.head}`);
  ok("  · 回报了来源链接（界面据此标「来自链接」）", up.source === SRC);

  // ---- 3. 错误分支：消息要是给人看的中文 ----
  const bad = [
    ["file:// 被拒", { url: "file:///C:/Windows/win.ini" }, /http/],
    ["空链接被拒", { url: "   " }, /链接/],
    ["没写 scheme 被拒", { url: "example.com/a.mp3" }, /http/],
    ["404 有明确说明", { url: BASE + "/media/voice/nope" }, /404/],
    ["指向网页被拒", { url: BASE + "/" }, /网页/],
    ["连不上有明确说明", { url: "http://127.0.0.1:1/a.wav" }, /连不上|超时/],
  ];
  for (const [label, body, re] of bad) {
    const r = await api("/api/upload/url", body);
    const msg = (r.data && r.data.error) || "";
    ok(label, r.status === 400 && r.data?.ok === false && re.test(msg), msg.slice(0, 56));
  }

  // ---- 4. 界面：链接这一行真的挂上了，拉完接着走同一条下游 ----
  // 上面那六条是**故意**打 400 的，浏览器会把每个 4xx 都记一条 console error。
  // 清掉这层网络噪声再进界面段，末尾那条「无 JS 报错」才只反映真正的脚本异常。
  consoleErrors.length = 0;
  const before = (await api("/api/voices")).data.voices.length;
  await page.click("#l-new");
  await page.waitForSelector("dialog.modal #nv-url-go", { timeout: 8000 });
  ok("新增音色弹窗里有「粘贴音频直链」一行", true);

  const goDisabled = await page.isDisabled("dialog.modal [data-role=yes]");
  ok("  拉取前「保存音色」是禁用的", goDisabled);

  await page.fill("dialog.modal #nv-url", SRC);
  await page.click("dialog.modal #nv-url-go");
  console.log("[3] 已点「拉取」，等待下载 + 解码…");
  await page.waitForSelector("dialog.modal [data-role=yes]:not([disabled])", { timeout: 30000 });

  const dropT = ((await page.textContent("dialog.modal #nv-drop-t")) || "").replace(/\s+/g, " ");
  const autoName = await page.inputValue("dialog.modal #nv-name");
  const read = ((await page.textContent("dialog.modal #nv-editor [data-role=read]")) || "").trim();
  const hasCanvas = await page.$("dialog.modal #nv-editor canvas");
  console.log(`[4] 拖放区：${dropT.trim()}`);
  console.log(`    自动填的音色名：${autoName} / 编辑器读数：${read}`);

  ok("拉取后「保存音色」变可用", !(await page.isDisabled("dialog.modal [data-role=yes]")));
  ok("  拖放区标出了「来自链接」", /来自链接/.test(dropT));
  ok("  波形编辑器接到了这段音频", !!hasCanvas && read !== "—", read);
  ok("  音色名按链接末段自动填好", autoName.length > 0, autoName);

  await page.screenshot({ path: OUT });

  // 只读收尾：取消，不存音色
  await page.click("dialog.modal [data-role=no]");
  await page.waitForTimeout(300);
  ok("「取消」已关闭弹窗", !(await page.$("dialog.modal")));
  const after = (await api("/api/voices")).data.voices.length;
  ok("  没有写出任何音色（只读）", after === before, `${before} → ${after}`);

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
