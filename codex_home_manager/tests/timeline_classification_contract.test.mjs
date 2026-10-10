import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const bundle = await build({ entryPoints: [fileURLToPath(new URL("../src/threadTimeline.ts", import.meta.url))], bundle: true, format: "esm", platform: "browser", write: false });
const { timelineItemFromJson, readBrowserTimelinePage } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].contents).toString("base64")}`);
const fixtures = JSON.parse(await fs.readFile(new URL("./fixtures/timeline_classification_contract.json", import.meta.url), "utf8"));
for (const { name, record, expected } of fixtures) {
  const item = timelineItemFromJson(record, 0);
  for (const [key, value] of Object.entries(expected)) assert.deepEqual(item?.[key], value, `${name}: ${key}`);
}
const file = new File([fixtures.map(({ record }, index) => JSON.stringify({ ...record, timestamp: new Date(index * 10_000).toISOString() })).join("\n") + "\n"], "classification.jsonl");
for (const kind of ["assistant", "assistant_unclassified", "commentary"]) {
  const page = await readBrowserTimelinePage(file, "fixture", "Fixture", "fixture", { kind, sourceFilter: "all" });
  assert.equal(page.items.length, fixtures.filter(({ expected }) => expected.kind === kind).length, kind);
  assert.ok(page.items.every((item) => item.kind === kind));
}
console.log(`Classification contract: ${fixtures.length} fixtures and 3 filters passed`);
assert.equal((await readBrowserTimelinePage(file, "fixture", "Fixture", "fixture", { kind: "user", sourceFilter: "all" })).items.some((item) => item.promptSourceType === "internal"), false);
const browserFixture = fixtures.find((fixture) => fixture.name === "ambient browser and human request");
const browserFile = new File([JSON.stringify(browserFixture.record) + "\n"], "browser.jsonl");
for (const sourceFilter of ["pure", "focused"]) {
  assert.equal((await readBrowserTimelinePage(browserFile, "fixture", "Fixture", "fixture", { sourceFilter, search: "8765" })).items.length, 0);
  assert.equal((await readBrowserTimelinePage(browserFile, "fixture", "Fixture", "fixture", { sourceFilter, search: "Continue" })).items.length, 1);
}
