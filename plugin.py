"""Holy Script bridge plugin for WanGP.

Runs INSIDE the normal WanGP process (stock Start button, zero flags):

1. Starts WanGP's own MCP server in a background thread on
   127.0.0.1:7866 (same code path as `python wgp.py --mcp`, but the
   Gradio UI stays alive because we never sys.exit).
2. Starts a cloudflared quick tunnel to :7866 as a child of this
   process (lives and dies with WanGP - no orphans, no scheduler).
3. Shows the public address in a "Holy Script" tab with a copy
   button, plus live API/tunnel status.

The Holy Script website talks to WanGP through that address (the
site resolves the MCP path itself - users paste the plain address).
"""

import os
import re
import shutil
import socket
import subprocess
import threading
import time

import gradio as gr

from shared.utils.plugins import WAN2GPPlugin

PlugIn_Name = "Holy Script"
PlugIn_Id = "HolyScriptBridge"

API_HOST = "127.0.0.1"
API_PORT = 7866

_TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

_state = {
    "mcp": "starting",     # starting | ok | local-only | error: ...
    "tunnel": "starting",  # starting | ok | error: ... | no-cloudflared
    "url": "",
}
_url_event = threading.Event()
_mcp_started = False
_tunnel_started = False
_here = os.path.dirname(os.path.abspath(__file__))
_app_dir = os.path.dirname(os.path.dirname(_here))  # WanGP app/ dir
_tunnel_log = os.path.join(_here, "holy-tunnel.log")
_address_file = os.path.join(_app_dir, "holy-address.txt")


def _log(msg):
    print(f"[holy-script] {msg}", flush=True)


def _port_open(host, port):
    try:
        s = socket.create_connection((host, port), timeout=2)
        s.close()
        return True
    except OSError:
        return False


def _ensure_mcp():
    """Serve the MCP API on 127.0.0.1:7866 (daemon thread).

    Uses its own shared.api session (exactly like `wgp.py --mcp`) -
    the tab's Gradio webui session lacks live state access for
    session-backed tools.

    Waits for the tunnel URL first so the tunnel hostname can be
    allowlisted: FastMCP auto-enables DNS-rebinding protection on
    localhost binds and answers 421 to any other Host. Without the
    allowlist the public URL stays red forever.
    """
    global _mcp_started
    if _mcp_started:
        return
    _mcp_started = True

    def _run():
        try:
            from urllib.parse import urlsplit
            from mcp.server.transport_security import TransportSecuritySettings
            from shared.api import init as _api_init
            from shared.mcp_server import build_server_for_session

            # Tunnel URL arrives seconds after boot; the API is useless
            # from outside before it exists anyway. Fall back to
            # localhost-only if the tunnel never materializes.
            _url_event.wait(timeout=90)
            allowed = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
            host = urlsplit(_state["url"]).netloc.split(":")[0] if _state["url"] else ""
            if host:
                allowed += [host, host + ":*"]
                _log(f"public host allowlisted: {host}")
            else:
                _state["mcp"] = "local-only"
                _log("no tunnel URL - serving API on localhost only")
            session = _api_init(root=_app_dir, console_output=False,
                                console_isatty=False)
            # Same settings wgp.py uses for streamable-http transport.
            settings = {
                "host": API_HOST,
                "port": API_PORT,
                "json_response": True,
                "stateless_http": True,
                "transport_security": TransportSecuritySettings(
                    enable_dns_rebinding_protection=True,
                    allowed_hosts=allowed,
                ),
            }
            server = build_server_for_session(
                session,
                settings,
                api_version=2,
                allow_async=True,
                http_media_transfer=True,
            )
            _register_resolution_tool(server, session)
            _register_upload_route(server, session)
            import uvicorn

            _log(f"MCP server starting on {API_HOST}:{API_PORT}")
            if _state["mcp"] == "starting":
                _state["mcp"] = "ok"
            uvicorn.run(
                server.streamable_http_app(),
                host=API_HOST,
                port=API_PORT,
                log_level="warning",
            )
        except Exception as exc:  # never take WanGP down with us
            _state["mcp"] = f"error: {exc}"
            _log(f"MCP server failed: {exc}")

    threading.Thread(target=_run, name="holy-mcp", daemon=True).start()


