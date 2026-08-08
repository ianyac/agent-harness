# Tauri macOS Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the verified React client and local Python service as a one-click, signed, notarized macOS application with native workspace selection and reliable sidecar lifecycle.

**Architecture:** Tauri 2 hosts the existing frontend and owns a target-specific PyInstaller sidecar. Rust generates the launch secret, writes bootstrap JSON to child stdin, waits for a readiness record on stdout, exposes only the in-memory connection through a narrow command, and stops or restarts the child deterministically.

**Tech Stack:** Tauri 2, Rust, `tauri-plugin-shell`, `tauri-plugin-dialog`, `tauri-plugin-notification`, TypeScript platform adapter, PyInstaller, macOS codesign/notary tooling

## Global Constraints

- Begin only after the local-service and web-client completion gates pass.
- The desktop app embeds the same `ui/frontend/dist` build; do not fork React components or CSS.
- Distribute directly as signed and notarized macOS artifacts; Mac App Store sandboxing is outside v1.
- Build native `aarch64-apple-darwin` and `x86_64-apple-darwin` sidecars on matching macOS runners; do not cross-compile PyInstaller output.
- Never expose the launch secret in argv, stdout, logs, URLs, local storage, or crash reports.
- Grant Tauri capabilities only for the bundled sidecar and the exact native commands used by the platform adapter.
- Restart the sidecar at most once after an unexpected exit; a second exit stays on the recovery screen.
- Do not modify `.github/`; document the exact CI handoff in the UI mailbox.
- Every implementation task follows red → green → focused commit.

## File Structure

```text
ui/
├── desktop/
│   ├── app-icon-source.png
│   └── src-tauri/
│       ├── Cargo.toml
│       ├── Cargo.lock
│       ├── build.rs
│       ├── tauri.conf.json
│       ├── capabilities/default.json
│       ├── icons/
│       └── src/
│           ├── main.rs
│           ├── lib.rs
│           ├── commands.rs
│           ├── lifecycle.rs
│           ├── readiness.rs
│           └── state.rs
├── packaging/
│   ├── sidecar_entry.py
│   ├── agent_harness_sidecar.spec
│   ├── build-sidecar.sh
│   ├── verify-sidecar.sh
│   └── smoke-packaged-app.sh
└── frontend/
    └── src/platform/tauri.ts
```

---

### Task 1: Scaffold the minimal Tauri host and capability boundary

**Files:**
- Create: `ui/desktop/src-tauri/Cargo.toml`
- Create: `ui/desktop/src-tauri/build.rs`
- Create: `ui/desktop/src-tauri/tauri.conf.json`
- Create: `ui/desktop/src-tauri/capabilities/default.json`
- Create: `ui/desktop/src-tauri/src/main.rs`
- Create: `ui/desktop/src-tauri/src/lib.rs`
- Modify: `ui/frontend/package.json`
- Modify: `ui/frontend/package-lock.json`

**Interfaces:**
- Consumes: `ui/frontend/dist` and Vite dev server
- Produces: `npm run tauri:dev`, `npm run tauri:build`, one main window, minimal capabilities

- [ ] **Step 1: Add the Tauri scripts and verify the host is absent**

Add the Tauri CLI to the frontend as a development dependency; Plan 2 already
installed `@tauri-apps/api` for the shared platform adapter. Add scripts:

```json
{
  "tauri:dev": "tauri dev --config ../desktop/src-tauri/tauri.conf.json",
  "tauri:build": "tauri build --config ../desktop/src-tauri/tauri.conf.json"
}
```

Run: `cd ui/frontend && npm run tauri:build`

Expected: FAIL because the Rust host does not exist.

- [ ] **Step 2: Create the Rust host and exact capabilities**

Use a library entrypoint so Rust unit tests can import modules:

```rust
// ui/desktop/src-tauri/src/main.rs
fn main() {
    agent_harness_desktop::run();
}
```

`Cargo.toml` includes Tauri plus the shell, dialog, notification, and opener
plugins, `serde`, `serde_json`, `tokio`, `base64`, and `rand`. Generate the
secret with `let mut bytes = [0_u8; 32]; rand::rng().fill_bytes(&mut bytes);`
through `RngCore`, then encode with
`base64::engine::general_purpose::URL_SAFE_NO_PAD`. Initialize each
plugin in `lib.rs`; their webview commands remain disabled by capability policy
because only Rust calls them.

