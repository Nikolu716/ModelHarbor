"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {JSDOM} = require("jsdom");
const root = path.resolve(__dirname, "..", "app", "web", "static");
const dom = new JSDOM(fs.readFileSync(path.join(root, "index.html"), "utf8"), {
  runScripts: "outside-only", url: "http://127.0.0.1:8000/",
});
const w = dom.window, d = w.document, calls = [], ticks = [];
w.escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
w.setInterval = tick => ticks.push(tick);
w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
w.HTMLDialogElement.prototype.close = function () { this.open = false; };
d.getElementById("mappings-wrap").getClientRects = () => [{}];
Object.defineProperty(d, "hidden", {configurable: true, value: false});
const rule = {id: "rule", name: '<img src=x onerror="window.pwned=1">', enabled: true,
  source_model: "requested", source_ips: [], target_models: ["first", "second"]};
const station = (name, available, reason = "") => ({name, available, reason});
const plan = {mode: "target", model: "first", stations: ["Ready"], pending: [],
  targets: [{model: "first", stations: [station("Ready", true), station("Quota", false, "站点额度暂不可用")]}],
  fallback: [station("Original", true)]};
let data = {rules: [rule], error: "", migration_error: "", previews: {rule: {
  "/responses": plan,
  "/chat/completions": {...plan, mode: "fallback", model: "requested", stations: ["Original"]},
}}};
w.api = async (url, method = "GET", _auth, body) => {
  calls.push({url, method, body});
  if (url === "/admin/stations") return [{name: "Ready", enabled: true,
    effective_models: ["first", "second"], provider: "openai", model_api_paths: {}}];
  assert.equal(url, "/admin/model-mappings");
  if (method === "PUT") data = {...data, rules: body.rules};
  return data;
};
w.eval(fs.readFileSync(path.join(root, "model-mappings.js"), "utf8"));
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
async function main() {
  await w.refreshModelMappings();
  const list = d.getElementById("mapping-list");
  assert.match(list.textContent, /Responses：优先 first · 当前候选：Ready/);
  assert.match(list.textContent, /Chat：回退 requested · 当前候选：Original/);
  assert.doesNotMatch(list.textContent, /当前候选：.*Quota/);
  assert.match(list.querySelector(".mapping-availability").title, /Quota：站点额度暂不可用/);
  assert.equal(list.querySelectorAll("img").length, 0);
  assert.equal(w.pwned, undefined);

  // Background metadata refresh must not reset an open editor's unsaved fields.
  list.querySelector('[data-mapping-action="edit"]').click();
  const fields = d.querySelectorAll("[data-target-model]");
  assert.deepEqual([...fields].map(f => f.value), ["first", "second"]);
  fields[0].value = "custom-model";
  const before = calls.length;
  for (const tick of ticks) tick();
  await settle();
  assert.equal(calls.length, before);
  assert.equal(fields[0].value, "custom-model");
  d.getElementById("mapping-form").dispatchEvent(new w.Event("submit", {cancelable: true}));
  await settle();
  const saved = calls.find(call => call.method === "PUT").body.rules[0];
  assert.deepEqual([...saved.target_models], ["custom-model", "second"]);
  assert.equal("targets" in saved, false);
  assert.equal(d.querySelectorAll("[data-target-station]").length, 0);

  // Updated backend admission states, rather than directory-only guesses, control the preview.
  data.previews.rule["/responses"] = {...plan, mode: "fallback", model: "requested", stations: ["Original"]};
  for (const tick of ticks) tick();
  await settle();
  assert.match(list.textContent, /Responses：回退 requested · 当前候选：Original/);
  assert.ok(calls.every(call => ["/admin/model-mappings", "/admin/stations"].includes(call.url)));
  dom.window.close();
  console.log("model mapping preview and editor checks passed");
}
main().catch(error => { console.error(error); dom.window.close(); process.exitCode = 1; });
