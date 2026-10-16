# RimCrow

> **Translation notice:** This English documentation was generated with AI assistance from the Chinese source docs. Some descriptions may be inaccurate or not fully aligned with the current UI. Corrections and improvement suggestions are welcome.

[中文](./README.md)

<img src="./doc/assets/Preview.png" style="width:50%;" />

RimCrow, formerly RimModManager, is a desktop mod manager for **RimWorld**. It brings mod scanning, load order management, backups, Workshop tools, Git-based mod sources, log troubleshooting, texture optimization, and AI-assisted diagnosis into one app.

![Main interface](./doc/assets/主界面.png)

## Documentation

Chinese docs are the primary source of truth. English docs are maintained as paired user-facing guides for the main workflows.

- First run: see [Quick Start](./doc/en/01-quick-start.md).
- Paths and launch: see [Profiles and Paths](./doc/en/02-environment-and-paths.md).
- Mod lists and load order: see [Mod List and Load Order](./doc/en/04-mod-list-and-load-order.md).
- Auto sort and rules: see [Auto Sort and Rules](./doc/en/05-auto-sort-and-rules.md).
- Workshop and download sources: see [Workshop and Download Sources](./doc/en/03-workshop-and-download-sources.md).
- Backup, import, and export: see [Backup, Import, and Export](./doc/en/06-backup-import-export.md).
- Logs and AI diagnosis: see [Logs and AI Diagnosis](./doc/en/08-logs-and-ai-diagnosis.md).
- Texture optimization: see [Texture Optimization](./doc/en/07-texture-optimization.md).

## Platform Status

| Platform | Status | Notes |
| --- | --- | --- |
| Windows | Primary platform | Desktop mode, packaging, and full workflows are mainly verified on Windows. WebView2 Runtime is required. |
| macOS | Basic path support | Basic path detection for RimWorld, Steam, user data, Player.log, and Steam launch paths is available. Packaging quality is not promised yet. |
| Linux | In progress | Some Steam path detection has been added, but desktop runtime, Steamworks, and packaging are not guaranteed yet. |

## Main Features

