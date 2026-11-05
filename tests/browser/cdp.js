// 사용: python tests/browser/serve_fake.py (다른 터미널) → node tests/browser/cdp.js <스크린샷 폴더>
// Chrome DevTools Protocol로 실제 브라우저에서 화면을 조작하고 스크린샷을 찍는다(외부 패키지 없음, Node 내장 WebSocket·fetch 사용)
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const OUT = process.argv[2], BASE = "http://127.0.0.1:8124";
const chrome = process.env.CHROME_PATH || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function target(port) {
  for (let i = 0; i < 50; i++) { try { const r = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" }); return await r.json(); } catch { await sleep(200); } }
  throw new Error("chrome 연결 실패");
}
function client(wsUrl) {
  const ws = new WebSocket(wsUrl); let id = 0; const pend = new Map();
  ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } };
  const ready = new Promise(r => ws.onopen = r);
  return { ready, send: (method, params = {}) => Promise.race([new Promise(res => { const i = ++id; pend.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); }),
    new Promise((_, rej) => setTimeout(() => rej(new Error("응답 없음: " + method)), 20000))]) };
}
async function tab(port) { const t = await target(port); const c = client(t.webSocketDebuggerUrl); await c.ready; await c.send("Page.enable"); return c; }
async function ev(c, expr) { const r = await c.send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true }); if (r.result.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 400)); return r.result.result.value; }
async function shot(c, name) { await c.send("Page.bringToFront"); await sleep(200); const r = await c.send("Page.captureScreenshot", { format: "png" }); fs.writeFileSync(path.join(OUT, name + ".png"), Buffer.from(r.result.data, "base64")); }
async function nav(c, url) { await c.send("Page.navigate", { url }); await sleep(1500); }
const clickText = (text, sel = "button") => `(() => { const b = [...document.querySelectorAll('${sel}')].find(x => x.textContent.trim() === ${JSON.stringify(text)} && !x.disabled); if (!b) return false; b.click(); return true; })()`;
const setVal = (sel, v) => `(() => { const e = document.querySelector(${JSON.stringify(sel)}); e.value = ${JSON.stringify(v)}; e.dispatchEvent(new Event("input", {bubbles:true})); return !!e; })()`;
const panel = id => `document.getElementById(${JSON.stringify(id)}).innerText.replace(/\\s+/g, " ").slice(0, 600)`;
const rowIds = () => `[...document.querySelectorAll("#center .srow")].map(r => r.getAttribute("data-step")).join(",")`;

