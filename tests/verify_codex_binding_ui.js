"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {JSDOM} = require("jsdom");
const root = path.resolve(__dirname, "..", "app", "web", "static");
const dom = new JSDOM(fs.readFileSync(path.join(root, "index.html"), "utf8"), {runScripts: "outside-only", url: "http://localhost/dashboard"});
const w = dom.window, d = w.document, calls = [], timers = new Map(), intervals = [], copied = [];
let timerId = 0, attemptId = 0, startResult, statusResult, modelRenders = 0;
const oldStation = {name: "Existing Codex", provider: "chatgpt", chatgpt_account: "saved-profile",
  chatgpt_mode: "codex_relay", chatgpt: {email: "old@example.test", connected: true, codex_connected: true, plan: "pro"}};
w.STATIONS = [oldStation];
w.setInterval = callback => { intervals.push(callback); return intervals.length; };
w.setTimeout = callback => { timers.set(++timerId, callback); return timerId; };
w.clearTimeout = id => timers.delete(id);
w.showToast = () => {};
w.renderStationModels = () => modelRenders++;
w.api = async (url, method = "GET", _reload, body) => {
  calls.push({url, method, body});
  if (method === "DELETE") return {status: "cancelled"};
  if (method === "POST" && url === "/admin/chatgpt/codex/login") return startResult || {
    id: "attempt-" + ++attemptId, status: "pending", user_code: "TEST-1234",
    verification_url: "https://auth.openai.com/codex/device", expires_at: Date.now() / 1000 + 600};
  if (method === "GET") return statusResult || {status: "pending"};
  throw new Error("unexpected route: " + url);
};
w.eval(fs.readFileSync(path.join(root, "codex-binding.js"), "utf8"));
w.eval(fs.readFileSync(path.join(root, "chatgpt.js"), "utf8"));
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
async function tickPoll() {
  const entry = [...timers.entries()][0];
  assert.ok(entry, "authorization status must be polled");
  timers.delete(entry[0]); await entry[1](); await settle();
}
const select = (id, value) => { d.getElementById(id).value = value; d.getElementById(id).dispatchEvent(new w.Event("change")); };
const bind = () => d.getElementById("sf-chatgpt-login").click();
const starts = () => calls.filter(call => call.method === "POST");
async function main() {
  select("sf-provider", "chatgpt");
  select("sf-chatgpt-mode", "codex_relay");
  w.ModelHarborChatGPT.update();
  assert.equal(d.getElementById("sf-chatgpt-login").textContent, "绑定 Codex 账号");
  assert.match(w.ModelHarborCodexBinding.validate(), /先绑定/);
  bind(); bind(); await settle();
  assert.equal(starts().length, 1, "double click cannot start two authorizations");
  assert.equal(d.getElementById("sf-codex-code").textContent, "TEST-1234");
  assert.equal(d.getElementById("sf-codex-login").href, "https://auth.openai.com/codex/device");
  assert.match(w.ModelHarborCodexBinding.validate(), /完成或取消/);
  d.execCommand = command => {
    assert.equal(command, "copy");
    assert.equal(d.activeElement.parentElement.id, "sf-codex-device");
    copied.push(d.activeElement.value); return true;
  };
  w.isSecureContext = false;
  d.getElementById("sf-codex-copy").click(); await settle();
  assert.deepEqual(copied, ["TEST-1234"]);
  assert.equal(d.getElementById("sf-codex-copy").textContent, "已复制");
  assert.match(d.getElementById("sf-codex-feedback").textContent, /已复制/);
  d.execCommand = () => false;
  d.getElementById("sf-codex-copy").click(); await settle();
  assert.match(d.getElementById("sf-codex-feedback").textContent, /Ctrl\+C/);
  assert.equal(w.getSelection().toString(), "TEST-1234");

  statusResult = {status: "completed", profile: "fresh-profile", account: {
    email: "new@example.test", connected: true, codex_connected: true, plan: "plus"}};
  await tickPoll();
  assert.equal(d.getElementById("sf-chatgpt-account").value, "fresh-profile");
  assert.equal(d.getElementById("sf-chatgpt-email").textContent, "new@example.test");
  assert.match(d.getElementById("sf-chatgpt-status").textContent, /待保存/);
  assert.equal(d.getElementById("sf-chatgpt-plan").textContent, "Plus");
  assert.equal(w.ModelHarborCodexBinding.validate(), "");
  assert.equal(d.getElementById("sf-chatgpt-usage-link").disabled, true, "unsaved account usage is not read through the old station");
  assert.equal(timers.size, 0);
  assert.ok(modelRenders > 0);
  assert.equal(oldStation.chatgpt_account, "saved-profile");

  w.ModelHarborCodexBinding.reset();
  d.getElementById("sf-chatgpt-account").value = "saved-profile";
  w.ModelHarborChatGPT.update();
  assert.equal(d.getElementById("sf-chatgpt-login").textContent, "更换绑定账号");
  statusResult = null; bind(); await settle();
  d.getElementById("sf-codex-cancel").click(); await settle();
  assert.equal(d.getElementById("sf-chatgpt-account").value, "saved-profile");
  assert.equal(w.ModelHarborCodexBinding.validate(), "");
  assert.equal(timers.size, 0);
  assert.ok(calls.some(call => call.method === "DELETE"));

  // Closing the editor before a code arrives must cancel that late attempt.
  let finishStart;
  startResult = new Promise(resolve => {finishStart = resolve;});
  bind(); await settle();
  w.ModelHarborCodexBinding.reset();
  finishStart({id: "late-start", status: "pending", user_code: "LATE-1234", expires_at: Date.now() / 1000 + 600});
  await settle();
  assert.ok(calls.some(call => call.url.endsWith("/late-start") && call.method === "DELETE"));
  assert.equal(d.getElementById("sf-chatgpt-account").value, "saved-profile");
  assert.equal(d.getElementById("sf-codex-device").hidden, true);

  // Switching editor contexts while completion is in flight cannot bind the wrong station.
  startResult = null;
  let finishStatus;
  statusResult = new Promise(resolve => {finishStatus = resolve;});
  bind(); await settle();
  const polling = tickPoll(); await settle();
  w.ModelHarborCodexBinding.reset();
  d.getElementById("sf-chatgpt-account").value = "";
  finishStatus({status: "completed", profile: "too-late", account: {codex_connected: true}});
  await polling;
  assert.equal(d.getElementById("sf-chatgpt-account").value, "");
  assert.equal(timers.size, 0);

  // Expiry preserves the old binding, disables copy, and permits a new code.
  d.getElementById("sf-chatgpt-account").value = "saved-profile";
  startResult = {id: "expired", status: "pending", user_code: "OLD-1234", expires_at: Date.now() / 1000 - 1};
  statusResult = null; bind(); await settle();
  for (const tick of intervals) tick();
  assert.equal(d.getElementById("sf-codex-copy").disabled, true);
  assert.equal(d.getElementById("sf-chatgpt-login").textContent, "重新获取验证码");
  assert.equal(w.ModelHarborCodexBinding.validate(), "");
  startResult = null; bind(); await settle();
  assert.equal(d.getElementById("sf-codex-copy").disabled, false);
  select("sf-chatgpt-mode", "shared"); await settle();
  assert.equal(d.getElementById("sf-codex-device").hidden, true);
  assert.equal(timers.size, 0);
  assert.ok(starts().every(call => call.url === "/admin/chatgpt/codex/login"), "native mode never invokes sharing OAuth");
  dom.window.close();
  console.log("Codex binding, staging, cancellation and clipboard checks passed");
}
main().catch(error => { console.error(error); dom.window.close(); process.exitCode = 1; });