`tauri.conf.json` points `frontendDist` to `../../frontend/dist`, uses the
Vite dev URL, creates one initially hidden window titled `Harness`, and applies
a CSP whose `connect-src` permits only its own origin plus loopback HTTP and
WebSocket ports. Task 5 adds `bundle.externalBin` after the binary exists so
the scaffold build does not depend on an absent artifact.

`capabilities/default.json` grants only `core:default` to the main webview. Rust
owns sidecar, dialog, notification, and opener operations; the webview receives
no shell execute/spawn, filesystem wildcard, HTTP-plugin, or opener capability.

- [ ] **Step 3: Run Rust checks and a frontend production build**

Run: `cd ui/desktop/src-tauri && cargo test && cargo clippy --all-targets -- -D warnings`

Expected: PASS.

Run: `cd ui/frontend && npm run build`

Expected: PASS.

- [ ] **Step 4: Commit the Tauri scaffold**

```bash
git add ui/desktop/src-tauri ui/frontend/package.json ui/frontend/package-lock.json
git commit -m "ui: scaffold minimal Tauri host"
```

### Task 2: Parse readiness and own the sidecar lifecycle

**Files:**
- Create: `ui/desktop/src-tauri/src/readiness.rs`
- Create: `ui/desktop/src-tauri/src/state.rs`
- Create: `ui/desktop/src-tauri/src/lifecycle.rs`
- Modify: `ui/desktop/src-tauri/src/lib.rs`

**Interfaces:**
- Consumes: bundled sidecar process and readiness JSON on stdout
- Produces: `ServiceConnection`, `ServiceLifecycle::start/restart_once/shutdown`

- [ ] **Step 1: Write failing Rust unit tests**

```rust
// ui/desktop/src-tauri/src/readiness.rs
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_ready_record_without_accepting_extra_fields() {
        let record = parse_ready(
            br#"{"type":"server-ready","host":"127.0.0.1","port":49152}"#,
        )
        .unwrap();
        assert_eq!(record.port, 49152);
        assert!(parse_ready(
            br#"{"type":"server-ready","host":"0.0.0.0","port":49152}"#,
        )
        .is_err());
    }
}
```

Add tests for malformed JSON, wrong type, non-loopback host, port zero,
readiness timeout, clean shutdown, one restart, and refusal of a second restart.
Use an injected `SidecarSpawner` trait and fake child; unit tests must not launch
Python.

- [ ] **Step 2: Run lifecycle tests and verify failure**

Run: `cd ui/desktop/src-tauri && cargo test readiness lifecycle`

Expected: FAIL because the modules do not exist.

- [ ] **Step 3: Implement sidecar bootstrap and state**

```rust
#[derive(Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ServiceConnection {
    pub base_url: String,
    pub token: String,
}
```

Generate the token with the operating system random source and encode 32 bytes
as URL-safe base64. Spawn the named sidecar through `tauri_plugin_shell`, write
one line to child stdin:

```json
{"type":"bootstrap","secret":"generated-in-rust","workspace":"/canonical/path"}
```

Accumulate stdout only until one newline, cap it at 8 KiB, parse the exact ready
record, then stop treating stdout as protocol. Never log stdin payloads. Enforce
a 15-second readiness deadline. Store child handle, connection, restart count,
and state enum behind `tokio::sync::Mutex`.

On normal exit call child kill only if still running and await the terminal
event. On unexpected exit emit `service-state` to the webview, restart once,
and emit the new in-memory connection. A second exit emits `failed` and retains
the diagnostic log path.

- [ ] **Step 4: Run lifecycle tests and clippy**

Run: `cd ui/desktop/src-tauri && cargo test && cargo clippy --all-targets -- -D warnings`

Expected: PASS.

- [ ] **Step 5: Commit sidecar lifecycle**

```bash
git add ui/desktop/src-tauri/src
git commit -m "ui: manage Tauri sidecar lifecycle"
```

### Task 3: Expose narrow native commands and complete the Tauri adapter

**Files:**
- Create: `ui/desktop/src-tauri/src/commands.rs`
- Modify: `ui/desktop/src-tauri/src/lib.rs`
- Modify: `ui/desktop/src-tauri/capabilities/default.json`
- Modify: `ui/frontend/src/platform/tauri.ts`
- Create: `ui/frontend/src/platform/tauri.test.ts`

**Interfaces:**
- Consumes: `ServiceLifecycle`, native folder dialog, notification permission
- Produces: `service_connection`, `choose_workspace`, `notify`, `open_logs`, `restart_service`, `quit_app`

- [ ] **Step 1: Write failing frontend adapter tests**

