# Codex Home Manager Source Monorepo

The GitHub `source` branch is a single repository containing every tracked source file needed to build, test, and audit Codex Home Manager:

```text
README.md
SOURCE_COMMITS.json
scripts/
codex_home_manager/
```

- `scripts/` contains the shared diagnostics, guarded repair, history audit, plugin repair, restart validation, source export, and source verification code used by the product.
- `codex_home_manager/` contains the React application, local connector backend, packaging code, quality gate, and product tests.
- `SOURCE_COMMITS.json` records the two source commits plus the Git object, byte count, mode, and SHA-256 of every exported file.

The source exporter reads committed Git blobs rather than copying an arbitrary working tree. It requires both source repositories to be clean, exports only root `README.md` plus root `scripts/` and all manager-tracked files, and refuses any existing output path.

## Export a source tree

Run this from the repair workspace root. Always choose a new output directory; the exporter never deletes, cleans, merges, or overwrites an earlier directory.

```powershell
$sourceOutput = ".tmp\codex-home-manager-source-20260715-001"
python .\scripts\export_codex_home_manager_source.py export `
  --root-repository . `
  --manager-repository .\codex_home_manager `
  --output $sourceOutput
```

The command fails before creating the output if either repository is dirty or a required source file is not tracked. A partial directory left by an I/O failure is not reused; inspect it and select another new output path for the next run.

## Verify an export or clone

Install the locked Python runtime dependencies in the selected Python environment, then run the verifier from the pristine export. The verifier checks the exact file inventory and every SHA-256 before it imports the exported Python modules, parses every exported PowerShell file, installs Node packages inside the export, and runs the exported core quality gate.

```powershell
python -m pip install --require-hashes --only-binary=:all: `
  -r "$sourceOutput\codex_home_manager\packaging\windows\requirements-connector.txt"

python "$sourceOutput\scripts\export_codex_home_manager_source.py" verify `
  --source $sourceOutput `
  --install-node-dependencies `
  --run-gate
```

The same check applies after an independent clone:

```powershell
git clone --branch source --single-branch `
  https://github.com/SimpleZion/codex-home-manager.git `
  codex-home-manager-source
cd codex-home-manager-source
python -m pip install --require-hashes --only-binary=:all: `
  -r .\codex_home_manager\packaging\windows\requirements-connector.txt
python .\scripts\export_codex_home_manager_source.py verify `
  --source . `
  --install-node-dependencies `
  --run-gate
```

Publication should use the verified new export as the complete branch tree. Initialize or clone only inside that new directory, review `git status`, and update the remote `source` branch with an explicit force-with-lease bound to the previously observed remote commit. Do not synchronize the export into either source working directory.

The hosted Cloudflare site remains a static frontend. Local Codex data stays on the user's machine and is read only through the loopback connector after browser authorization.
