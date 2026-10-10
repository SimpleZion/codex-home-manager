import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import ts from "typescript";

const projectRoot = path.resolve(import.meta.dirname, "..");
const domainPath = path.join(projectRoot, "src", "providerInteroperability.ts");
const componentPath = path.join(projectRoot, "src", "ProviderInteroperabilityPanel.tsx");
const stylePath = path.join(projectRoot, "src", "provider-interoperability.css");
const mainPath = path.join(projectRoot, "src", "main.tsx");
const domainSource = fs.readFileSync(domainPath, "utf8");
const componentSource = fs.readFileSync(componentPath, "utf8");
const styleSource = fs.readFileSync(stylePath, "utf8");
const mainSource = fs.readFileSync(mainPath, "utf8");

const compiled = ts.transpileModule(domainSource, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText;
const domainModule = { exports: {} };
new Function("exports", "module", "URLSearchParams", compiled)(domainModule.exports, domainModule, URLSearchParams);
const {
  defaultRepairScope,
  repairRequest,
  repairNeeded,
  routeNeedsAttention,
  transactionCommitted
} = domainModule.exports;

assert.deepEqual(defaultRepairScope, {
  history: true,
  continuationCompatibility: true,
  commonConfig: true,
  interoperabilitySettings: true
});
assert.throws(
  () => repairRequest({ history: false, continuationCompatibility: false, commonConfig: false, interoperabilitySettings: false }),
  /at least one/
);
assert.equal(routeNeedsAttention({ route: { classification: "conflicting" } }), true);
assert.equal(routeNeedsAttention({ route: { classification: "third_party" } }), false);
assert.equal(repairNeeded({ audit: { replacementCount: 0 }, adapterRepair: { commonConfigChanged: true, settingChanges: [] } }), true);
assert.equal(transactionCommitted({ status: "committed" }), true);
assert.equal(transactionCommitted({ status: "failed_rolled_back" }), false);

for (const phase of ["idle", "loading", "ready", "blocked", "applying", "verifying", "failed"]) {
  assert.match(domainSource, new RegExp(`"${phase}"`), `missing explicit ${phase} phase`);
}
assert.match(componentSource, /actual route evidence|实际路由证据/i);
assert.match(componentSource, /历史桶 custom[^\n]+不能用于判断实际请求由谁计费/);
assert.match(componentSource, /核心版本门控/);
assert.match(componentSource, /完成同版本重放测试和哈希登记前不会切换到未知核心/);
assert.match(componentSource, /尚未匹配到经过验证的互通清单/);
assert.match(componentSource, /这不表示补丁文件已经损坏/);
assert.match(componentSource, /配置与进程环境指向不同 Codex 核心/);
assert.match(componentSource, /adapter\.support !== "read_write"/);
assert.match(componentSource, /type="checkbox"/);
assert.match(componentSource, /provider-interoperability\/repair\/preview/);
assert.match(componentSource, /provider-interoperability\/repair"/);
assert.match(componentSource, /provider-interoperability\/verify/);
assert.match(componentSource, /provider-interoperability\/rollback\/preview/);
assert.match(componentSource, /provider-interoperability\/tasks\/\$\{encodeURIComponent\(value\)\}\/audit/);
assert.match(componentSource, /role="dialog"/);
assert.match(componentSource, /aria-modal="true"/);
assert.match(componentSource, /event\.key === "Escape"/);
assert.match(componentSource, /event\.key !== "Tab"/);
assert.match(componentSource, /browserMode/);
assert.match(componentSource, /无法检查 CC Switch 外部状态/);
assert.doesNotMatch(componentSource, /experimental_bearer_token|access_token|refresh_token|client_secret|sk-private/i);
assert.doesNotMatch(componentSource, /JSON\.stringify\((?:status|preview|transaction|verification)\)/);

assert.match(styleSource, /@media \(max-width: 980px\)/);
assert.match(styleSource, /@media \(max-width: 620px\)/);
assert.match(styleSource, /min-width:\s*0/);
assert.match(styleSource, /overflow-wrap:\s*anywhere/);
assert.match(styleSource, /grid-template-columns:\s*minmax\(0, 1fr\)/);
assert.match(mainSource, /value: "providers"/);
assert.match(mainSource, /activeSection === "providers"/);
assert.match(mainSource, /<ProviderInteroperability/);

console.log("provider interoperability UI contracts passed");
