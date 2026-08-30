"""A small HTTP server that hands out live video for cameras without RTSP.

A battery doorbell has no RTSP server, so Home Assistant has nothing to point a
camera entity at. What the EZVIZ app plays instead is the cloud VTM stream, and
``pyezvizapi`` can open that and remux it to MPEG-TS with FFmpeg. This server
exposes it - plus a plain JPEG and an MJPEG fallback built from live captures -
on ordinary URLs, so a Generic Camera entity can consume them.

Every URL carries a token that is generated once and kept in ``/data``. The
published URLs include it, so the entities keep working while a stray request
from elsewhere on the network does not.
"""

from __future__ import annotations

from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from pyezvizapi.cloud_stream import copy_cloud_stream_to_mpegts

_LOGGER = logging.getLogger("ezviz_push.liveview")

# Fixed inside the container: the Supervisor maps it to whatever host port the
# user picks in the add-on's Network panel, and Home Assistant itself reaches
# the add-on by container hostname on the internal network.
PORT = 8099

MJPEG_BOUNDARY = "ezvizframe"
# Repeated captures are a cloud round trip each, so a request every couple of
# seconds is as fast as this fallback can sensibly go.
MIN_MJPEG_INTERVAL = 1.0


class LiveViewServer:
    """Serve snapshots and live video for the bridge's cameras."""

    def __init__(
        self,
        bridge: Any,
        port: int,
        token: str,
        base_url: str,
        *,
        mjpeg_interval: float = 3.0,
        clip_seconds: float = 15.0,
    ) -> None:
        """Initialize the server without binding to the port yet."""
        self._bridge = bridge
        self._port = port
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._mjpeg_interval = max(MIN_MJPEG_INTERVAL, mjpeg_interval)
        self._clip_seconds = clip_seconds
        self._httpd: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Bind the port and serve in a background thread."""
        handler = partial(_Handler, self)
        self._httpd = ThreadingHTTPServer(("0.0.0.0", self._port), handler)
        self._httpd.daemon_threads = True
        threading.Thread(
            target=self._httpd.serve_forever,
            name="ezviz-liveview",
            daemon=True,
        ).start()
        _LOGGER.info("Live view server listening on %s", self._base_url)

    def stop(self) -> None:
        """Stop serving."""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    # ------------------------------------------------------------------
    # URLs published to Home Assistant
    # ------------------------------------------------------------------

    def urls(self, serial: str) -> dict[str, str]:
        """Return the URLs for one camera, token included."""
        query = f"?token={self._token}"
        return {
            "live_stream_url": f"{self._base_url}/{serial}/live.ts{query}",
            "snapshot_url": f"{self._base_url}/{serial}/snapshot.jpg{query}",
            "mjpeg_url": f"{self._base_url}/{serial}/mjpeg{query}",
        }

    # ------------------------------------------------------------------
    # Request handling, called from the handler
    # ------------------------------------------------------------------

    @property
    def token(self) -> str:
        """Return the token every request must carry."""
        return self._token

    @property
    def bridge(self) -> Any:
        """Return the bridge this server reads cameras from."""
        return self._bridge

    def index_html(self) -> bytes:
        """Return a page listing every camera and its links."""
        rows = []
        for serial in self._bridge.serials():
            name = self._bridge.device_name(serial)
            urls = self.urls(serial)
            links = " &middot; ".join(
                f'<a href="{url}">{label}</a>'
                for label, url in (
                    ("live.ts", urls["live_stream_url"]),
                    ("snapshot.jpg", urls["snapshot_url"]),
                    ("mjpeg", urls["mjpeg_url"]),
                )
            )
            rows.append(f"<li><strong>{name}</strong> ({serial})<br>{links}</li>")
        body = "".join(rows) or "<li>No cameras known yet.</li>"
        return (
            "<!doctype html><meta charset=utf-8>"
            "<title>EZVIZ Doorbell Push</title>"
            "<h1>EZVIZ Doorbell Push</h1>"
            f"<ul>{body}</ul>"
        ).encode()

    def stream_mpegts(self, serial: str, output: Any) -> None:
        """Copy the cloud live stream to output as MPEG-TS.

        Raises:
            PyEzvizError: If the stream cannot be opened or FFmpeg fails.
        """
        client = self._bridge.client

        # A sleeping battery camera answers nothing, so ask the cloud to keep
        # it awake first. It is a no-op on a mains powered device.
        self._bridge.keep_awake(serial)

        encrypted = bool(self._bridge.device_status(serial).get("encrypted"))
        if encrypted:
            # An encrypted stream has to be collected in full before it can be
            # decrypted, so it can only ever be a clip, not an endless stream.
            _LOGGER.info(
                "Video encryption is on for %s, serving a %.0fs clip. Switch"
                " encryption off in the EZVIZ app for a continuous stream.",
                serial,
                self._clip_seconds,
            )
            copy_cloud_stream_to_mpegts(
                client,
                serial,
                output,
                decrypt_video=True,
                duration_seconds=self._clip_seconds,
            )
            return

        copy_cloud_stream_to_mpegts(client, serial, output)

    def stream_mjpeg(self, serial: str, handler: BaseHTTPRequestHandler) -> None:
        """Write an endless MJPEG stream of freshly captured pictures."""
        while True:
            started = time.monotonic()
            image = self._bridge.capture_snapshot(serial)
            if not image:
                return
            handler.wfile.write(
                f"--{MJPEG_BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                f"Content-Length: {len(image)}\r\n\r\n".encode()
            )
            handler.wfile.write(image)
            handler.wfile.write(b"\r\n")
            handler.wfile.flush()

            remaining = self._mjpeg_interval - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)


class _Handler(BaseHTTPRequestHandler):
    """Route the handful of URLs the live view server answers."""

    server_version = "EzvizDoorbellPush"
    protocol_version = "HTTP/1.1"

    def __init__(self, live: LiveViewServer, *args: Any, **kwargs: Any) -> None:
        """Bind the handler to its server before the base class parses."""
        self._live = live
        # Once a stream's headers are out, nothing more may be written to the
        # socket but stream data - an error page would land inside the video.
        self._streaming = False
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        """Send the access log to the add-on's logger at debug level."""
        _LOGGER.debug("%s %s", self.address_string(), format % args)

    # ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        """Answer one GET request."""
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        parts = [part for part in parsed.path.split("/") if part]

        if parsed.path in ("/", "") and self._authorized(query):
            self._respond(200, "text/html; charset=utf-8", self._live.index_html())
            return

        if not self._authorized(query):
            self._respond(401, "text/plain", b"Missing or wrong token\n")
            return

        if len(parts) != 2:
            self._respond(404, "text/plain", b"Not found\n")
            return

        serial, resource = parts
        if serial not in self._live.bridge.serials():
            self._respond(404, "text/plain", b"Unknown camera\n")
            return

        try:
            if resource == "snapshot.jpg":
                self._snapshot(serial)
            elif resource == "last.jpg":
                self._last_snapshot(serial)
            elif resource == "mjpeg":
                self._mjpeg(serial)
            elif resource == "live.ts":
                self._live_ts(serial)
            else:
                self._respond(404, "text/plain", b"Not found\n")
        except (BrokenPipeError, ConnectionResetError):
            _LOGGER.debug("Client went away during %s for %s", resource, serial)
        # A handler runs on its own connection thread, and anything that
        # escapes it is printed as a traceback and kills only that request, so
        # everything is caught here and reported properly instead.
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Request %s for %s failed: %s", resource, serial, err)
            _LOGGER.debug("Request failed", exc_info=True)
            self._fail(str(err))

    # ------------------------------------------------------------------

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        """Return whether the request carries the right token."""
        given = (query.get("token") or [""])[0]
        return secrets.compare_digest(given, self._live.token)

    def _respond(self, code: int, content_type: str, body: bytes) -> None:
        """Send a complete, small response."""
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, message: str) -> None:
        """Report a failure, unless the response is already on its way."""
        if self._streaming:
            return
        try:
            self._respond(503, "text/plain", f"{message}\n".encode())
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _snapshot(self, serial: str) -> None:
        """Answer with a freshly captured picture."""
        image = self._live.bridge.capture_snapshot(serial)
        if not image:
            self._fail("Could not capture a picture")
            return
        self._respond(200, "image/jpeg", image)

    def _last_snapshot(self, serial: str) -> None:
        """Answer with the last alarm snapshot the bridge published."""
        image = self._live.bridge.last_snapshot(serial)
        if not image:
            self._respond(404, "text/plain", b"No snapshot yet\n")
            return
        self._respond(200, "image/jpeg", image)

    def _mjpeg(self, serial: str) -> None:
        """Answer with an endless MJPEG stream."""
        # Neither stream has a length, so the connection itself has to mark
        # where the response ends.
        self.close_connection = True
        self._streaming = True
        self.send_response(200)
        self.send_header(
            "Content-Type", f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}"
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self._live.stream_mjpeg(serial, self)

    def _live_ts(self, serial: str) -> None:
        """Answer with the cloud live stream, remuxed to MPEG-TS."""
        self.close_connection = True
        self._streaming = True
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self._live.stream_mpegts(serial, self.wfile)


def load_token(path: Any, configured: str = "") -> str:
    """Return the live view token, generating and storing one if needed."""
    if configured:
        return configured
    try:
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token
        token = secrets.token_urlsafe(16)
        path.write_text(token, encoding="utf-8")
    except OSError as err:
        _LOGGER.warning(
            "Could not store the live view token (%s); generating a new one,"
            " which changes the URLs on every restart",
            err,
        )
        return secrets.token_urlsafe(16)
    return token
