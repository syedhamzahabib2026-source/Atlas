# Atlas Mobile Runtime

Operational infrastructure for **AssignMint React Native iOS** workflows. Extends Atlas without changing core orchestration logic.

Last updated: 2026-05-21  
**Current mobile phase: 17 (mobile verification workflows)**

---

## Phase 14 — Simulator screenshot infrastructure

### Module

`tools/mobile/simulator_manager.py` — `SimulatorManager`

| Method | Purpose |
|--------|---------|
| `list_devices()` | Available simulators (`simctl list devices available -j`) |
| `list_booted()` | Currently booted devices |
| `resolve_device(name \| udid)` | Resolve device; newest iOS runtime on name collision |
| `boot(device, open_app=…)` | Boot by name or UDID |
| `open_simulator_app(udid?)` | `open -a Simulator` |
| `capture_screenshot(device?, path?, label?)` | `simctl io … screenshot` → `logs/screenshots/mobile/` |
| `shutdown(device?, all_devices=…)` | Shutdown one, all booted, or `shutdown all` |

### Results

Structured dataclasses for future **RecoveryEngine** / verification attachment:

- `SimulatorOperationResult` — base; `to_dict()` for task metadata
- `ScreenshotResult` — includes `path`

### Tool registry

Registered on `--mobile-demo` (and callable via `tools.registry`):

- `mobile.list_simulators`
- `mobile.list_booted`
- `mobile.screenshot`

### Demo

```bash
python main.py --mobile-demo
python main.py --mobile-demo --simulator-device "iPhone 17 Pro"
python main.py --mobile-demo --simulator-device <UDID>
```

Flow: list → boot → screenshot → shutdown.

### Requirements

- macOS with Xcode Command Line Tools
- `xcrun simctl` on PATH
- Simulator.app (opened automatically when `open_app=True`)

---

## Phase 15 — Metro bundler orchestration

### Module

`tools/mobile/metro_manager.py` — `MetroManager`

| Method | Purpose |
|--------|---------|
| `detect_running()` / `is_running()` | Port 8081 + `GET /status` (`packager-status:running`) |
| `get_runtime_state()` | Snapshot: pid, state, failures, log count |
| `start(reset_cache?, wait_until_ready?)` | `npm run start` in AssignMint root |
| `stop(force?)` | SIGTERM process group + release port |
| `restart(reset_cache?)` | Clean stop → start |
| `get_logs(tail_lines)` | Ring buffer + on-disk log file |
| `detect_failures(lines?)` | Regex signatures (module resolve, EADDRINUSE, transform, …) |

### Config

`configs/mobile.yaml` — AssignMint `project_root`, `metro_port`, `primary_simulator` (`iPhone 16 Pro`).

### Tool registry

- `mobile.metro.detect`
- `mobile.metro.start` / `stop` / `restart`
- `mobile.metro.logs`
- `mobile.metro.runtime_state`

### Demo

```bash
python main.py --metro-demo
python main.py --metro-demo --metro-reset-cache
```

Flow: detect → start → tail logs → failure scan → stop.  
Logs: `logs/mobile/metro/metro-*.log`

### Requirements

- macOS, Node/npm, AssignMint at `configs/mobile.yaml` `project_root`
- Default port **8081** (`RCT_METRO_PORT` / `PORT` set on spawn)

---

## Phase 16 — Xcode / React Native iOS launch

### Module

`tools/mobile/xcode_manager.py` — `XcodeManager`

| Method | Purpose |
|--------|---------|
| `launch_ios()` | `npx react-native run-ios --no-packager` (Metro managed separately) |
| `get_logs()` | Ring buffer + `logs/mobile/xcode/run-ios-*.log` |
| `detect_failures()` | Signing, pods, Metro, simulator, bundle, compile errors |

### `IOSLaunchSummary` fields

`build_success`, `install_success`, `simulator_connected`, `app_launch_detected`, `launch_duration_sec`, `failures[]`, `lifecycle` — all `.to_dict()` for RecoveryEngine.

### Lifecycle markers (log-driven)

`build_started` → `build_succeeded` / `build_failed` → `install_completed` → `app_launch_detected`

### Tool registry

- `mobile.ios.launch`

### Demo (full mobile stack)

```bash
python main.py --ios-demo
```

Flow: boot **iPhone 16 Pro** → ensure Metro :8081 → `run-ios` → print summary.  
Leaves simulator + Metro running for manual testing.

### Config (`configs/mobile.yaml`)

`xcode_scheme`, `bundle_id`, `ios_build_timeout_sec` (default 900s).

---

## Phase 17 — Mobile verification workflows

### Module

`tools/mobile/mobile_workflows.py` — `MobileWorkflows`

| Workflow | What it checks |
|----------|----------------|
| `verify_ios_launch` | Simulator + Metro + app launch + stability + screenshot |
| `capture_home_screen` | Screenshot + runtime/metro health metadata |
| `verify_login_screen` | Cold launch → login log markers + screenshot + no RN errors |
| `verify_dashboard_layout` | Main-tabs log markers + screenshot (needs guest/auth session) |

### `MobileVerificationResult`

`success`, `screenshots[]`, `runtime_health`, `metro_health`, `simulator_info`, `detected_failures`, `diagnostics`, timestamps — `.to_dict()` for RecoveryEngine.

Login/dashboard checks use **Metro console log markers** (e.g. `shouldShowAuth`, `MainTabs`) — no vision, no UI automation.

### Demo

```bash
python main.py --mobile-verify-demo
```

Critical pass: `verify_ios_launch`, `verify_login_screen`.  
`verify_dashboard_layout` may fail without an authenticated/guest session (reported as optional).

Screenshots: `logs/screenshots/mobile/verify/`

### Registry

- `mobile.verify.ios_launch`
- `mobile.verify.login_screen`

---

## Roadmap (mobile)

| Step | Status |
|------|--------|
| Simulator screenshots | **Phase 14** |
| Metro bundler lifecycle | **Phase 15** |
| iOS app launch (`run-ios`) | **Phase 16** |
| Verification workflows | **Phase 17** |
| Vision / UI automation | Planned |
| RecoveryEngine mobile evidence | Planned |
| Slack `/atlas mobile` commands | Planned |

See [ATLAS_PHASES.md](ATLAS_PHASES.md) for the global phase index.
