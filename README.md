# Holy Script bridge plugin for WanGP

One plugin turns any WanGP (Pinokio or not) into a Holy Script backend.
No flags, no scheduled tasks, no batch files, no second process.

This is a **standalone install unit**: publish this folder as its own
GitHub repo and WanGP's Plugin Manager clones it straight into
`app/plugins/` from the URL (see Install below). It is a mirror of the
copy in the main app repo at `wangp-plugin/holy-script/` — keep the two
in sync when the plugin changes.

A private repo works for sharing with a specific person: WanGP clones
with the user's GitHub auth, so just add them as a collaborator.

## What it does

Inside the normal WanGP process it:

1. Serves WanGP's own MCP API on `127.0.0.1:7866` (background thread —
   the Gradio UI stays alive).
2. Opens a cloudflared quick tunnel to that port (child of WanGP —
   lives and dies with it, never orphaned).
3. Adds a **Holy Script** tab showing the public address with a copy
   button, plus live API/tunnel status.

## Install (any user, own PC)

### Recommended — WanGP's built-in Plugin Manager (paste a GitHub URL)

WanGP already ships an installer for exactly this. No file copying, no
JSON editing:

1. Open WanGP → **Plugins** tab → **Install New Plugin**.
2. Paste this repository's GitHub URL
   (`https://github.com/Arturogpj/holyscript-bridge`).
3. Click **Download and Install Plugin** — WanGP clones the repo and
   installs `requirements.txt` if present.
4. Tick the plugin in **Available Plugins** → **Save Settings** →
   **restart WanGP**.
5. Open the **Holy Script** tab, copy the address, paste it into the
   WanGP pill at the top of the studio (the site handles the rest).

The Plugin Manager also handles updates and uninstall for plugins
installed this way. The repo root must be plugin-shaped (this layout):

```
holyscript-bridge/           <- GitHub repo root (= this folder)
├── __init__.py
├── plugin.py
├── plugin_info.json
└── README.md
```

### Alternative — manual copy

Only for installs where the Plugin Manager isn't available:

1. Copy this folder into WanGP's `app/plugins/` folder.
2. Enable it once: in `app/wgp_config.json` add `"holy-script"` to the
   `enabled_plugins` list (or enable it in WanGP's plugin manager).

`cloudflared` is found automatically (Pinokio bundles it; otherwise any
copy on `PATH`). If it isn't found, the tab says so and the local API
still works.

## What the plugin serves

Besides starting WanGP's MCP server (the website resolves its path itself —
you paste the plain address only), the plugin registers:

| Route / tool | What it does |
|---|---|
| MCP `POST /mcp` | WanGP's own MCP API (JSON-RPC). FastMCP answers **421** to hosts it hasn't allowlisted — the plugin adds your tunnel host automatically once the tunnel URL is known (up to 90 s), else localhost-only. |
| `GET /holy/file?name=…` | Serves a clip straight from WanGP's flat `outputs/` folder — permanent, seekable playback URLs for takes (basename only, 400 on traversal, 404 if gone). |
| `PUT /holy/upload` | Direct browser upload for reference images/audio/video — avoids the website's ~4.5 MB request cap. Origin-allowlisted (`script.holyfstudios.com`, `localhost`/`127.0.0.1`, `*.vercel.app` for previews), CORS-preflightable (`OPTIONS` → 204), filename sanitized with extension derived from MIME when missing, **videos remuxed through ffmpeg to a duration-bearing `.mp4`** (WanGP's `has_video_file_extension` does not include `.webm`, and MediaRecorder files have no Duration — both caused "Reference Video must be at least 2 seconds long (found 0.00s)"), stored under `outputs/mcp_uploads/<id>/` and registered as gallery media. |
| `GET /holy/meta?name=…` | What a finished clip was made with, read from the file's own metadata (WanGP writes the settings it actually used — the resolved random seed too): `seed`, `model_type`, `resolution`, steps, phases. CORS for the allowed origins. Lets the site show the seed of clips that went out as "random". |
| `GET /holy/preview?job=…` | The newest live preview picture WanGP made for a rendering job (JPEG). WanGP's MCP only says a preview exists; the plugin keeps the picture per job and serves it so a take shows it while rendering. 404 until the first preview. |
| MCP tool `holy_resolution_choices(model_type)` | WanGP-computed resolution groups (honors your `resolutions.json` and `enable_4k_resolutions`) so the site's preset editor shows exactly what your box offers. |

## Logs & address file

In this plugin folder unless noted:

- `holy-tunnel.log` — cloudflared output (the public URL is parsed from here)
- `holy-uploads.log` — every upload attempt: preflight origin, refusals, failures, successes with size + gallery id ("failures must never be invisible")
- `holy-address.txt` (in the WanGP app folder) — the current public address
- MCP thread errors are caught so the plugin can never take WanGP down

Status lines in the Holy Script tab: API `OK - listening on …` / `LOCAL ONLY` / `FAILED`; Tunnel `RUNNING` / `starting...` / `DISABLED` / `FAILED`.

Connection architecture (browser → site relay → MCP, plus direct uploads) is documented in the main app repo's `AGENTS.md` → *Generations + WanGP* and `README.md` → *Generations Desk & WanGP*.

## Maintainer notes

This folder is the install unit; the URL friends paste is its own
GitHub repo — keep `plugin.py`, `plugin_info.json`, `__init__.py`, and
this README identical to the copy in the main app repo
(`wangp-plugin/holy-script/`). Do not add repo metadata (`package.json`,
CI, etc.) at the root of the published repo — the Plugin Manager expects
a plugin-shaped root. Updates reach installed copies through the Plugin
Manager's "Check for Updates".
