"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {JSDOM} = require("jsdom");
const root = path.resolve(__dirname, "..", "app", "web", "static");
const dom = new JSDOM(fs.readFileSync(path.join(root, "index.html"), "utf8"), {runScripts: "outside-only", url: "http://localhost/dashboard"});
const w = dom.window, d = w.document, calls = [], copied = [], notices = [], timers = new Map(), intervals = [];
let timerId = 0;
w.ResizeObserver = class { observe() {} disconnect() {} };
w.setInterval = callback => { intervals.push(callback); return intervals.length; };
w.setTimeout = callback => { timers.set(++timerId, callback); return timerId; };
w.clearTimeout = id => timers.delete(id);
w.requestAnimationFrame = callback => callback();
w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event("close")); };
const subscription = {name: "ChatGPT", provider: "chatgpt", enabled: true, chatgpt_account: "own-profile",
  effective_models: ["gpt-test"], probe_model_candidates: ["gpt-test"], model_api_paths: {"gpt-test": ["/responses"]},
  chatgpt: {connected: true}, chatgpt_usage: {status: "connected", updated_at: 1900000000, windows: [
    {name: "<img src=x>", window: "primary", used_percent: 20, duration_mins: 300, resets_at: 1900001000},
    {name: "Codex", window: "secondary", used_percent: 35, duration_mins: 10080}], reset_credits: {availableCount: 2}}};
