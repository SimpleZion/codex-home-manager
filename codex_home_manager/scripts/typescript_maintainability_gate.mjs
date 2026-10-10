import fs from "node:fs";
import path from "node:path";
import ts from "typescript";

const managerRoot = path.resolve(import.meta.dirname, "..");
const sourceRoot = path.join(managerRoot, "src");
const defaultFunctionLineBudget = 240;
const legacyFunctionBudgets = new Map([
  ["src/main.tsx:App", 1234],
  // These two state machines coordinate cancellable pagination, virtual-list
  // anchoring, and indexed search. Keep exact reviewed baselines while new
  // top-level functions remain subject to the 240-line default.
  ["src/main.tsx:ThreadPromptModal", 1262],
  ["src/main.tsx:ThreadsModule", 458],
  ["src/main.tsx:ThreadTimelinePanel", 822],
  ["src/main.tsx:PromptVirtualList", 241],
  ["src/main.tsx:DetailPanel", 363],
  ["src/main.tsx:ThreadFullDetailModal", 363],
  ["src/main.tsx:DailyTokenUsageChart", 297]
]);

function sourceFiles(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(entryPath);
    return /\.(?:ts|tsx)$/.test(entry.name) ? [entryPath] : [];
  });
}

function functionRecords(filePath) {
  const relativePath = path.relative(managerRoot, filePath).replaceAll("\\", "/");
  const source = fs.readFileSync(filePath, "utf8");
  const sourceFile = ts.createSourceFile(filePath, source, ts.ScriptTarget.Latest, true, filePath.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const records = [];
  const addRecord = (name, node) => {
    const start = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
    const end = sourceFile.getLineAndCharacterOfPosition(node.end).line + 1;
    records.push({ relativePath, name, start, lineCount: end - start + 1 });
  };
  for (const statement of sourceFile.statements) {
    if (ts.isFunctionDeclaration(statement) && statement.name) addRecord(statement.name.text, statement);
    if (!ts.isVariableStatement(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (!ts.isIdentifier(declaration.name) || !declaration.initializer) continue;
      if (ts.isArrowFunction(declaration.initializer) || ts.isFunctionExpression(declaration.initializer)) addRecord(declaration.name.text, declaration.initializer);
    }
  }
  return records;
}

const violations = [];
for (const filePath of sourceFiles(sourceRoot)) {
  for (const record of functionRecords(filePath)) {
    const budget = legacyFunctionBudgets.get(`${record.relativePath}:${record.name}`) ?? defaultFunctionLineBudget;
    if (record.lineCount > budget) violations.push(`${record.relativePath}:${record.start} ${record.name} has ${record.lineCount} lines; ratchet budget is ${budget}`);
  }
}

if (violations.length) {
  for (const violation of violations) process.stderr.write(`FAIL: ${violation}\n`);
  process.exitCode = 1;
} else {
  process.stdout.write("TypeScript maintainability ratchet PASS\n");
}