(async () => {
  const port = 9333, dir = fs.mkdtempSync(path.join(os.tmpdir(), "chr-"));
  const proc = spawn(chrome, ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, "--window-size=1440,900", "--no-first-run", "--disable-gpu", "about:blank"], { stdio: "ignore" });
  const log = { push: x => { console.log(x); } };
  try {
    const a = await tab(port);
    await a.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
    await nav(a, BASE + "/");
    await shot(a, "1_welcome");
    log.push("new-task-click:" + await ev(a, clickText("＋ 새 작업")));
    await sleep(300);
    await shot(a, "2_new_task");
    // 제목 없이 빈 plan을 시작하려 하면 막힌다
    log.push("empty-without-title:" + await ev(a, clickText("빈 plan으로 시작")));
    await sleep(300);
    log.push("toast-without-title:" + await ev(a, `[...document.querySelectorAll(".toast")].map(t => t.textContent).join("|")`));
    await ev(a, setVal("#newtitle", "연습장 확인"));
    await ev(a, setVal("#newnick", "alice"));
    log.push("empty-plan:" + await ev(a, clickText("빈 plan으로 시작")));
    await sleep(1500);
    await shot(a, "3_empty_scratchpad");
    log.push("center:" + await ev(a, panel("center")));
    log.push("composer:" + await ev(a, `(document.querySelector("#composer textarea")||{}).placeholder`));
    // 명령 만들기: 문서에서 검색 → 고르기 → 필수 값 채우기
    log.push("builder-open:" + await ev(a, clickText("＋ 명령 만들기")));
    await sleep(300);
    const build = async (query, pick, fill, title, chk) => {
      await ev(a, setVal("#center .card .card input", query));
      log.push("search:" + await ev(a, clickText("문서에서 찾기")));
      await sleep(600);
      log.push("results:" + await ev(a, `[...document.querySelectorAll("#center .card .card .srow b")].map(b => b.textContent).slice(0, 6).join(" | ")`));
      log.push("pick:" + await ev(a, `(() => { const r = [...document.querySelectorAll("#center .card .card .srow")].find(x => x.querySelector("b").textContent === ${JSON.stringify(pick)}); if (!r) return false; r.click(); return true; })()`));
      await sleep(400);
      for (const [ph, val] of fill) await ev(a, `(() => { const e = [...document.querySelectorAll("#center .detail input")].find(x => x.placeholder.startsWith(${JSON.stringify(ph)})); if (!e) return false; e.value = ${JSON.stringify(val)}; e.dispatchEvent(new Event("input", {bubbles:true})); return true; })()`);
      if (title) await ev(a, `(() => { const e = [...document.querySelectorAll("#center .detail input")].find(x => x.placeholder === "절차 제목"); e.value = ${JSON.stringify(title)}; e.dispatchEvent(new Event("input", {bubbles:true})); })()`);
      if (chk) await ev(a, `(() => { const e = [...document.querySelectorAll("#center .detail input")].find(x => x.placeholder.startsWith("확인(읽기)")); if (e) { e.value = ${JSON.stringify(chk)}; e.dispatchEvent(new Event("input", {bubbles:true})); } })()`);
      log.push("preview:" + await ev(a, `(document.querySelector("#center .detail pre")||{innerText:"(없음)"}).innerText`));
    };
    await build("describe vpcs", "aws ec2 describe-vpcs", [], "VPC 조회");
    await shot(a, "4_builder");
    log.push("add-draft:" + await ev(a, clickText("초안에 추가")));
    await sleep(1200);
    // 필수 값이 비어 있어도 초안으로 저장된다(⚠)
    log.push("builder-open2:" + await ev(a, clickText("＋ 명령 만들기")));
    await sleep(300);
    await build("reboot instances", "aws ec2 reboot-instances", [], "인스턴스 재부팅", "aws ec2 describe-instances");
    log.push("add-incomplete:" + await ev(a, clickText("초안에 추가")));
    await sleep(1200);
    log.push("builder-open3:" + await ev(a, clickText("＋ 명령 만들기")));
    await sleep(300);
    await build("reboot instances", "aws ec2 reboot-instances", [["--instance-ids", "i-0abc123def4567890"]], "인스턴스 재부팅(완성)", "aws ec2 describe-instances --instance-ids i-0abc123def4567890");
    log.push("add-complete:" + await ev(a, clickText("초안에 추가")));
    await sleep(1200);
    log.push("rows:" + await ev(a, rowIds()));
    log.push("validation-icons:" + await ev(a, `[...document.querySelectorAll("#center .srow")].map(r => r.getAttribute("data-step") + ":" + [...r.querySelectorAll(".ico, .vi")].map(i => i.textContent.trim()).join("")).join(" , ")`));
    await shot(a, "5_drafts_incomplete");
    log.push("send-disabled-before-confirm:" + await ev(a, `(() => { const b = [...document.querySelectorAll("#center button")].find(x => x.textContent.startsWith("검토 요청")); return b ? b.disabled + ":" + b.textContent : "none"; })()`));
    // 불완전 초안은 확정할 수 없다 → 상세에서 값을 고쳐 확정
    log.push("confirm-all-valid:" + await ev(a, clickText("요청 가능한 것 모두 포함")));
    await sleep(1000);
    log.push("send-after-confirm:" + await ev(a, `(() => { const b = [...document.querySelectorAll("#center button")].find(x => x.textContent.startsWith("검토 요청")); return b ? b.disabled + ":" + b.textContent : "none"; })()`));
    await shot(a, "5b_confirmed");
    // 드래그: 두 번째 초안을 첫 번째 앞으로
    const before = await ev(a, rowIds());
    const ids = before.split(",");
    const moved = await ev(a, `(() => { const rows = [...document.querySelectorAll("#center .srow")]; const from = rows[rows.length - 1], to = rows[0]; const dt = new DataTransfer();
      from.dispatchEvent(new DragEvent("dragstart", {bubbles:true, dataTransfer:dt})); to.dispatchEvent(new DragEvent("dragover", {bubbles:true, cancelable:true, dataTransfer:dt})); to.dispatchEvent(new DragEvent("drop", {bubbles:true, cancelable:true, dataTransfer:dt})); return from.getAttribute("data-step"); })()`);
    await sleep(1200);
    const after = await ev(a, rowIds());
    log.push("drag rows-before:" + before + " rows-after:" + after + " first-is-dragged:" + (after.split(",")[0] === moved));
    log.push("undo:" + await ev(a, clickText("↶")));
    await sleep(800);
    log.push("rows-after-undo:" + await ev(a, rowIds()));
    log.push("redo:" + await ev(a, clickText("↷")) + " rows:" + await ev(a, rowIds()));
    log.push("undo-again:" + await ev(a, clickText("↶")) + " rows:" + await ev(a, rowIds()));
    await sleep(500);
    // 다시 확정(드래그·되돌리기 뒤 확정 상태는 유지되어야 한다)
    log.push("send-to-apply:" + await ev(a, clickText(await ev(a, `[...document.querySelectorAll("#center button")].find(x => x.textContent.startsWith("검토 요청")).textContent`))));
    await sleep(1500);
    log.push("toasts-after-send:" + await ev(a, `[...document.querySelectorAll(".toast")].map(t => t.textContent).join("|")`));
    await shot(a, "5c_sent");
    log.push("center-after-send:" + await ev(a, panel("center")));
    // 초대 링크 만들기 → 다른 탭(다른 닉네임)이 참여
    log.push("invite-btn(center header):" + await ev(a, clickText("초대 링크 만들기")));
    await sleep(800);
    log.push("workers-under-title:" + await ev(a, `document.getElementById("center").innerText.replace(/\s+/g, " ").slice(0, 150)`));
    const invite = await ev(a, `document.querySelector("#center input[readonly]").value`);
    log.push("invite:" + invite.replace(/i=.*/, "i=…"));
    const b = await tab(port);
    await nav(b, invite);
    await ev(b, `(() => { const n = document.querySelector("#center input"); n.value = "bob"; n.dispatchEvent(new Event("input", {bubbles:true})); })()`);
    log.push("join:" + await ev(b, clickText("참여")));
    await sleep(2500);
    await shot(b, "6_bob");
    log.push("bob-tabs-before-approval:" + await ev(b, `[...document.querySelectorAll(".ctab")].map(x => x.textContent).join(",")`));
    log.push("bob-approve:" + await ev(b, clickText("확정 (일괄 승인)")));
    await sleep(1500);
    log.push("alice-tabs-after-plan-approval(Apply 없어야 함):" + await ev(a, `[...document.querySelectorAll(".ctab")].map(x => x.textContent + (x.className.includes(" on") ? "*" : "")).join(",")`));
    await shot(a, "6b_plan_approved");
    log.push("alice-apply-btn:" + await ev(a, clickText("apply 진행 →")));
    await sleep(2000);
    log.push("tabs-after-start:" + await ev(a, `[...document.querySelectorAll(".ctab")].map(x => x.textContent + (x.className.includes(" on") ? "*" : "")).join(",")`));
    log.push("apply-actions:" + await ev(a, `[...document.querySelectorAll("#center .detail button")].map(x => x.textContent.trim()).join("|")`));
    await shot(a, "6c_apply_tab");
    const ico = (tabc, step, label) => ev(tabc, `(() => { const b = document.querySelector('#center [data-step="${step}"] button[aria-label="${label}"]'); if (!b || b.disabled) return b ? "disabled" : "none"; b.click(); return "clicked"; })()`);
    const applyRows = (await ev(a, rowIds())).split(",");
    log.push("apply-rows:" + applyRows.join(","));
    const reads = await ev(a, `[...document.querySelectorAll("#center .srow")].filter(r => r.querySelector('button[aria-label="확인(실행)"]')).map(r => r.getAttribute("data-step")).join(",")`);
    const muts = await ev(a, `[...document.querySelectorAll("#center .srow")].filter(r => r.querySelector('button[aria-label="승인"], span[aria-label="승인"]')).map(r => r.getAttribute("data-step")).join(",")`);
    log.push("read-steps:" + reads + " change-steps:" + muts);
    const r0 = reads.split(",")[0], m0 = muts.split(",")[0];
    log.push("read-icon(" + r0 + ", alice):" + await ico(a, r0, "확인(실행)"));
    await sleep(2000);
    if (m0) {
      log.push("approve-icon(" + m0 + ", alice=requester):" + await ico(a, m0, "승인"));
      log.push("approve-icon(" + m0 + ", bob):" + await ico(b, m0, "승인"));
      await sleep(1500);
      log.push("run-icon(" + m0 + ", bob):" + await ico(b, m0, "실행"));
      await sleep(2500);
    }
    log.push("row-icons:" + await ev(a, `[...document.querySelectorAll("#center .srow")].map(r => r.getAttribute("data-step") + ":" + [...r.querySelectorAll(".ico")].map(i => i.textContent).join("")).join(", ")`));
    log.push("terminal:" + (await ev(a, `(document.querySelector("#center .term") || {innerText:"(없음)"}).innerText.replace(/\s+/g, " ").slice(0, 260)`)));
    log.push("history-dot:" + await ev(a, `[...document.querySelectorAll("#left .hline")].map(x => x.querySelector(".dot").className + "/" + getComputedStyle(x.querySelector(".dot")).animationName).join(" | ")`));
    await shot(a, "6d_after_confirm");
    // 이력은 서버 DB에서: 저장소를 비우고(자격만 복원) 새로 고쳐도 제목·상태가 나온다
    const hist = await ev(a, `fetch("/api/history", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({items: JSON.parse(localStorage.getItem("creds")).map(c => ({sid:c.sid, token:c.token}))})}).then(r => r.json()).then(j => JSON.stringify(j.items.map(i => ({title:i.title, state:i.state, steps:i.steps, valid:i.valid}))))`);
    log.push("history-from-db:" + hist);
    log.push("stored-in-browser:" + await ev(a, `Object.keys(localStorage).join(",") + " | creds-keys:" + Object.keys(JSON.parse(localStorage.getItem("creds"))[0]).join(",")`));
    await shot(a, "7_applying");
    await a.send("Emulation.setDeviceMetricsOverride", { width: 390, height: 800, deviceScaleFactor: 2, mobile: true });
    await sleep(800);
    await shot(a, "8_mobile");
  } catch (e) { log.push("ERROR:" + e.message); }
  finally { proc.kill(); }
  
  process.exit(0);
})();
