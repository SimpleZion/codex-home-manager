import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { build } from "esbuild";

const projectRoot = path.resolve(import.meta.dirname, "..");
const reportPath = path.resolve(process.argv[2] || "");
const outputPath = path.resolve(process.argv[3] || "");
const outputRoot = path.resolve(projectRoot, "..", "output");
for (const target of [reportPath, outputPath]) {
  const relative = path.relative(outputRoot, target);
  if (relative.startsWith("..") || path.isAbsolute(relative)) throw new Error("Audit files must stay in workspace/output");
}
const source = JSON.parse(await readFile(reportPath, "utf8"));
const bundle = await build({
  entryPoints: [path.join(projectRoot, "src", "threadTimeline.ts")],
  bundle: true, format: "esm", platform: "browser", write: false
});
const { timelineItemFromJson, readBrowserTimelinePage } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].contents).toString("base64")}`
);
const fixtures = [];
for (const fixture of source.fixtures) {
  const item = timelineItemFromJson(fixture.input, 0);
  const file = new File([JSON.stringify(fixture.input) + "\n"], "classification-fixture.jsonl");
  const pureInputPage = await readBrowserTimelinePage(file, "fixture", "Fixture", file.name, {kind: "user", sourceFilter: "pure"});
  fixtures.push({name: fixture.name, kind: item?.kind, label: item?.label,
    phase: item?.phase, readable: item?.readable, text: item?.text,
    promptSourceType: item?.promptSourceType, pureInputRows: pureInputPage.items.length});
}
const result = {sourceReport: reportPath, productModified: false, fixtures};
await writeFile(outputPath, JSON.stringify(result, null, 2) + "\n");
console.log(JSON.stringify(result));
