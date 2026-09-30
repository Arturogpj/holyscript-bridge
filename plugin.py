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

import hashlib
import inspect
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request

import gradio as gr

from shared.utils.plugins import WAN2GPPlugin

PlugIn_Name = "Holy Script"
PlugIn_Id = "HolyScriptBridge"

API_HOST = "127.0.0.1"
API_PORT = 7866

_TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

# The site talks to WanGP's MCP API v2. WanGP added these arguments to
# build_server_for_session on 2026-09-07 (v12.73); an older WanGP fails
# with a bare "unexpected keyword argument", so we check up front.
MIN_WANGP = "v12.73"
_V2_ARGS = ("api_version", "allow_async")

# cloudflared is fetched from Cloudflare's own GitHub releases the first
# time it's missing - the writer's friends never install anything by hand.
_RELEASE_API = "https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
_RELEASE_DIRECT = "https://github.com/cloudflare/cloudflared/releases/latest/download/"

_state = {
    "mcp": "starting",     # starting | ok | local-only | outdated | error: ...
    "tunnel": "starting",  # starting | installing | install-failed | ok | error: ...
    "tunnel_why": "",      # reason when tunnel == install-failed
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


def _wangp_version():
    """This box's WanGP version as 'v12.73' (None when it can't be read)."""
    for name in ("wgp", "__main__"):
        version = getattr(sys.modules.get(name), "WanGP_version", None)
        if version:
            return f"v{version}"
    return None


def _api_v2_ready(build_server):
    """True when this WanGP's MCP server builder speaks API v2."""
    try:
        params = inspect.signature(build_server).parameters
    except (TypeError, ValueError):
        return True  # can't tell - try anyway, the real error will show
    return all(name in params for name in _V2_ARGS)


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

            if not _api_v2_ready(build_server_for_session):
                _state["mcp"] = "outdated"
                _log(f"this WanGP ({_wangp_version() or 'unknown version'}) is too "
                     f"old for Holy Script - update WanGP (needs {MIN_WANGP} or newer)")
                return

            # Tunnel URL arrives seconds after boot (longer the very first
            # time, while cloudflared downloads); the API is useless from
            # outside before it exists anyway. Fall back to localhost-only
            # if the tunnel never materializes.
            waited = 0
            while not _url_event.wait(timeout=2):
                waited += 2
                if waited >= (240 if _state["tunnel"] == "installing" else 90):
                    break
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
            _register_preview_route(server, session)
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


_CLOUDFLARED = "cloudflared.exe" if os.name == "nt" else "cloudflared"


def _cloudflared_candidates(app_dir):
    """Where cloudflared may live, most specific first.

    Pinokio puts WanGP at <pinokio>/api/<name>.git/app and its tools in
    <pinokio>/bin - on ANY drive or folder - so walking up from the app
    folder finds the right one wherever Pinokio was installed. The app
    folder itself is where the auto-installer puts its own copy.
    """
    yield os.path.join(app_dir, _CLOUDFLARED)
    folder = os.path.abspath(app_dir)
    while True:
        yield os.path.join(folder, "bin", _CLOUDFLARED)
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    for root in (r"D:\pinokio", r"C:\pinokio",
                 os.path.join(os.path.expanduser("~"), "pinokio")):
        yield os.path.join(root, "bin", _CLOUDFLARED)


def _find_cloudflared():
    for candidate in _cloudflared_candidates(_app_dir):
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("cloudflared")


def _cloudflared_asset():
    """Name of Cloudflare's release file for this system (None = no build)."""
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        arch = "amd64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        return None
    if os.name == "nt":
        return "cloudflared-windows-amd64.exe" if arch == "amd64" else None
    if sys.platform == "darwin":
        return f"cloudflared-darwin-{arch}.tgz"
    return f"cloudflared-linux-{arch}"


def _release_asset(asset):
    """(download url, sha256 or None) for the newest cloudflared release.

    GitHub lists a sha256 digest for every release file; when the lookup
    fails we still download over HTTPS from the same repo, just unchecked.
    """
    try:
        req = urllib.request.Request(_RELEASE_API, headers={
            "User-Agent": "holy-script-plugin",
            "Accept": "application/vnd.github+json",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            release = json.load(resp)
        for item in release.get("assets", []):
            if item.get("name") == asset:
                digest = str(item.get("digest") or "")
                return (item["browser_download_url"],
                        digest[7:].lower() if digest.startswith("sha256:") else None)
    except Exception as exc:
        _log(f"release lookup failed ({exc}) - using the direct download link")
    return _RELEASE_DIRECT + asset, None


def _download(url, dest):
    """Stream url to dest; returns the sha256 of what was written."""
    req = urllib.request.Request(url, headers={"User-Agent": "holy-script-plugin"})
    digest = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def _extract_tgz(archive, final):
    """macOS builds ship as .tgz: pull out the one file we need."""
    with tarfile.open(archive) as tar:
        member = next((m for m in tar.getmembers()
                       if m.isfile() and os.path.basename(m.name) == "cloudflared"), None)
        if member is None:
            raise RuntimeError("cloudflared was not inside the download")
        with open(final + ".new", "wb") as out:
            shutil.copyfileobj(tar.extractfile(member), out)
    os.replace(final + ".new", final)


def _binary_runs(path):
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True,
                             timeout=30, stdin=subprocess.DEVNULL)
        return out.returncode == 0 and "cloudflared" in (out.stdout + out.stderr).lower()
    except (OSError, subprocess.SubprocessError):
        return False


def _install_cloudflared(dest_dir=None):
    """Download Cloudflare's official cloudflared into the WanGP app folder.

    Returns (path, "") or (None, reason). Runs only when nothing was
    found, so a friend never has to install or type anything. Never
    leaves a half-written file where _find_cloudflared would trust it.
    """
    asset = _cloudflared_asset()
    if not asset:
        return None, f"no cloudflared build for this system ({sys.platform}/{platform.machine()})"
    dest_dir = dest_dir or _app_dir
    final = os.path.join(dest_dir, _CLOUDFLARED)
    part = final + ".part"
    try:
        url, expected = _release_asset(asset)
        _log(f"downloading {url}")
        digest = _download(url, part)
        if expected and digest != expected:
            raise RuntimeError("the download did not match Cloudflare's checksum")
        if asset.endswith(".tgz"):
            _extract_tgz(part, final)
            os.remove(part)
        else:
            os.replace(part, final)
        if os.name != "nt":
            os.chmod(final, 0o755)
        if not _binary_runs(final):
            os.remove(final)
            raise RuntimeError("the downloaded file would not start")
        _log(f"cloudflared installed: {final}")
        return final, ""
    except Exception as exc:
        for leftover in (part, final + ".new"):
            try:
                os.remove(leftover)
            except OSError:
                pass
        return None, str(exc) or exc.__class__.__name__


def _ensure_tunnel():
    """Bring the tunnel up in the background (never blocks WanGP's UI build)."""
    global _tunnel_started
    if _tunnel_started:
        return
    _tunnel_started = True
    threading.Thread(target=_tunnel_main, name="holy-tunnel", daemon=True).start()


def _tunnel_main():
    exe = _find_cloudflared()
    if not exe:
        _state["tunnel"] = "installing"
        _log("cloudflared not found - downloading it from Cloudflare (first time only)")
        exe, why = _install_cloudflared()
        if not exe:
            _state["tunnel"] = "install-failed"
            _state["tunnel_why"] = why
            _log(f"could not install cloudflared: {why}")
            _url_event.set()  # no tunnel is coming: let the API start now, localhost-only
            return
        _state["tunnel"] = "starting"
    _start_tunnel(exe)


def _start_tunnel(exe):
    """Spawn cloudflared as OUR child + watch its log for the URL."""

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
        _url_event.set()  # no tunnel is coming: let the API start now, localhost-only


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

    # What a finished clip was made with. WanGP writes the settings it
    # ACTUALLY used (random seed resolved) into the file's metadata, so the
    # site can show the seed of clips that went out as "random".
    # Read by fetch() → CORS for allowed origins only.
    def holy_meta(request):
        from starlette.responses import JSONResponse

        origin = request.headers.get("origin", "")
        headers = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"} if _origin_allowed(origin) else {}
        name = (request.query_params.get("name") or "")
        safe = os.path.basename(name)
        if not safe or safe != name or ".." in name:
            return JSONResponse({"error": "bad filename"}, status_code=400, headers=headers)
        path = os.path.join(_app_dir, "outputs", safe)
        if not os.path.isfile(path):
            return JSONResponse({"error": "not found in outputs"}, status_code=404, headers=headers)
        try:
            from shared.utils.video_metadata import read_metadata_from_video

            meta = read_metadata_from_video(path) or {}
        except Exception as exc:
            return JSONResponse({"error": f"metadata unreadable: {exc}"}, status_code=500, headers=headers)
        keep = ("seed", "model_type", "resolution", "video_length", "num_inference_steps", "guidance_phases", "generation_time")
        return JSONResponse({k: meta.get(k) for k in keep if k in meta}, headers=headers)

    server.custom_route("/holy/meta", methods=["GET"], include_in_schema=False)(holy_meta)
    _log("meta route registered")


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


# Live preview. WanGP builds a preview picture during every render, headless
# included (api_cli "preview" command -> PreviewUpdate with a PIL image), but
# its MCP layer only forwards `has_image_preview: true`. We keep the newest
# picture per job and serve it at GET /holy/preview?job=<mcp job id>.
_preview_lock = threading.Lock()
_jobs_by_id = {}      # mcp job_id -> SessionJob (newest last, bounded)
_previews = {}        # id(SessionJob) -> {"image", "seq", "jpeg", "jpeg_seq"}
_MAX_PREVIEW_JOBS = 12


def _register_preview_route(server, session):
    try:
        from shared import mcp_server as _mcp
    except Exception as exc:
        _log(f"preview route unavailable: {exc}")
        return

    # 1) Remember which SessionJob each MCP job id belongs to.
    store_cls = getattr(_mcp, "_JobStore", None)
    if store_cls is not None and not getattr(store_cls, "_holy_preview_patched", False):
        original_submit = store_cls.submit

        def submit(self, source):
            record = original_submit(self, source)
            with _preview_lock:
                _jobs_by_id[record.job_id] = record.job
                while len(_jobs_by_id) > _MAX_PREVIEW_JOBS:
                    old_id = next(iter(_jobs_by_id))
                    old_job = _jobs_by_id.pop(old_id)
                    _previews.pop(id(old_job), None)
            return record

        store_cls.submit = submit
        store_cls._holy_preview_patched = True

    # 2) Keep the newest preview picture each job emits. Cheap on the
    #    render thread: store the reference, encode only when asked.
    original_emit = session._emit_callback

    def emit(method_name, payload, *, job=None):
        if method_name == "on_preview" and job is not None:
            image = getattr(payload, "image", None)
            if image is not None:
                with _preview_lock:
                    slot = _previews.setdefault(id(job), {"seq": 0, "jpeg": None, "jpeg_seq": -1})
                    slot["image"] = image
                    slot["seq"] += 1
        return original_emit(method_name, payload, job=job)

    session._emit_callback = emit

    def holy_preview(request):
        import io

        from starlette.responses import JSONResponse, Response

        job_id = (request.query_params.get("job") or "").strip()
        with _preview_lock:
            job = _jobs_by_id.get(job_id)
            slot = _previews.get(id(job)) if job is not None else None
            if not slot or slot.get("image") is None:
                return JSONResponse({"error": "no preview yet"}, status_code=404)
            if slot["jpeg_seq"] != slot["seq"]:
                try:
                    image = slot["image"].convert("RGB")
                    if image.width > 1280:
                        image = image.resize((1280, max(1, round(image.height * 1280 / image.width))))
                    buf = io.BytesIO()
                    image.save(buf, format="JPEG", quality=82)
                    slot["jpeg"] = buf.getvalue()
                    slot["jpeg_seq"] = slot["seq"]
                except Exception as exc:
                    return JSONResponse({"error": f"preview encode failed: {exc}"}, status_code=500)
            body = slot["jpeg"]
        return Response(body, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=30"})

    server.custom_route("/holy/preview", methods=["GET"], include_in_schema=False)(holy_preview)
    _log("preview route registered")


def _status_lines():
    if _state["mcp"] == "outdated":
        ver = _wangp_version()
        api = (f"OUTDATED - this WanGP is too old for Holy Script "
               f"({'yours is ' + ver + ', ' if ver else ''}needs {MIN_WANGP} or newer).\n"
               "    Update WanGP (Pinokio: open WanGP, click Update), restart it, "
               "then press Refresh status.")
    elif _port_open(API_HOST, API_PORT):
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
    elif t == "installing":
        tun = "installing - downloading cloudflared from Cloudflare (one time only)..."
    elif t == "install-failed":
        tun = (f"FAILED - couldn't download cloudflared ({_state['tunnel_why']}).\n"
               "    Check your internet connection and restart WanGP to try again.")
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
                             lines=5, interactive=False)
            url_box = gr.Text(label="Public API address",
                              value=url, interactive=False,
                              show_copy_button=True)
            refresh = gr.Button("Refresh status")
            refresh.click(_status_lines, outputs=[status, url_box])
        self.on_tab_outputs = [status, url_box]