```ts
it("requests connection details through the narrow Rust command", async () => {
  invoke.mockResolvedValueOnce({ baseUrl: "http://127.0.0.1:49152", token: "s" });
  await expect(tauriPlatform.getServiceConnection()).resolves.toEqual({
    baseUrl: "http://127.0.0.1:49152",
    token: "s",
  });
  expect(invoke).toHaveBeenCalledWith("service_connection");
});
```

Add tests for cancelled folder selection, canonical path return, denied
notification permission, service-state events, open logs, restart, and quit.

- [ ] **Step 2: Run adapter tests and verify failure**

Run: `cd ui/frontend && npm test -- --run src/platform/tauri.test.ts`

Expected: FAIL because the commands are not implemented.

- [ ] **Step 3: Implement Rust commands and frontend calls**

`service_connection` clones the in-memory connection; it never serializes to
disk. `choose_workspace` opens a directory-only dialog, canonicalizes the
selection, verifies it remains a directory, and returns `Option<String>`.
`notify` accepts plain title/body with maximum lengths and uses the notification
plugin. `open_logs` reveals only the application log directory through the
opener plugin's Rust API, not a general frontend path. `restart_service` is available
only in failed state. `quit_app` requests a normal Tauri exit so shutdown hooks
run.

Register commands explicitly with `generate_handler!`. Update capabilities for
only plugin operations that remain in JavaScript; Rust commands do not need
frontend filesystem permission.

- [ ] **Step 4: Run Rust and frontend tests**

Run: `cd ui/desktop/src-tauri && cargo test && cargo clippy --all-targets -- -D warnings`

Expected: PASS.

Run: `cd ui/frontend && npm test -- --run src/platform && npm run typecheck`

Expected: PASS.

- [ ] **Step 5: Commit native integration**

```bash
git add ui/desktop/src-tauri ui/frontend/src/platform
git commit -m "ui: bridge native macOS capabilities"
```

### Task 4: Create the approved app icon and native window polish

**Files:**
- Create: `ui/desktop/app-icon-source.png`
- Generate: `ui/desktop/src-tauri/icons/*`
- Modify: `ui/desktop/src-tauri/tauri.conf.json`
- Modify: `ui/desktop/src-tauri/src/lib.rs`
- Modify: `ui/frontend/src/styles/global.css`

**Interfaces:**
- Consumes: approved warm-neutral visual system and `H` product mark
- Produces: complete macOS icon set, menus, hidden-until-ready window behavior

- [ ] **Step 1: Generate and inspect the source icon**

Invoke the `imagegen` skill with this art direction:

```text
Create a 1024×1024 macOS application icon for Harness. Warm off-white rounded
square surface, centered charcoal H monogram built from clean geometric strokes,
subtle moss-green operational accent, restrained depth, no text beyond the H,
no gradients that reduce small-size legibility, no terminal prompt imagery.
```

Save the selected result as `ui/desktop/app-icon-source.png`. Inspect it at
1024, 128, 32, and 16 px; reject it if the H closes up or the accent disappears.

- [ ] **Step 2: Generate the Tauri icon set and verify required sizes**

Run: `cd ui/frontend && npx tauri icon ../desktop/app-icon-source.png --output ../desktop/src-tauri/icons`

Expected: exit 0 and generated `.icns` plus PNG sizes referenced by
`tauri.conf.json`.

- [ ] **Step 3: Add native menu and readiness polish**

Create macOS menu items for New Chat (`Command+N`), Command Palette
(`Command+K`), Toggle Activity (`Command+Shift+I`), Settings, Hide, and Quit.
Emit stable menu ids to the frontend; use the same command registry as browser
shortcuts. Keep the window hidden until sidecar health succeeds, then center
and show it. Restore the last valid size/position while ensuring part of the
window intersects a current display.

- [ ] **Step 4: Run the host and frontend checks**

Run: `cd ui/desktop/src-tauri && cargo test && cargo clippy --all-targets -- -D warnings`

Expected: PASS.

Run: `cd ui/frontend && npm test -- --run && npm run build`

Expected: PASS.

- [ ] **Step 5: Commit native polish**

```bash
git add ui/desktop ui/frontend/src/styles/global.css
git commit -m "ui: polish native Harness window"
```

### Task 5: Package the Python service as a target-specific sidecar

**Files:**
- Create: `ui/packaging/sidecar_entry.py`
- Create: `ui/packaging/agent_harness_sidecar.spec`
- Create: `ui/packaging/build-sidecar.sh`
- Create: `ui/packaging/verify-sidecar.sh`
- Modify: `ui/pyproject.toml`
- Modify: `ui/uv.lock`
- Modify: `ui/desktop/src-tauri/tauri.conf.json`
- Create: `ui/tests/test_packaged_paths.py`