def _find_cloudflared():
    for candidate in (
        r"D:\pinokio\bin\cloudflared.exe",
        r"C:\pinokio\bin\cloudflared.exe",
        os.path.join(_app_dir, "cloudflared.exe"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("cloudflared")


def _ensure_tunnel():
    """Spawn cloudflared as OUR child + watch its log for the URL."""
    global _tunnel_started
    if _tunnel_started:
        return
    _tunnel_started = True

    exe = _find_cloudflared()
    if not exe:
        _state["tunnel"] = "no-cloudflared"
        _log("cloudflared not found - tunnel disabled (API still works locally)")
        return

    def _watch(log_path):
        url = ""
        for _ in range(60):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    m = _TUNNEL_RE.search(f.read())
                if m:
                    url = m.group(0)
                    break
            except OSError:
                pass
            threading.Event().wait(2)
        if url:
            _state["tunnel"] = "ok"
            _state["url"] = url
            _url_event.set()
            try:
                with open(_address_file, "w", encoding="ascii") as f:
                    f.write(url)
            except OSError:
                pass
            _log(f"tunnel up: {url}")
        else:
            _state["tunnel"] = "error: no URL after 2 minutes (see holy-tunnel.log)"
            _log("tunnel produced no URL - cloudflared may be rate-limited, retry Refresh")

    try:
        # Fresh log each launch so we never read a stale URL.
        try:
            os.remove(_tunnel_log)
        except OSError:
            pass
        subprocess.Popen(
            [exe, "tunnel", "--url", f"http://{API_HOST}:{API_PORT}",
             "--logfile", _tunnel_log],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        _log("cloudflared tunnel starting...")
        threading.Thread(target=_watch, args=(_tunnel_log,),
                         name="holy-tunnel-watch", daemon=True).start()
    except Exception as exc:
        _state["tunnel"] = f"error: {exc}"
        _log(f"tunnel failed to start: {exc}")


def _register_resolution_tool(server, session):
    """Expose WanGP's own computed resolution groups for a model.

    Covers per-model lists, categories, block alignment AND this box's
    custom resolutions.json — things the stock MCP tools don't return.
    """
    try:
        from shared import resolutions as resolution_utils
    except Exception as exc:
        _log(f"resolution tool unavailable: {exc}")
        return
    import json as _json

    def holy_resolution_choices(model_type: str = "") -> dict:
        try:
            model_def = session.get_model_def(model_type) if model_type else {}
            enable_4k = False
            try:
                with open(os.path.join(_app_dir, "wgp_config.json"), encoding="utf-8") as f:
                    enable_4k = bool(_json.load(f).get("enable_4k_resolutions", 0))
            except Exception:
                pass
            choices, current = resolution_utils.resolve_resolution_choices(
                None, model_def or {}, enable_4k_resolutions=enable_4k)
            groups, _, _ = resolution_utils.group_resolution_choices(choices, current)
            out = {"groups": [], "current": current}
            for g in groups:
                items = resolution_utils.group_choices(choices, g)
                out["groups"].append({
                    "group": g,
                    "items": [{"label": label, "value": value} for label, value in items],
                })
            return out
        except Exception as exc:
            return {"error": str(exc)}

    server.add_tool(
        holy_resolution_choices,
        name="holy_resolution_choices",
        description="WanGP-computed resolution groups and choices for one model_type (includes custom resolutions.json).",
    )
    _log("resolution tool registered")

    # Stable file serving for finished clips. WanGP's own download
    # tickets are single-use (the browser's first range request eats
    # them, playback then stalls) and expire; outputs files live on
    # disk, so serve them directly by filename — permanent, seekable,
    # replayable. Basename-only, outputs dir only.
    def holy_file(request):
        from starlette.responses import FileResponse, JSONResponse

        name = (request.query_params.get("name") or "")
        safe = os.path.basename(name)
        if not safe or safe != name or ".." in name:
            return JSONResponse({"error": "bad filename"}, status_code=400)
        for base in (os.path.join(_app_dir, "outputs"),):
            path = os.path.join(base, safe)
            if os.path.isfile(path):
                import mimetypes

                return FileResponse(
                    path,
                    filename=safe,
                    media_type=mimetypes.guess_type(path)[0] or "video/mp4",
                )
        return JSONResponse({"error": "not found in outputs"}, status_code=404)

    server.custom_route("/holy/file", methods=["GET"], include_in_schema=False)(holy_file)
    _log("file route registered")


_upload_log = os.path.join(_here, "holy-uploads.log")


def _ulog(line):
    """Append to the upload log — failures must never be invisible."""
    try:
        with open(_upload_log, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def _origin_allowed(origin):
    """Production site, any Vercel preview, and local dev ports.

    A refused origin shows up in the browser as an opaque CORS error
    (no readable response), which used to make the app silently fall
    back to uploading through the tiny relay and fail with 413.
    """
    if not origin:
        return False
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(origin)
        if parts.scheme not in {"http", "https"}:
            return False
        host = (parts.hostname or "").lower()
        if host in {"script.holyfstudios.com", "localhost", "127.0.0.1"}:
            return True
        return host.endswith(".vercel.app")
    except Exception:
        return False


_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
}

_KNOWN_EXT_RE = re.compile(r"\.(png|jpe?g|webp|gif|bmp|tiff?|wav|mp3|m4a|aac|ogg|flac|mp4|mov|webm|mkv|avi)$", re.I)


_VIDEO_UPLOAD_EXT = {".webm", ".mp4", ".mov", ".mkv", ".avi", ".m4v"}


def _remux_uploaded_video(src_path):
    """Rewrite an uploaded video into a duration-bearing .mp4.

    WanGP's has_video_file_extension is {mp4, mkv, avi, mov} — not webm.
    MediaRecorder webm/fMP4 also ship with no Duration, so ffprobe reads
    0.00s and MiniMax rejects "Reference Video must be at least 2 seconds
    long". ffmpeg on this box reads the packets and writes a real moov.
    Failure returns the original path (upload still registered).
    """
    ext = os.path.splitext(src_path)[1].lower()
    if ext not in _VIDEO_UPLOAD_EXT:
        return src_path
    try:
        from shared.utils.video_decode import probe_video_stream_metadata, resolve_media_binary
    except Exception as exc:
        _ulog(f"remux skip (no video_decode): {exc}")
        return src_path
    ffmpeg = resolve_media_binary("ffmpeg")
    if not ffmpeg:
        _ulog("remux skip (no ffmpeg)")
        return src_path
    tmp = src_path + ".tmp.mp4"
    final = os.path.splitext(src_path)[0] + ".mp4"
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts",
        "-i", src_path,
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ac", "2", "-movflags", "+faststart",
        tmp,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=180)
        if proc.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) < 4096:
            err = (proc.stderr or b"").decode("utf-8", "replace")[:300]
            _ulog(f"remux failed rc={proc.returncode} err={err}")
            try:
                os.remove(tmp)
            except OSError:
                pass
            return src_path
        meta = probe_video_stream_metadata(tmp)
        dur = float((meta or {}).get("duration") or 0)
        if dur <= 0.05:
            _ulog("remux produced 0s duration, keeping original")
            try:
                os.remove(tmp)
            except OSError:
                pass
            return src_path
        os.replace(tmp, final)
        if os.path.normcase(src_path) != os.path.normcase(final):
            try:
                os.remove(src_path)
            except OSError:
                pass
        _ulog(f"remux ok {os.path.basename(src_path)} -> {os.path.basename(final)} dur={dur:.2f}s")
        return final
    except Exception as exc:
        _ulog(f"remux exception: {exc}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return src_path


def _safe_upload_name(name, content_type):
    """A gallery-safe filename.

    Canvas nodes are titled "IMAGE" / "AUDIO" with no extension, and the
    gallery rejects files whose extension it does not recognise — which
    silently killed every reference upload. Fall back to the request's
    Content-Type when the name carries no known extension.
    """
    base = os.path.basename(str(name or "").strip()) or "reference"
    if _KNOWN_EXT_RE.search(base):
        return base
    mime = str(content_type or "").split(";")[0].strip().lower()
    return base + _EXT_BY_MIME.get(mime, "")


def _register_upload_route(server, session):
    """CORS-enabled direct upload for reference media.

    The browser PUTs bytes straight to this box through the tunnel, so
    large images/audio never pass through the tiny serverless relay
    (which rejects bodies above ~4.5 MB with a 413). Same storage and
    gallery registration WanGP's own upload route uses.
    """
    import uuid as _uuid

    def _cors_headers(request):
        origin = request.headers.get("origin", "")
        headers = {
            "Access-Control-Allow-Methods": "PUT, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "600",
        }
        if _origin_allowed(origin):
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
        return headers

    async def holy_upload(request):
        from starlette.responses import JSONResponse, Response
        from shared.mcp_server import _MAX_UPLOAD_BYTES, _register_gallery_media

        headers = _cors_headers(request)
        origin = request.headers.get("origin", "")
        if request.method == "OPTIONS":
            _ulog(f"preflight origin={origin or '-'} allowed={_origin_allowed(origin)}")
            return Response(status_code=204, headers=headers)
        if not _origin_allowed(origin):
            _ulog(f"REFUSED origin={origin or '-'}")
            return JSONResponse({"error": "origin not allowed"}, status_code=403, headers=headers)

        name = _safe_upload_name(
            request.query_params.get("name"),
            request.headers.get("content-type"),
        )
        if not name or name in {".", ".."}:
            _ulog(f"bad filename origin={origin}")
            return JSONResponse({"error": "bad filename"}, status_code=400, headers=headers)

        target_dir = os.path.join(_app_dir, "outputs", "mcp_uploads", _uuid.uuid4().hex)
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, name)
        size = 0
        try:
            with open(target, "xb") as writer:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > _MAX_UPLOAD_BYTES:
                        raise ValueError("upload too large")
                    writer.write(chunk)
            target = _remux_uploaded_video(target)
            name = os.path.basename(target)
            size = os.path.getsize(target)
            record = _register_gallery_media(session, target)
        except Exception as exc:
            try:
                os.remove(target)
            except OSError:
                pass
            _ulog(f"FAILED name={name} size={size} error={exc}")
            return JSONResponse({"error": str(exc)}, status_code=400, headers=headers)

        media_id = str(record.get("media_id") or record.get("id") or "")
        _ulog(f"ok name={name} size={size} media_id={media_id}")
        return JSONResponse(
            {"status": "uploaded", "media_id": media_id, "filename": name, "size": size},
            headers=headers,
        )

    server.custom_route("/holy/upload", methods=["PUT", "OPTIONS"], include_in_schema=False)(holy_upload)
    _log("upload route registered")


def _status_lines():
    if _port_open(API_HOST, API_PORT):
        api = f"OK - listening on {API_HOST}:{API_PORT}"
    elif _state["mcp"] == "local-only":
        api = "LOCAL ONLY - tunnel never came up, remote stays red"
    elif _state["mcp"].startswith("error"):
        api = "FAILED - " + _state["mcp"]
    else:
        api = "starting..."
    t = _state["tunnel"]
    if t == "ok":
        tun = "RUNNING"
    elif t == "starting":
        tun = "starting..."
    elif t == "no-cloudflared":
        tun = "DISABLED - cloudflared not found (local API still works)"
    else:
        tun = "FAILED - " + t
    return f"API server: {api}\nTunnel: {tun}", _state["url"]


class HolyScriptPlugin(WAN2GPPlugin):
    def setup_ui(self):
        _ensure_tunnel()
        self.add_tab(tab_id=PlugIn_Id, label=PlugIn_Name,
                     component_constructor=self.create_tab)

    def on_tab_select(self, state):
        return _status_lines()

    def on_tab_deselect(self, state):
        pass

    def on_model_change(self, state, model_type):
        pass

    def create_tab(self, api_session):
        _ensure_mcp()
        status_text, url = _status_lines()
        with gr.Column():
            gr.Markdown(
                "### Holy Script bridge\n"
                "This PC's WanGP is reachable from the Holy Script website. "
                "Copy the address below and paste it into Holy Script - the "
                "connection panel does the rest.\n\n"
                "Leave WanGP running while you generate - the tunnel and the API "
                "shut down together with it."
            )
            status = gr.Text(label="Status", value=status_text,
                             lines=2, interactive=False)
            url_box = gr.Text(label="Public API address",
                              value=url, interactive=False,
                              show_copy_button=True)
            refresh = gr.Button("Refresh status")
            refresh.click(_status_lines, outputs=[status, url_box])
        self.on_tab_outputs = [status, url_box]
