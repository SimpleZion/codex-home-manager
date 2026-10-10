import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { Zip, ZipPassThrough, zipSync } from "fflate";
import { validateDesktopRelease } from "./check-desktop-release.mjs";

const digest = (value) => createHash("sha256").update(value).digest("hex");
const fixtureDirectory = process.env.RUNNER_TEMP
  ? join(process.env.RUNNER_TEMP, "cockpit-public-tests")
  : fileURLToPath(new URL("../.tmp/public-tests/", import.meta.url));
async function fixture() {
  await mkdir(fixtureDirectory, { recursive: true });
  const site = await mkdtemp(join(fixtureDirectory, "desktop-"));
  const sourceManifest = Buffer.from(JSON.stringify({ schemaVersion: 2, sources: {
    rootRepository: {files: []}, managerRepository: {files: []},
  }}));
  const archive = zipSync({"SOURCE_COMMITS.json": sourceManifest});
  await writeFile(join(site, "codex-home-manager-source.zip"), archive);
  const artifacts = [{name:"app.exe",kind:"exe",sha256:"a".repeat(64),size:1}, {name:"app.zip",kind:"zip",sha256:"b".repeat(64),size:2}];
  const bundle = {schemaVersion:2,version:"1.0.34",flavor:"tauri-desktop",artifacts};
  const bundleBytes = Buffer.from(JSON.stringify(bundle));
  await writeFile(join(site,"connector-release.json"), bundleBytes);
  const sourceRecord = {path:"codex-home-manager-source.zip",sha256:digest(archive),size:archive.length};
  const receipt = {schemaVersion:2,productName:"Codex Cockpit",version:"1.0.34",installerSha256:artifacts[0].sha256};
  for (const key of ["catalogSha256","frontendManifestSha256","legacySha256","sourceFingerprint","resourcesFingerprint"]) receipt[key]="c".repeat(64);
  await mkdir(join(site,"assets"));
  const frontendBody = Buffer.from("fixture frontend");
  await writeFile(join(site,"index.html"),frontendBody);
  const artifactDigest = createHash("sha256").update("index.html\0").update(frontendBody).update("\0").digest("hex");
  const frontend = Buffer.from(JSON.stringify({schemaVersion:2,files:["index.html"],artifactDigest}));
  await writeFile(join(site,"frontend-release-manifest.json"),frontend);
  receipt.frontendManifestSha256=digest(frontend);
  const manifest = {product:"Codex Cockpit",version:"1.0.34",bundle_sha256:digest(bundleBytes),
    desktop_build:{method:"publisher-local-tauri-nsis",github_built:false,portable:false,source_snapshot_after_build:true,receipt},
    source_validation:{method:"isolated-local-full-gate",source_ref:"refs/heads/source",source_commit:"d".repeat(40),
      manifest_sha256:digest(sourceManifest),quality:{status:"passed",sourceManifestSha256:digest(sourceManifest)},archive:sourceRecord},
    public_artifacts:[...artifacts.map((item)=>({path:item.name,sha256:item.sha256,size:item.size})),sourceRecord,{path:"verify-codex-home-manager.ps1",sha256:"e".repeat(64),size:1}]};
  return {site,manifest,bundle};
}

test("desktop provenance accepts explicitly local nonportable build",async()=>{ const value=await fixture(); await validateDesktopRelease(value.manifest,value.bundle,value.site); });
test("desktop provenance rejects borrowed CI claim",async()=>{ const value=await fixture(); value.manifest.desktop_build.github_built=true; await assert.rejects(()=>validateDesktopRelease(value.manifest,value.bundle,value.site),/provenance/); });
test("desktop provenance rejects a failed source gate",async()=>{ const value=await fixture(); value.manifest.source_validation.quality.status="failed"; await assert.rejects(()=>validateDesktopRelease(value.manifest,value.bundle,value.site),/validation/); });
test("desktop provenance rejects unsigned bundle mutation",async()=>{ const value=await fixture(); await writeFile(join(value.site,"connector-release.json"),"{}"); await assert.rejects(()=>validateDesktopRelease(value.manifest,value.bundle,value.site),/digest/); });
test("desktop provenance rejects extra archive files",async()=>{ const value=await fixture(); await writeFile(join(value.site,"codex-home-manager-source.zip"),zipSync({"SOURCE_COMMITS.json":Buffer.from(JSON.stringify({schemaVersion:2,sources:{rootRepository:{files:[]},managerRepository:{files:[]}}})),"secret.key":Buffer.from("secret")})); await assert.rejects(()=>validateDesktopRelease(value.manifest,value.bundle,value.site),/unlisted/); });
test("desktop provenance rejects a different frozen frontend",async()=>{ const value=await fixture(); value.manifest.desktop_build.receipt.frontendManifestSha256="f".repeat(64); await assert.rejects(()=>validateDesktopRelease(value.manifest,value.bundle,value.site),/frontend/); });
test("desktop provenance rejects mutated frontend bytes with an unchanged receipt",async()=>{ const value=await fixture(); await writeFile(join(value.site,"index.html"),"mutated"); await assert.rejects(()=>validateDesktopRelease(value.manifest,value.bundle,value.site),/frontend resource bytes/); });
test("desktop provenance rejects extra frontend resources",async()=>{ const value=await fixture(); await writeFile(join(value.site,"assets","extra.js"),"extra"); await assert.rejects(()=>validateDesktopRelease(value.manifest,value.bundle,value.site),/frontend file set/); });
test("desktop provenance rejects duplicate archive names",async()=>{
  const value=await fixture(); const chunks=[];
  const archive=new Zip((error,chunk)=>{ if(error)throw error; chunks.push(Buffer.from(chunk)); });
  for(const content of [Buffer.from("unlisted payload"),Buffer.from(JSON.stringify({schemaVersion:2,sources:{rootRepository:{files:[]},managerRepository:{files:[]}}}))]) {
    const file=new ZipPassThrough("SOURCE_COMMITS.json"); archive.add(file); file.push(content,true);
  }
  archive.end(); await writeFile(join(value.site,"codex-home-manager-source.zip"),Buffer.concat(chunks));
  await assert.rejects(()=>validateDesktopRelease(value.manifest,value.bundle,value.site),/duplicate/);
});