**Interfaces:**
- Consumes: Python service, harness package, vendored tiktoken data, host target triple
- Produces: `ui/desktop/src-tauri/binaries/agent-harness-sidecar-$TARGET_TRIPLE`

- [ ] **Step 1: Write failing frozen-path tests**

```python
# ui/tests/test_packaged_paths.py
from pathlib import Path

from server.static import resource_root


def test_frozen_resource_root_uses_meipass(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)
    assert resource_root() == tmp_path
```

Add tests that the packaged cache path resolves, the entrypoint reads bootstrap
from stdin, and readiness output omits the secret.

- [ ] **Step 2: Run packaged-path tests and verify failure**

Run: `cd ui && uv run pytest tests/test_packaged_paths.py -v`

Expected: FAIL until frozen resource resolution exists.

- [ ] **Step 3: Implement the PyInstaller entrypoint and spec**

Add PyInstaller to the packaging dependency group. `sidecar_entry.py` imports
`server._paths` before `server.__main__` and always selects secret-from-stdin
mode. The spec builds a one-file console-enabled binary so stdin/stdout remain
the bootstrap channel, and collects:

- `server` and `harness` modules;
- FastAPI, Uvicorn, HTTPX, tiktoken, requests, and Pydantic hidden imports;
- `vendor/tiktoken` under a stable resource path; and
- `ui/frontend/dist` under `frontend/dist`.

Do not collect `.codex/auth.json`, workspace files, test fixtures, `.agent`
sessions, or environment values.

`build-sidecar.sh` obtains `rustc --print host-tuple`, refuses a target other
than the current host, runs PyInstaller through `uv`, and copies the result to
the exact Tauri suffix. `verify-sidecar.sh` sends a temporary bootstrap record,
parses readiness, calls authenticated health, sends SIGTERM, and verifies no
process remains.

- [ ] **Step 4: Build and verify the native sidecar**

Run: `cd ui && bash packaging/build-sidecar.sh && bash packaging/verify-sidecar.sh`

Expected: both commands exit 0; `file` reports the current host architecture;
`otool -L` shows no Homebrew or local Python path.

- [ ] **Step 5: Run service tests**

Run: `cd ui && uv run pytest -v`

Expected: PASS.

- [ ] **Step 6: Commit sidecar packaging**

```bash
git add ui/packaging ui/pyproject.toml ui/uv.lock ui/desktop/src-tauri/tauri.conf.json ui/tests/test_packaged_paths.py
git commit -m "ui: package Python service as sidecar"
```

### Task 6: Verify crash recovery and packaged application behavior

**Files:**
- Create: `ui/packaging/smoke-packaged-app.sh`
- Create: `ui/frontend/e2e/tauri-recovery.spec.ts`
- Modify: `ui/desktop/src-tauri/src/lifecycle.rs`
- Modify: `ui/frontend/src/components/RecoveryView.tsx`
- Modify: `ui/README.md`

**Interfaces:**
- Consumes: debug/release Tauri bundle and controllable fake sidecar exit
- Produces: deterministic launch, one restart, stable second-failure recovery, clean shutdown

- [ ] **Step 1: Add failing packaged smoke assertions**

The smoke script launches the built `.app`, waits for the main window and
health, creates a fake session, kills the sidecar, verifies exactly one new
sidecar pid and restored session, kills it again, verifies the stable recovery
view, invokes Quit, and checks that no sidecar pid remains.

The Playwright/WebDriver test asserts accessible recovery actions Restart
service, Open logs, and Quit, plus restored active session after the first
restart.

- [ ] **Step 2: Run recovery tests and capture failures**

Run: `cd ui/frontend && npm run e2e -- tauri-recovery.spec.ts`

Expected: FAIL until the debug host exposes the controlled-exit fixture.

- [ ] **Step 3: Complete recovery behavior**

Add a debug-only test command compiled behind `cfg(debug_assertions)` that asks
the child to exit. Production capabilities never expose it. Reset restart count
only after a new app launch, not after sidecar uptime. Persist no launch token
across restart; generate a new one and force the frontend socket to reconnect
from a fresh snapshot.

Write sidecar stderr to the platform log directory with launch secrets and
Authorization headers redacted. Cap each file at 5 MiB and retain three files.

- [ ] **Step 4: Run packaged recovery and all local suites**

