"""Tests for tunnel.py — local HTTP forwarding and tunnel lifecycle."""

import base64
import json
import threading
from unittest.mock import patch, MagicMock

import pytest

from tunnel import (
    _forward_to_local, start_tunnel, stop_tunnel, tunnel_status,
    _stop_event, _status, _status_lock,
)
import tunnel


@pytest.fixture(autouse=True)
def reset_tunnel_state():
    """Reset tunnel module state between tests."""
    tunnel._stop_event.clear()
    with tunnel._status_lock:
        tunnel._status = "disabled"
    tunnel._thread = None
    yield
    tunnel._stop_event.set()
    with tunnel._status_lock:
        tunnel._status = "disabled"
    tunnel._thread = None


# ---------------------------------------------------------------------------
# _forward_to_local tests
# ---------------------------------------------------------------------------

class TestForwardToLocal:
    @patch("tunnel.http.client.HTTPConnection")
    def test_get_request_returns_200(self, mock_conn_cls):
        """GET request forwarded correctly, returns 200."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"<html>OK</html>"
        mock_resp.getheaders.return_value = [
            ("Content-Type", "text/html"),
        ]

        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_resp
        mock_conn_cls.return_value = mock_conn

        msg = {
            "id": "req-1",
            "method": "GET",
            "path": "/api/status",
            "headers": {"Accept": "text/html"},
            "body_b64": "",
        }

        result = _forward_to_local(msg)

        assert result["id"] == "req-1"
        assert result["status"] == 200
        body = base64.b64decode(result["body_b64"])
        assert body == b"<html>OK</html>"
        mock_conn.request.assert_called_once_with(
            "GET", "/api/status", body=None, headers={"Accept": "text/html"}
        )

    @patch("tunnel.http.client.HTTPConnection")
    def test_post_request_forwards_body(self, mock_conn_cls):
        """POST request with form data is forwarded correctly."""
        mock_resp = MagicMock()
        mock_resp.status = 302
        mock_resp.read.return_value = b""
        mock_resp.getheaders.return_value = [
            ("Location", "/"),
            ("Content-Type", "text/html"),
        ]

        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_resp
        mock_conn_cls.return_value = mock_conn

        form_body = b"device=127.0.0.1%3A5555&task_name=auto_quest&task_type=auto"
        msg = {
            "id": "req-2",
            "method": "POST",
            "path": "/tasks/start",
            "headers": {
                "Content-Type": "application/x-www-form-urlencoded",
            },
            "body_b64": base64.b64encode(form_body).decode("ascii"),
        }

        result = _forward_to_local(msg)

        assert result["id"] == "req-2"
        assert result["status"] == 302
        assert result["headers"]["Location"] == "/"
        mock_conn.request.assert_called_once_with(
            "POST", "/tasks/start", body=form_body,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

    @patch("tunnel.http.client.HTTPConnection")
    def test_redirect_not_followed(self, mock_conn_cls):
        """Redirects should be returned as-is, NOT followed."""
        mock_resp = MagicMock()
        mock_resp.status = 302
        mock_resp.read.return_value = b""
        mock_resp.getheaders.return_value = [
            ("Location", "/tasks"),
        ]

        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_resp
        mock_conn_cls.return_value = mock_conn

        msg = {
            "id": "req-3",
            "method": "POST",
            "path": "/tasks/start",
            "headers": {},
            "body_b64": "",
        }

        result = _forward_to_local(msg)

        # Should return 302 directly, not follow to the redirect target
        assert result["status"] == 302
        assert result["headers"]["Location"] == "/tasks"
        # Should only have made ONE request (not followed redirect)
        assert mock_conn.request.call_count == 1

    @patch("tunnel.http.client.HTTPConnection")
    def test_filters_host_header(self, mock_conn_cls):
        """host, transfer-encoding, connection headers should be stripped."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b""
        mock_resp.getheaders.return_value = []

        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_resp
        mock_conn_cls.return_value = mock_conn

        msg = {
            "id": "req-4",
            "method": "GET",
            "path": "/",
            "headers": {
                "Host": "relay.example.com",
                "transfer-encoding": "chunked",
                "Connection": "keep-alive",
                "Accept": "text/html",
            },
            "body_b64": "",
        }

        _forward_to_local(msg)

        _, kwargs = mock_conn.request.call_args
        forwarded_headers = kwargs["headers"]
        assert "Host" not in forwarded_headers
        assert "transfer-encoding" not in forwarded_headers
        assert "Connection" not in forwarded_headers
        assert forwarded_headers["Accept"] == "text/html"

    @patch("tunnel.http.client.HTTPConnection")
    def test_connection_error_returns_502(self, mock_conn_cls):
        """Connection refused returns 502."""
        mock_conn = MagicMock()
        mock_conn.request.side_effect = ConnectionRefusedError("Connection refused")
        mock_conn_cls.return_value = mock_conn

        msg = {
            "id": "req-5",
            "method": "GET",
            "path": "/",
            "headers": {},
            "body_b64": "",
        }

        result = _forward_to_local(msg)

        assert result["id"] == "req-5"
        assert result["status"] == 502
        body = base64.b64decode(result["body_b64"]).decode()
        assert "unreachable" in body.lower()

    @patch("tunnel.http.client.HTTPConnection")
    def test_empty_body_sends_none(self, mock_conn_cls):
        """Empty body_b64 should send body=None."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b""
        mock_resp.getheaders.return_value = []

        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_resp
        mock_conn_cls.return_value = mock_conn

        msg = {
            "id": "req-6",
            "method": "GET",
            "path": "/",
            "headers": {},
            "body_b64": "",
        }

        _forward_to_local(msg)

        _, kwargs = mock_conn.request.call_args
        assert kwargs["body"] is None

    @patch("tunnel.http.client.HTTPConnection")
    def test_filters_transfer_encoding_from_response(self, mock_conn_cls):
        """transfer-encoding and connection should be filtered from response."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"ok"
        mock_resp.getheaders.return_value = [
            ("Content-Type", "text/html"),
            ("transfer-encoding", "chunked"),
            ("connection", "keep-alive"),
        ]

        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_resp
        mock_conn_cls.return_value = mock_conn

        msg = {"id": "req-7", "method": "GET", "path": "/",
               "headers": {}, "body_b64": ""}

        result = _forward_to_local(msg)

        assert "transfer-encoding" not in result["headers"]
        assert "connection" not in result["headers"]
        assert result["headers"]["Content-Type"] == "text/html"


