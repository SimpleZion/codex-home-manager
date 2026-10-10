import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const projectRoot = path.resolve(import.meta.dirname, "..");
const origin = (process.env.COCKPIT_TEST_URL || "http://127.0.0.1:8765").replace(/\/$/, "");
const outputRoot = process.env.COCKPIT_TEST_OUTPUT || path.join(projectRoot, "output", "cockpit-tests", `maintenance-${Date.now()}`);
let service = null;

async function serviceReady() {
  try {
    const response = await fetch(`${origin}/api/capabilities`, { signal: AbortSignal.timeout(1500) });
    return response.ok;
  } catch {
    return false;
  }
}

async function waitForService() {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (await serviceReady()) return;
    if (service?.exitCode !== null) throw new Error(`Cockpit service exited with code ${service?.exitCode}`);
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Cockpit service did not become ready at ${origin}`);
}

function runNodeTest(testFile, outputDirectory) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [testFile], {
      cwd: projectRoot,
      env: {
        ...process.env,
        COCKPIT_TEST_URL: origin,
        COCKPIT_TEST_OUTPUT: outputDirectory,
        COCKPIT_SOURCE_WORKSPACE: path.resolve(projectRoot, "..")
      },
      stdio: "inherit"
    });
    child.once("error", reject);
    child.once("exit", (code) => code === 0 ? resolve() : reject(new Error(`${testFile} failed with code ${code}`)));
  });
}

try {
  await fs.mkdir(outputRoot, { recursive: true });
  if (!await serviceReady()) {
    const port = new URL(origin).port || "8765";
    service = spawn(process.env.PYTHON || "python", ["-m", "uvicorn", "backend.server:app", "--host", "127.0.0.1", "--port", port], {
      cwd: projectRoot,
      env: process.env,
      stdio: ["ignore", "inherit", "inherit"]
    });
    await waitForService();
  }
  await runNodeTest("tests/maintenance_center_ui.test.mjs", path.join(outputRoot, "functional"));
  await runNodeTest("tests/maintenance_visual_layout.test.mjs", path.join(outputRoot, "layout"));
  console.log(JSON.stringify({ status: "passed", origin, output: outputRoot }));
} finally {
  if (service && service.exitCode === null) service.kill();
}
