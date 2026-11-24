import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from curl_cffi.requests import Headers

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mirror"))
sys.path.insert(0, str(ROOT / "deploy"))

import bridge
from run import nginx_configuration
from setup import port_list


class DeploymentTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "host": "mirror.example.test", "frame_host": "content.example.net",
            "port": 19443, "bridge_port": 19883, "ui_port": 19881,
            "frame_first": 21443, "frame_count": 4,
        }

    def test_routes_put_frames_on_distinct_ports_and_deny_internal_routes(self):
        text = nginx_configuration(self.config, Path("/preview"), "127.0.0.1", "a" * 64)
        self.assertIn("listen 127.0.0.1:19443 ssl http2;", text)
        self.assertIn("listen 127.0.0.1:21446 ssl;", text)
        self.assertIn("proxy_buffering off;", text)
        self.assertIn("proxy_request_buffering off;", text)
        self.assertIn("server_name content.example.net;", text)
        self.assertIn("location = /login_token { return 404; }", text)
        self.assertIn("location ^~ /__mirror_content/ { return 404; }", text)
        self.assertIn("proxy_pass http://127.0.0.1:19883/__mirror_content/$server_port", text)

    def test_port_overlap_fails(self):
        self.config["frame_first"] = self.config["port"] - 1
        with self.assertRaises(ValueError):
            port_list(self.config)


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        bridge.CONF = root / "accounts.json"
        bridge.FRAME_MAP = root / "frame-origins.json"
        bridge.FRAME_SHELL = root / "frame-shell.js"
        bridge.FRAME_HOST = "content.example.net"
        bridge.FRAME_FIRST = 21443
        bridge.FRAME_COUNT = 2
        self.secret = "a" * 64
        self.headers = {"X-Mirror-Bridge": self.secret}
        self.origin = "https://mirror.example.test:19443"
        bridge.write_config({"bridge_secret": self.secret, "accounts": [], "default_account": None})
        self.client = bridge.app.test_client()
        access = patch.object(bridge, "site_allowed", return_value=True)
        access.start()
        self.addCleanup(access.stop)

    def request(self, method, path, **options):
        return self.client.open(path, method=method, headers={**self.headers, **options.pop("headers", {})},
                                base_url=self.origin, **options)

    def test_empty_install_opens_account_manager_and_does_not_call_upstream(self):
        r = self.request("GET", "/login")
        self.assertEqual(r.location, "/mirror/accounts")
        saved = self.request("GET", "/mirror/state").get_json()
        self.assertEqual(saved["accounts"], [])
        self.assertIsNone(saved["selected"])
        self.assertEqual(self.request("GET", "/api/organizations").status_code, 401)
        direct = self.client.get("/mirror/state", base_url=self.origin)
        self.assertEqual(direct.status_code, 403)

    def test_add_and_switch_keep_data_private(self):
        def account_state():
            return self.request("GET", "/mirror/state").get_json()

        csrf = account_state()["csrf"]
        self.assertEqual(self.request("POST", "/mirror/add", json={}).status_code, 403)
        headers = {"Origin": self.origin, "X-Mirror-CSRF": csrf}
        with patch.object(bridge.requests, "Session") as session:
            fake_response = session.return_value.__enter__.return_value.get.return_value
            fake_response.status_code = 200
            fake_response.json.return_value = [{"id": "fixture-organization"}]
            for suffix in ("one", "two"):
                r = self.request("POST", "/mirror/add", headers=headers,
                                 json={"name": suffix, "sessionKey": "sk-ant-sid-" + suffix})
                self.assertEqual(r.status_code, 200)
        state = account_state()
        self.assertEqual(state["selected"], state["accounts"][0]["id"])
        selected = state["accounts"][1]["id"]
        r = self.request("POST", "/mirror/switch", headers=headers, json={"id": selected})
        self.assertEqual(r.get_json(), {"next": "/mirror/resume"})
        self.assertEqual(account_state()["selected"], selected)
        self.assertNotIn("session_key", json.dumps(account_state()))

    def test_frame_source_follows_new_host_and_unknown_ports_are_closed(self):
        ident = "11111111-1111-1111-1111-111111111111"
        port = bridge.frame_port(ident)
        bridge.FRAME_SHELL.write_text("'./engine.js'; const host=`${go()}//${Xc}`;")
        r = self.request("GET", "/mirror/frame-shell.js?port=" + str(port))
        self.assertIn(b"https://content.example.net:21443", r.data)
        self.assertEqual(self.request("GET", "/mirror/frame-shell.js?port=21444").status_code, 404)

    def upstream_response(self, headers, chunks, status=200):
        bridge.write_config({
            "bridge_secret": self.secret, "default_account": "fixture",
            "accounts": [{"id": "fixture", "name": "Fixture", "session_key": "testing-only"}],
        })
        patches = [patch.object(bridge.pool, method) for method in ("acquire", "release")]
        for patcher in patches:
            self.addCleanup(patcher.stop)
        acquire, release = [patcher.start() for patcher in patches]
        session = acquire.return_value
        response = session.request.return_value
        response.status_code = status
        response.headers = Headers(headers)
        response.iter_content.side_effect = lambda: iter(chunks)
        return session, response, release

    def test_sse_forwards_first_event_before_upstream_finishes_and_releases_on_close(self):
        first = b'event: delta\ndata: {"type":"thinking_delta","thinking":"visible fixture"}\n\n'
        second = b'event: delta\ndata: {"type":"text_delta","text":"answer"}\n\n'
        read = []

        def chunks():
            for item in (first, second):
                read.append(item)
                yield item

        session, upstream, release = self.upstream_response(
            {"Content-Type": "text/event-stream"}, chunks()
        )
        result = self.request("POST", "/api/example/completion", json={"text": "test"}, buffered=False)
        with result:
            self.assertEqual(result.mimetype, "text/event-stream")
            self.assertEqual(next(iter(result.response)), first)
            self.assertEqual(read, [first])
            self.assertTrue(session.request.call_args.kwargs["stream"])
        upstream.close.assert_called_once()
        release.assert_called_once_with(session)

    def test_upload_preserves_multipart_bytes_and_repeated_query_parameters(self):
        payload = (b'--fixture\r\nContent-Disposition: form-data; name="file"; filename="example.bin"'
                   b'\r\nContent-Type: application/octet-stream\r\n\r\n\x00\xff\r\n--fixture--\r\n')
        session, _, _ = self.upstream_response({"Content-Type": "application/json"}, [b'{"ok":true}'])
        result = self.request("POST", "/api/files?tag=one&tag=two&empty=&path=A%2BB",
                              content_type="multipart/form-data; boundary=fixture", data=payload)
        with result:
            self.assertEqual(result.get_json(), {"ok": True})
        options = session.request.call_args.kwargs
        self.assertEqual(options["data"], payload)
        self.assertEqual(options["headers"]["Content-Type"], "multipart/form-data; boundary=fixture")
        self.assertEqual(options["params"], [("tag", "one"), ("tag", "two"), ("empty", ""), ("path", "A+B")])

    def test_binary_download_preserves_range_body_and_infers_file_type(self):
        session, _, _ = self.upstream_response({
            "Content-Disposition": "attachment; filename=example.docx",
            "Content-Range": "bytes 0-3/8",
        }, [b"PK\x03\x04"], status=206)
        result = self.request("GET", "/api/files/example/contents", headers={"Range": "bytes=0-3"})
        with result:
            self.assertEqual(result.status_code, 206)
            self.assertEqual(result.data, b"PK\x03\x04")
            self.assertEqual(result.headers["Content-Range"], "bytes 0-3/8")
            self.assertEqual(result.mimetype,
                             "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertEqual(session.request.call_args.kwargs["headers"]["Range"], "bytes=0-3")


if __name__ == "__main__":
    unittest.main()
