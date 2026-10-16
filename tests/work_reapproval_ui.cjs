"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor() { this.children = []; this.textContent = ""; this.hidden = false; this.value = ""; }
  addEventListener() {}
  replaceChildren(...children) { this.children = children; }
  append(...children) { this.children.push(...children); }
}

async function scenario(error) {
  const elements = new Map();
  const byId = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const window = {};
  const context = vm.createContext({window, document: {createElement: () => new Element()}, byId,
    api: async () => { throw error; }, Option: function(label, value) { this.label = label; this.value = value; },
    setTimeout, clearTimeout, conversationId: null, isBusy: false, busy() {}, addMessage() {}});
  vm.runInContext(fs.readFileSync("frontend/work.js", "utf8"), context);
  await new Promise(resolve => setImmediate(resolve));
  return byId("work-context").textContent;
}

(async () => {
  assert.equal(await scenario(Object.assign(new Error("503"), {code: "PITCHI_WORK_SOURCE_REAPPROVAL_REQUIRED"})),
    "新しいPitchi-workソースの再承認が必要です。Host管理者に再承認を依頼してください。");
  assert.equal(await scenario(Object.assign(new Error("offline"), {kind: "network"})),
    "仕事の受付に接続できません。");
  console.log("PASS source reapproval is distinct from connectivity failure");
})().catch(error => { console.error(error); process.exitCode = 1; });