# ---------------------------------------------------------------------------
# Tunnel lifecycle tests
# ---------------------------------------------------------------------------

class TestTunnelStatus:
    def test_initial_status_is_disabled(self):
        assert tunnel_status() == "disabled"

    def test_status_after_stop(self):
        stop_tunnel()
        assert tunnel_status() == "disabled"


class TestStartTunnel:
    @patch("tunnel.asyncio.new_event_loop")
    def test_start_creates_thread(self, mock_loop):
        mock_loop.return_value = MagicMock()
        start_tunnel("ws://localhost/ws/tunnel", "secret", "bot1")
        assert tunnel._thread is not None
        assert tunnel._thread.is_alive()
        # Clean up
        tunnel._stop_event.set()
        tunnel._thread.join(timeout=2)

    @patch("tunnel.asyncio.new_event_loop")
    def test_start_twice_ignores_second(self, mock_loop):
        mock_loop.return_value = MagicMock()
        start_tunnel("ws://localhost/ws/tunnel", "secret", "bot1")
        first_thread = tunnel._thread
        start_tunnel("ws://localhost/ws/tunnel", "secret", "bot1")
        assert tunnel._thread is first_thread
        tunnel._stop_event.set()
        tunnel._thread.join(timeout=2)


class TestStopTunnel:
    def test_stop_sets_event(self):
        stop_tunnel()
        assert tunnel._stop_event.is_set()
        assert tunnel_status() == "disabled"