w.STATIONS = [subscription, {name: "Other", provider: "openai", enabled: true, effective_models: ["other-model"], probe_model_candidates: ["other-model"]}];
w.statusData = {stations: w.STATIONS};
w.editingStation = null;
w.escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
w.setTab = w.setSectionTab = () => {};
w.showToast = (message, error) => notices.push({message, error});
w.refreshStations = async () => true;
w.render = () => {};
w.api = async (url, method = "GET", _reload, body) => {
  calls.push({url, method, body});
  if (url.includes("/usage")) return subscription.chatgpt_usage;
  if (url === "/admin/visual-tests/prompts") return [{id: "builtin", name: "Draw", prompt: "Draw an SVG", builtin: true}];
  return method === "POST" ? {running: true} : {result: null, running: false, schedule: {enabled: false}};
};
w.ModelHarborChatGPT = {consume: async () => {}};
w.eval(fs.readFileSync(path.join(root, "chatgpt-quota.js"), "utf8"));
w.eval(fs.readFileSync(path.join(root, "visual-tests.js"), "utf8"));
const settle = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
async function open(name) {
  const button = d.createElement("button");
  button.dataset.visualTest = name;
  d.body.append(button);
  button.click();
  await settle();
}
async function main() {
  await open("Other");
  const limit = d.getElementById("visualTestLimit");
  assert.equal(limit.value, "12000");
  assert.equal(limit.closest("label").hidden, false);
  limit.value = "25000";
  d.getElementById("visualTestForm").dispatchEvent(new w.Event("submit", {bubbles: true, cancelable: true}));
  await settle();
  assert.equal(calls.find(call => call.method === "POST").body.max_output_tokens, 25000);
  d.getElementById("visualTestClose").click();
  await open("ChatGPT");
  assert.equal(limit.closest("label").hidden, true);
  assert.equal(limit.disabled, true);
  assert.equal(d.getElementById("visualScheduleEnabled").disabled, true);
  assert.equal(d.getElementById("visualTestProtocol").querySelector('[value="chat_completions"]').disabled, true);
  d.getElementById("visualTestForm").dispatchEvent(new w.Event("submit", {bubbles: true, cancelable: true}));
  await settle();
  const submitted = calls.filter(call => call.method === "POST").at(-1).body;
  assert.equal(submitted.model, "gpt-test");
  assert.equal("max_output_tokens" in submitted, false);
  d.getElementById("visualTestClose").click();
  const quota = d.createElement("div");
  quota.innerHTML = w.ModelHarborChatGPTQuota.markup(subscription);
  d.body.append(quota);
  quota.querySelector("button").click();
  await settle();
  assert.equal(d.getElementById("chatgptUsageDialog").open, true);
  assert.equal(d.querySelectorAll("#chatgptUsageWindows progress").length, 2);
  assert.equal(d.querySelectorAll("#chatgptUsageWindows img").length, 0);
  assert.equal(d.getElementById("chatgptUsageCreditCount").textContent, "2 张可用");
  assert.equal(d.getElementById("chatgptUsageCredits").querySelector("button").textContent, "使用");
  // The countdown must not rebuild connected quota cards or reset-credit controls.
  const creditButton = d.getElementById("chatgptUsageCredits").querySelector("button");
  for (const tick of intervals) tick();
  assert.equal(d.getElementById("chatgptUsageCredits").querySelector("button"), creditButton);
  d.getElementById("chatgptUsageClose").click();
  subscription.chatgpt_usage = {status: "pending", user_code: "TEST-1234", expires_at: Date.now() / 1000 + 600};
  quota.querySelector("button").click();
  await settle();
  assert.equal(d.getElementById("chatgptUsageDevice").hidden, false);
  assert.equal(d.getElementById("chatgptUsageCreditSection").hidden, true);
  assert.equal(d.getElementById("chatgptUsageWindows").hidden, true);
  assert.equal(d.getElementById("chatgptUsageFooter").hidden, true);
  assert.equal(d.getElementById("chatgptUsageCode").textContent, "TEST-1234");
  assert.ok(timers.size > 0, "opening an existing authorization must resume status polling");
  const copy = d.getElementById("chatgptUsageCopy");
  Object.defineProperty(w.navigator, "clipboard", {configurable: true, value: {writeText: async text => copied.push(text)}});
  w.isSecureContext = true;
  copy.click(); await settle();
  assert.equal(copied.at(-1), "TEST-1234");
  assert.equal(copy.textContent.trim(), "已复制");
  // LAN HTTP and denied Clipboard API permissions both use a helper inside the modal.
  d.execCommand = command => {
    assert.equal(command, "copy");
    assert.equal(d.activeElement.parentElement.id, "chatgptUsageDialog");
    copied.push(d.activeElement.value);
    return true;
  };
  w.isSecureContext = false;
  copy.click(); await settle();
  assert.equal(copied.length, 2);
  assert.equal(copied.at(-1), "TEST-1234");
  assert.equal(d.querySelectorAll("#chatgptUsageDialog textarea").length, 0);
  w.isSecureContext = true;
  w.navigator.clipboard.writeText = async () => { throw new Error("Permission denied"); };
  copy.click(); await settle();
  assert.equal(copied.length, 3);
  d.execCommand = () => false;
  copy.click(); await settle();
  assert.equal(w.getSelection().toString(), "TEST-1234");
  assert.equal(notices.at(-1).error, true);
  d.getElementById("chatgptUsageClose").click();
  assert.equal(timers.size, 0, "closing the dialog must stop authorization polling");
  quota.querySelector("button").click(); await settle();
  assert.ok(timers.size > 0);
  // A completed authorization changes the view and ends polling.
  subscription.chatgpt_usage = {status: "connected", windows: [{name: "Codex", duration_mins: 10080, used_percent: 16}]};
  await [...timers.values()][0](); await settle();
  assert.equal(d.getElementById("chatgptUsageDevice").hidden, true);
  assert.equal(d.getElementById("chatgptUsageWindows").hidden, false);
  assert.ok(d.getElementById("chatgptUsageWindows").textContent.includes("7 天"));
  assert.equal(timers.size, 0);
  w.ModelHarborChatGPTQuota.update("own-profile", {status: "pending", user_code: "OLD-1234", expires_at: Date.now() / 1000 - 1});
  assert.equal(copy.disabled, true);
  assert.equal(d.getElementById("chatgptUsageConnect").textContent, "重新获取验证码");
  assert.equal(d.getElementById("chatgptUsageFooter").hidden, false);
  dom.window.close();
  console.log("subscription quota and visual-test interaction checks passed");
}
main().catch(error => { console.error(error); dom.window.close(); process.exitCode = 1; });
