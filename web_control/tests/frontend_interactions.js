"use strict";
// JavaScriptCore unit fixture: no browser, server, USB or radio access.
const html = readFile("web_control/static/index.html");
const script = readFile("web_control/static/app.js");
const assert = (ok, message) => { if (!ok) throw new Error(message); };
const clone = (value) => JSON.parse(JSON.stringify(value));
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
class Node {
  constructor(tag = "DIV") { this.tagName = tag; this.dataset = {}; this.children = []; this.listeners = {}; this.attrs = {}; this.disabled = false; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  emit(name, values = {}) { const event = { target: this, button: 0, pointerId: 1, repeat: false, preventDefault() {}, ...values }; for (const fn of this.listeners[name] || []) fn(event); }
  setAttribute(name, value) { this.attrs[name] = value; }
  setPointerCapture() {}
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  get firstElementChild() { return this.children[0]; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((node) => node !== this); }
  closest() { return ["INPUT", "TEXTAREA", "SELECT"].includes(this.tagName) ? this : null; }
}
globalThis.Element = Node;
globalThis.AbortController = class { constructor() { this.signal = {}; } abort() { this.signal.aborted = true; } };
globalThis.crypto = { getRandomValues: (bytes) => bytes.fill(17) };
function fixture(confirmed = false) {
  const e = { calls: [], timers: new Map(), clock: 0, next: 1, deferStop: false, resumeStop: null };
  e.state = { mode: "demo", csrf_token: "csrf-a", board: { state: "ready", token: "board-a", port: "demo" }, tractor: { confirmed }, control: { enabled: confirmed, action: null }, events: [] };
  e.ids = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], new Node(match[1] === "tractorPower" ? "INPUT" : "DIV")]));
  e.buttons = [...html.matchAll(/data-action="([^"]+)"/g)].map((match) => { const node = new Node("BUTTON"); node.dataset.action = match[1]; return node; });
  e.button = (action) => e.buttons.find((button) => button.dataset.action === action);
  globalThis.window = e.window = new Node("WINDOW");
  globalThis.document = e.document = Object.assign(new Node("DOCUMENT"), { hidden: false, getElementById: (id) => { assert(e.ids[id], "Unknown DOM id " + id); return e.ids[id]; }, querySelectorAll: () => e.buttons, createElement: (tag) => new Node(tag.toUpperCase()), createTextNode: (text) => Object.assign(new Node("#TEXT"), { textContent: text }) });
  const timer = (fn, ms, repeat) => { const id = e.next++; e.timers.set(id, { fn, ms, repeat, at: e.clock + ms }); return id; };
  globalThis.setTimeout = (fn, ms) => timer(fn, ms, false);
  globalThis.setInterval = (fn, ms) => timer(fn, ms, true);
  globalThis.clearTimeout = globalThis.clearInterval = (id) => e.timers.delete(id);
  globalThis.fetch = (path, options) => {
    const body = options.body ? JSON.parse(options.body) : undefined;
    e.calls.push({ path, body, options });
    if (path === "/api/status" && e.failStatus) return Promise.reject(new Error("offline"));
    const response = { ok: true, json: async () => clone(e.state) };
    if (path === "/api/power") { e.state.tractor.confirmed = body.confirmed; e.state.control.enabled = body.confirmed && e.state.board.state === "ready"; }
    if (path === "/api/control") e.state.control.action = body.action;
    if (path === "/api/stop") {
      if (e.deferStop) { e.deferStop = false; return new Promise((resolve) => { e.resumeStop = () => { e.state.control.action = null; resolve(response); }; }); }
      e.state.control.action = null;
    }
    return Promise.resolve(response);
  };
  e.count = (path) => e.calls.filter((call) => call.path === "/api/" + path).length;
  e.controls = () => e.calls.filter((call) => call.path === "/api/control");
  e.key = (name, code, extra = {}) => e.window.emit(name, { code, key: "ц", target: e.window, ...extra });
  e.pointer = (action, id = 1) => e.button(action).emit("pointerdown", { pointerId: id });
  e.poll = async () => { [...e.timers.values()].find((timer) => timer.repeat && timer.ms === 500).fn(); await flush(); };
  e.advance = async (ms) => {
    const until = e.clock + ms;
    for (let steps = 0; steps < 1000; steps++) {
      const next = [...e.timers.entries()].filter(([, value]) => value.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) { e.clock = until; return; }
      const [id, value] = next; e.clock = value.at;
      if (value.repeat) value.at += value.ms; else e.timers.delete(id);
      value.fn(); await flush();
    }
    throw new Error("Timer fixture did not settle");
  };
  (0, eval)(script);
  return e;
}
async function run() {
  let e = fixture(); assert(e.buttons.every((button) => button.disabled), "Controls enabled before status"); await flush();
  assert(!e.ids.tractorPower.disabled && e.buttons.every((button) => button.disabled), "Power confirmation gate missing");
  e.pointer("forward"); await flush(); assert(e.count("control") === 0, "Unconfirmed power sent a command");
  e.ids.tractorPower.checked = true; e.ids.tractorPower.emit("change");
  assert(e.buttons.every((button) => button.disabled), "Controls enabled during power confirmation"); await flush();
  assert(e.buttons.every((button) => !button.disabled) && e.count("power") === 1, "Power confirmation did not enable controls");
  assert(e.calls.find((call) => call.path === "/api/power").body.board_token === "board-a", "Power request lost board epoch");

  e.pointer("left_forward"); await flush(); assert(e.count("control") === 1, "Pointer press did not lease");
  await e.advance(99); assert(e.count("control") === 1, "Lease fired before 100 ms");
  await e.advance(1); assert(e.count("control") === 2, "Held pointer did not renew at 100 ms");
  e.window.emit("pointerup"); await flush(); const afterRelease = e.count("control");
  await e.advance(200); assert(e.count("stop") === 1 && e.count("control") === afterRelease, "Release did not stop lease renewals");
  assert(e.calls.find((call) => call.path === "/api/stop").options.keepalive, "Release stop is not keepalive");

  e = fixture(true); await flush(); e.key("keydown", "KeyW"); await flush();
  assert(e.controls()[0].body.action === "forward", "Russian key layout did not use event.code");
  e.key("keydown", "KeyW", { repeat: true }); e.key("keydown", "KeyW"); await flush();
  assert(e.count("control") === 1, "Autorepeat/duplicate keydown restarted control");
  await e.advance(100); assert(e.count("control") === 2, "Held key did not renew its 100 ms lease");
  e.key("keyup", "KeyW"); await flush(); const afterKeyup = e.count("control");
  await e.advance(200); assert(e.count("stop") === 1 && e.count("control") === afterKeyup, "Keyup did not stop");
  e.button("left_forward").dataset.action = "unknown_action"; e.pointer("unknown_action"); await flush();
  assert(e.count("control") === afterKeyup, "Unknown action was sent");

  for (const event of ["blur", "visibilitychange", "pagehide"]) {
    e = fixture(true); await flush(); e.state.control.action = "tower_left"; await e.poll();
    if (event === "visibilitychange") { e.document.hidden = true; e.document.emit(event); } else e.window.emit(event);
    await flush(); assert(e.count("stop") === 0, "Idle " + event + " stopped another client");
    e = fixture(true); await flush(); e.pointer("boom_up"); await flush();
    if (event === "visibilitychange") { e.document.hidden = true; e.document.emit(event); } else e.window.emit(event);
    await flush(); const count = e.count("control"); await e.advance(200);
    assert(e.count("stop") === 1 && e.count("control") === count, "Held " + event + " failed to stop");
  }

  e = fixture(true); await flush(); e.key("keydown", "KeyW"); await flush();
  e.state.board.token = "board-b"; await e.poll(); const rotated = e.count("control");
  await e.advance(200); e.key("keydown", "KeyW", { repeat: true }); await flush();
  assert(e.count("control") === rotated && e.count("stop") === 1, "Board epoch rotation resumed a hold");
  e.key("keyup", "KeyW"); e.key("keydown", "KeyW"); await flush();
  assert(e.controls().at(-1).body.board_token === "board-b" && e.count("control") === rotated + 1, "Fresh press did not use rotated board epoch");
  e.state.board.state = "missing"; e.state.control.enabled = false; await e.poll();
  assert(e.buttons.every((button) => button.disabled), "Disconnect did not disable controls");
  const disconnected = e.count("control"); e.state.board.state = "ready"; e.state.board.token = "board-c"; e.state.control.enabled = true;
  await e.poll(); await e.advance(200); assert(e.count("control") === disconnected, "Readiness automatically resumed a hold");
  e.pointer("right_backward", 3); await flush(); assert(e.count("control") === disconnected + 1, "Fresh pointer press failed after reconnect");
  e.failStatus = true; await e.poll(); const failed = e.count("control"); await e.advance(200);
  assert(e.buttons.every((button) => button.disabled) && e.count("control") === failed, "Network failure left lease active");

  e = fixture(true); await flush(); e.pointer("left_forward"); await flush(); e.deferStop = true;
  e.pointer("right_forward", 2); await flush();
  assert(e.count("stop") === 1 && e.count("control") === 1, "Action change raced ahead of STOP acknowledgement");
  await e.advance(100); assert(e.count("control") === 1, "Old hold continued during action switch");
  e.resumeStop(); await flush(); assert(e.controls().at(-1).body.action === "right_forward", "Acknowledged action change failed");
  assert(e.calls.filter((call) => call.body?.seq).every((call, i, all) => i === 0 || call.body.seq > all[i - 1].body.seq), "Control/STOP sequence did not increase");
  e.deferStop = true; e.pointer("boom_down", 4); await flush(); e.window.emit("pointerup", { pointerId: 4 }); await flush();
  const cancelled = e.count("control"); e.resumeStop(); await flush(); await e.advance(100);
  assert(e.count("control") === cancelled, "Late STOP response resurrected a released gesture");

  e = fixture(true); await flush(); e.deferStop = true; e.ids.tractorPower.checked = false; e.ids.tractorPower.emit("change");
  e.key("keydown", "KeyW"); await flush();
  assert(e.count("control") === 0, "Unchecking power permits a keyboard command while STOP is pending");
  assert(e.buttons.every((button) => button.disabled), "Unchecking power leaves controls enabled while STOP is pending");
  e.resumeStop(); await flush(); assert(e.buttons.every((button) => button.disabled), "Unchecking power did not revoke controls");
  print("Frontend interaction tests passed: power gate, pointer/key leases, release/visibility stops, epochs, failures, action ordering.");
}
run().catch((error) => { print("FAIL: " + error); print(error.stack || ""); quit(); });