- Scan RimWorld mods from local folders, Workshop folders, and the manager library.
- Enable, disable, group, tag, color, annotate, and batch-edit mods.
- Manage load order with drag-and-drop, auto sort, rules, dependencies, interlocked mods, and issue hints.
- Maintain multiple RimWorld profiles with separate paths, user data, backups, rules, and launch settings.
- Back up, restore, compare, import, and export load orders.
- Search and manage Steam Workshop items, collections, SteamCMD downloads, and Steam client subscriptions.
- Track GitHub, GitLab, GitGud, zip, and other direct mod sources.
- Detect missing items, duplicate package IDs, coexisting versions, removed mods, and leftover records.
- View game logs, group common errors, search file contents, and use AI-assisted log diagnosis.
- Optimize textures with DDS generation, scaling options, fallback handling, and cleanup tools.
- Export mod recommendation lists in several formats.
- Use the multi-language UI and external translation workflow.
- This branch (feat/edgelord) additionally adds an MCP server, texture encoder tiers, and search enhancements — see [Branch Additions](#branch-additions-feat-edgelord).

## Download

If you only want to use RimCrow, download a release build instead of running from source.

- This fork (feat/edgelord, with MCP server and texture encoder tiers): https://github.com/ShiGouQAQ/RimCrow/releases
- Upstream:
  - Lanzou Cloud: https://wwbns.lanzouu.com/b00mq4tqgf
    Password: `aite`
  - GitHub Releases: https://github.com/Inky-Feather/RimCrow/releases

This fork's package is a slim build: it ships `RimCrow.exe`, `rimcrow-mcp.exe`, and the companion mod only. External tools (ripgrep / SteamCMD / todds) and community data caches (rules, steamDB, etc.) are not bundled — the app downloads them through its built-in tool check and data source updates on first use (network required).

## Quick Start

1. Download a full release package.
2. Extract it to a stable folder.
3. Start RimCrow and confirm the RimWorld game folder, user data folder, Workshop folder, and manager library folder.
4. Scan mods.
5. Enable the mods you want.
6. Run auto sort.
7. Check red errors and yellow warnings.
8. Save the load order, then launch the game.

## Common Troubleshooting

- Blank window or frontend timeout: install or repair Microsoft Edge WebView2 Runtime. Browser mode can help isolate desktop shell issues.
- Wrong game or data path: reselect the RimWorld game folder, user data folder, and Workshop folder, then scan again.
- SteamCMD download failure: check SteamCMD setup, proxy settings, network access, disk space, and whether the Workshop item is still available.
- AI request failure: check protocol, Base URL, API key, model name, proxy, quota, and provider status.
- Update failure: download the full package from another release channel and overwrite the old installation.

## Development

Use release builds for normal play. Source builds are mainly for development.

Recommended branch:

- `main`: release-oriented snapshots.
- `dev`: latest active development.
- `feat/edgelord` (this fork): upstream `dev` (currently v0.24.12) plus an MCP server, texture encoder tiers (todds / NVTT / texconv with auto selection), search enhancements, and stability fixes — see [Branch Additions](#branch-additions-feat-edgelord).

Basic setup:

```powershell
git clone --recurse-submodules https://github.com/Inky-Feather/RimCrow
cd RimCrow
git switch dev
uv sync
cd frontend
npm install
```

Run in development mode:

```powershell
cd frontend
npm run dev
cd ..
uv run python main.py
```

Run tests:

```powershell
uv run pytest -q tests
```

## Translation

The UI currently includes:

- Simplified Chinese: `zh-CN`
- Traditional Chinese: `zh-TW`
- English: `en`
- German: `de`
- Korean: `ko`
- Russian: `ru`

Chinese source text in code is the default reference for built-in locales. After changing source UI text, run the locale sync scripts described in the Chinese README.

## Branch Additions (feat-edgelord)

This branch adds the following on top of upstream `dev` (currently v0.24.12). Everything below ships with the release build.

### MCP Server

RimCrow's manager capabilities are exposed to external AI tools (Claude Code, OpenCode, ZCode, Claude Desktop, Cursor — any MCP client) over the **Model Context Protocol**:

```
MCP client
   ↕ stdio
rimcrow-mcp.exe (Rust bridge: stdio ↔ Streamable HTTP; discovers port & token from mcp-endpoint.json)
   ↕ HTTP (loopback only + bearer token)
Embedded endpoint inside the RimCrow GUI (backend/mcp_endpoint.py, starts with the app)
   ↕ in-process calls
RimCrow backend (scanning, load order, Workshop & downloads, Git sources, texture optimization, log troubleshooting)
```

The tool surface spans four domains:

- Manage: scan, mod list & details, load order read/write, auto sort, groups, backups, profile switching
- Troubleshoot: game status & two-stage launch, log reading & full-text search, event waiting
- Acquire: Workshop subscribe & download, Steam Web API subscriptions, Git repo subscriptions & release deployment, local mod package import, update checks
- Textures: encoder management & batch DDS/ZSTD conversion (dry-run preview → submit → poll/cancel)

The tool list is maintained single-source in `backend/mcp_tool_specs.py` and evolves with releases; this doc intentionally pins no counts. Clients should always rely on the live `tools/list` response — the snapshot embedded in the bridge is only a fallback for when the GUI is not running.

#### Install & Use

1. **Install the GUI**: download the release package from [this repo's Releases](https://github.com/ShiGouQAQ/RimCrow/releases), extract it to a stable folder, start `RimCrow.exe`, and finish path setup plus the first scan. The GUI and `rimcrow-mcp.exe` are built and version-aligned together — the bridge needs no separate install.
2. **Register the bridge with your MCP client** (`mcpServers` style, e.g. Claude Desktop / Cursor):

```json
{
  "mcpServers": {
    "rimcrow": {
      "command": "C:\\path\\to\\RimCrow\\rimcrow-mcp.exe"
    }
  }
}
```

- Claude Code (project scope): register the same `command` in `.mcp.json` at the project root.
- OpenCode (project scope): put the same command under `mcp.servers.rimcrow` in the project's `opencode.json`.
- The bridge needs no API key: auth is handled by the embedded endpoint (loopback only + bearer token); the endpoint file lives at `%LOCALAPPDATA%\RimCrow\mcp-endpoint.json` and refreshes on every GUI start.

3. **Verify connectivity**: keep the GUI running and call any tool from the client (e.g. read the load order). You can also confirm the bridge itself runs:

```powershell
.\rimcrow-mcp.exe -V
```

4. **Daily use**: the GUI just needs to stay open — every call re-probes the endpoint, and a GUI restart never requires restarting the client. Add `--spawn` to the bridge command to auto-launch the GUI when it is closed (allow time for a cold start). Typical scenarios: read & modify load order, scan and explain mod conflicts, search logs for errors, subscribe missing dependencies, batch texture conversion to DDS/ZSTD.

#### Troubleshooting

| Symptom | Fix |
| --- | --- |
| Calls return a "RimCrow not running" causal error | Start the GUI and retry, or add `--spawn` to the bridge command |
| Client cannot connect to the bridge | Make sure `command` is an absolute path to `rimcrow-mcp.exe` inside the extracted folder; verify with `rimcrow-mcp.exe -V` |
| Fewer tools than expected | The embedded snapshot is only a fallback; start the GUI and rely on the live list, restart the GUI once after upgrading |
| Bridge misbehaves | Set `RIMCROW_MCP_LOG=debug` and restart the client to reproduce; full parameters and the endpoint contract: [ShiGouQAQ/rimcrow-mcp](https://github.com/ShiGouQAQ/rimcrow-mcp) |

### Texture Encoder Tiers

Upstream texture optimization runs through a single todds pipeline; this branch adds three encoder tiers plus automatic selection:

| Tier | Encoder | Notes |
| --- | --- | --- |
| Standard | todds | Best quality, slowest |
| Medium | NVTT | Near-standard quality; requires NVIDIA's tool installed locally |
| Fast | texconv | Fastest; slightly lower quality, hard to notice in play |
| Auto | — | Detects the machine: picks Fast when its tools are present and GPU DirectCompute encoding works, otherwise falls back to Standard |

- GUI encoder card: detects local encoders, downloads texconv from the vendor page, shows GPU encoding probe results (transparent textures automatically go to CPU).
- Sampling benchmark: measures optimal GPU/CPU shard counts against real textures from enabled mods (sources read-only, output to a temp folder) and applies the result directly.
- Manual shard overrides for GPU/CPU, persisted and applied to all conversion tasks.
- MCP texture path: encoder status/download plus the full convert loop (dry-run preview → submit → poll/cancel).

Where: texture optimization modal ("encoder tiers" area) and settings ("texture encoder tiers"); config key `encoder_backend` (default `todds`, matching upstream behavior).

### Search & Stability

- Mod text search supports `filenames_only` (locate resource files by name / relative path) and `file_types` filtering.
- Single-instance lock: a second launch exits silently, preventing concurrent SQLite corruption in the app folder.
- Packaging additions: a `bridge` step (tool snapshot → cargo build → shipped with the main exe) and optional UPX compression (set `app.upx_dir` in `pack.local.json`; PyInstaller applies UPX to the main binary and CFG-protected system DLLs are skipped automatically).

## Privacy And Safety

RimCrow mainly works with local RimWorld files, mod folders, configuration, logs, and its own database.

Sensitive fields such as AI API keys, Steam Web API keys, and proxy credentials are stored through the system credential store when possible. Logs and API parameters are masked where supported, but you should still review logs, configs, and exported packages before sharing them.

Features that use AI providers, Steam, GitHub, Lanzou Cloud, community rules, or external databases may send requests to those third-party services. Local list management, sorting, and backup workflows can still be used without network-dependent features.

## Acknowledgements

- [4h47-fun/RimCrow (feat/mcp-server branch)](https://github.com/4h47-fun/RimCrow/tree/feat/mcp-server) — implementation reference for the MCP capability of this branch. That branch first brought an MCP Server to RimCrow (stdio transport, tiered dynamic tool registration, an EventBus bridge, and smoke scripts); this branch continues and extends that direction on an embedded-endpoint + Rust bridge architecture.

## License

This repository keeps the upstream MIT license. The MCP bridge is a standalone project, [ShiGouQAQ/rimcrow-mcp](https://github.com/ShiGouQAQ/rimcrow-mcp), licensed GPL-3.0-or-later.
