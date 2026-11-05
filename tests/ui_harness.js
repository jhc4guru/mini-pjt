// 화면 스크립트를 가짜 DOM에서 실행해 서버 스냅샷(JSON)마다 렌더링한다(브라우저 없이 예외·버튼 상태·드래그 동작을 확인).
// 사용: node tests/ui_harness.js app/static/index.html cases.json  → 각 케이스의 영역별 텍스트, 버튼 목록, 보낸 메시지를 JSON으로 출력
const vm = require("vm"), fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

class Node {}
class Text extends Node { constructor(t) { super(); this.text = String(t); } }
class El extends Node {
  constructor(tag) { super(); this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.className = ""; this.value = ""; this.classList = { add() {}, remove() {}, toggle() {} }; }
  append(...k) { if (k.some(x => x === null || x === undefined)) throw new Error("append(null): 실제 브라우저는 \"null\" 글자를 찍는다"); this.children.push(...k); }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(t, f) { this.listeners[t] = f; }
  replaceChildren(...k) { if (k.some(x => x === null || x === undefined)) throw new Error("replaceChildren(null)"); this.children = k; }
  remove() {}
  contains() { return false; }
  focus() {}
}
const els = { shell: new El("div"), left: new El("aside"), center: new El("main"), chat: new El("div"), composer: new El("footer") };
const doc = { createElement: t => new El(t), createTextNode: t => new Text(t), getElementById: id => els[id], querySelectorAll: () => [], body: new El("body"), addEventListener() {}, activeElement: null };
const sent = [];
const ctx = {
  document: doc, Node, localStorage: { getItem: () => null, setItem() {}, removeItem() {} }, sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  location: { search: "", protocol: "http:", host: "x", origin: "http://x" }, history: { replaceState() {} },
  WebSocket: class { constructor() { this.readyState = 1; } send(m) { sent.push(JSON.parse(m)); } close() {} },
  navigator: { clipboard: { writeText: () => Promise.resolve() } }, setTimeout: () => 0, fetch: () => Promise.reject(new Error("no network")), confirm: () => true, URLSearchParams, console,
};
vm.createContext(ctx);
vm.runInContext(script, ctx);
vm.runInContext("S.ws = new WebSocket(\"ws://x\");", ctx);

const text = n => (n instanceof Text ? n.text : (n.children || []).map(text).join(" "));
const walk = (n, f) => { if (n instanceof El) { f(n); n.children.forEach(c => walk(c, f)); } };
const buttons = n => { const out = []; walk(n, e => { if (e.tag === "button") out.push({ text: e.attrs["aria-label"] || text(e), disabled: "disabled" in e.attrs, click: e.listeners.click, cls: e.className || "" }); }); return out; };
const textareas = n => { const out = []; walk(n, e => { if (e.tag === "textarea") out.push(e); }); return out; };
const plain = bs => bs.map(b => ({ text: b.text, disabled: b.disabled }));
const find = (n, pred) => { let r = null; walk(n, e => { if (!r && pred(e)) r = e; }); return r; };
const fakeEvent = () => ({ preventDefault() {}, dataTransfer: { setData() {}, effectAllowed: "" } });

const result = {};
for (const c of cases) {
  vm.runInContext(`S.sid = "s1"; S.nick = ${JSON.stringify(c.view.you.nick)}; S.view = ${JSON.stringify(c.view)}; S.history = ${JSON.stringify(c.history || [])}; S.conn = "연결됨"; S.sel = ${JSON.stringify(c.sel || null)}; S.invite = ${JSON.stringify(c.invite || null)}; S.newTask = false; S.ctab = ${JSON.stringify(c.ctab || "plan")}; S.addAt = ${JSON.stringify(c.addAt || null)}; S.autoOff = true; S.bld = ${JSON.stringify(c.bld || null)}; S.echo = ${JSON.stringify(c.echo || null)}; render();`, ctx);
  const cta = textareas(els.composer)[0];
  const r = result[c.name] = {
    left: text(els.left), center: text(els.center), chat: text(els.chat), leftButtons: plain(buttons(els.left)), chatButtons: plain(buttons(els.chat)), buttons: plain(buttons(els.center)),
    composer: { buttons: plain(buttons(els.composer)), disabled: cta ? "disabled" in cta.attrs : null, placeholder: cta ? cta.attrs.placeholder : null },
    rows: [], draggable: [], actors: {}, rowIcons: {}, dots: [], terminal: "", tabs: buttons(els.center).filter(b => b.cls.includes("ctab")).map(b => ({ text: b.text, on: b.cls.includes(" on") })),
  };
  walk(els.center, e => {
    if (e.attrs["data-step"]) {
      r.rows.push(e.attrs["data-step"]);
      if (e.attrs.draggable === "true") r.draggable.push(e.attrs["data-step"]);
      const a = find(e, x => (x.className || "").startsWith("actor"));
      if (a) r.actors[e.attrs["data-step"]] = text(a);
      const icons = [];
      walk(e, x => { if ((x.className || "").startsWith("ico ")) icons.push({ label: x.attrs["aria-label"], glyph: text(x), tag: x.tag, disabled: "disabled" in x.attrs }); });
      r.rowIcons[e.attrs["data-step"]] = icons;
    }
  });
  walk(els.left, e => { if ((e.className || "").startsWith("dot ") && e.attrs["aria-label"]) r.dots.push({ cls: e.className, label: e.attrs["aria-label"] }); });
  walk(els.center, e => { if (e.className === "term") r.terminal = text(e); });
  const clicks = c.clicks || (c.click ? [c.click] : []);
  if (clicks.length) {                                                // 버튼을 순서대로 눌렀을 때 서버로 나가는 메시지(누를 때마다 다시 그려진 화면에서 찾는다)
    sent.length = 0;
    for (const label of clicks) {
      const b = [...buttons(els.center), ...buttons(els.left), ...buttons(els.composer)].find(x => x.text === label);
      if (b && b.click) b.click({ stopPropagation() {} });
    }
    r.sent = sent.slice();
    r.centerAfter = text(els.center);
  }
  if (c.drag) {                                                       // 끌어서 놓기
    sent.length = 0;
    const from = find(els.center, e => e.attrs["data-step"] === c.drag.from);
    from.listeners.dragstart(fakeEvent());
    const to = c.drag.end ? find(els.center, e => e.attrs["data-end"] === c.drag.end) : find(els.center, e => e.attrs["data-step"] === c.drag.to);
    to.listeners.dragover(fakeEvent());
    to.listeners.drop(fakeEvent());
    r.sent = sent.slice();
  }
}
// 새 작업과 시작 화면
vm.runInContext(`S.sid = null; S.view = null; S.history = []; S.newTask = true; render();`, ctx);
result.newtask = { left: text(els.left), center: text(els.center), chat: text(els.chat), composer: { buttons: plain(buttons(els.composer)), placeholder: (textareas(els.composer)[0] || { attrs: {} }).attrs.placeholder } };
vm.runInContext(`S.newTask = false; render();`, ctx);
result.welcome = { center: text(els.center), composer: textareas(els.composer).length };
const ev = JSON.parse(process.env.UI_EVAL || "null");
if (ev) result.eval = vm.runInContext(ev, ctx);
console.log(JSON.stringify(result));