Run: `cd ui && bash packaging/smoke-packaged-app.sh`

Expected: PASS with no orphan process.

Run: `cd ui/frontend && npm test -- --run && npm run typecheck && npm run build && npm run e2e`

Expected: PASS.

Run: `cd ui && uv run pytest -v`

Expected: PASS.

- [ ] **Step 5: Commit packaged recovery**

```bash
git add ui/packaging/smoke-packaged-app.sh ui/frontend/e2e/tauri-recovery.spec.ts ui/desktop/src-tauri/src/lifecycle.rs ui/frontend/src/components/RecoveryView.tsx ui/README.md
git commit -m "ui: recover packaged sidecar safely"
```

### Task 7: Add signing, notarization, architecture verification, and CI handoff

**Files:**
- Create: `ui/packaging/build-release.sh`
- Create: `ui/packaging/verify-release.sh`
- Create: `docs/streams/ui/2026-08-08-macos-release-ci-request.md`
- Modify: `ui/README.md`

**Interfaces:**
- Consumes: Apple signing identity, notary profile, native sidecar and Tauri build
- Produces: verified aarch64/x86_64 `.dmg` artifacts and exact overseer CI request

- [ ] **Step 1: Implement release verification before signing**

`verify-release.sh` must fail unless all of these are true:

```text
codesign --verify --deep --strict --verbose=2 Harness.app
spctl --assess --type execute --verbose=4 Harness.app
xcrun stapler validate Harness.app
file Harness.app/Contents/MacOS/Harness
file Harness.app/Contents/MacOS/agent-harness-sidecar
otool -L Harness.app/Contents/MacOS/agent-harness-sidecar
```

It rejects Homebrew, `.venv`, repository, or user-home paths in Mach-O linkage
and bundle strings. It launches the app once and checks clean sidecar shutdown.

- [ ] **Step 2: Implement the release build script**

`build-release.sh` validates `APPLE_SIGNING_IDENTITY` and
`APPLE_NOTARY_PROFILE` without printing them, builds and verifies the native
sidecar, builds the frontend, runs Tauri release build, signs nested binaries
from inside out, notarizes with `xcrun notarytool --keychain-profile`, staples,
and calls `verify-release.sh`. Refuse cross-architecture invocation.

- [ ] **Step 3: Write the owner-routed CI request**

The mailbox note asks the overseer for two native macOS jobs, one per target,
that run service/frontend/Rust tests, sidecar build, Tauri build, signing,
notarization, verification, and artifact upload. It lists required secret names
without secret values and requires artifacts to be built only from protected
release refs. It explicitly says the UI lane will not edit `.github/`.

- [ ] **Step 4: Run unsigned verification locally and inspect the signed path guard**

Run: `cd ui && bash packaging/build-sidecar.sh && cd frontend && npm run tauri:build`

Expected: debug or unsigned release build completes on the current architecture.

Run: `cd ui && bash packaging/verify-release.sh --allow-unsigned-smoke`

Expected: functional and architecture checks pass; signature/notary checks are
reported as intentionally skipped only with this explicit local flag.

- [ ] **Step 5: Run the full repository checks**

Run: `cd ui && uv run pytest -v`

Expected: PASS.

Run: `cd ui/frontend && npm test -- --run && npm run typecheck && npm run build && npm run e2e`

Expected: PASS.

Run: `cd ui/desktop/src-tauri && cargo test && cargo clippy --all-targets -- -D warnings`

Expected: PASS.

Run: `uv run pytest`

Expected: the root harness suite passes.

- [ ] **Step 6: Commit release delivery**

```bash
git add ui/packaging ui/README.md docs/streams/ui/2026-08-08-macos-release-ci-request.md
git commit -m "ui: define verified macOS release"
```

## Plan 3 Completion Gate

The web/macOS v1 is implementation-complete only when:

1. The same production React build works in browser and Tauri.
2. macOS launches with no system Python or manually started service.
3. Sidecar readiness reveals the window only after authenticated health succeeds.
4. Folder selection, menus, notifications, logs, restart, and quit use narrow native commands.
5. One crash restarts and restores; a second crash remains stable; quit leaves no child.
6. Native aarch64 and x86_64 artifacts pass architecture and linkage checks.
7. Signed release artifacts pass codesign, Gatekeeper, notarization, staple, and smoke checks.
8. Python, React, browser, Rust, packaged smoke, and root harness suites pass.
9. The CI handoff is routed to the overseer without a `.github/` edit from the UI lane.
10. `git status --short` is clean.
