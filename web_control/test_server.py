"""HTTP boundary tests use demo state and an ephemeral loopback port only."""

import http.client
import json
import threading
import unittest

try:
    from .controller import Controller
    from .server import LocalServer
except ImportError:
    from controller import Controller
    from server import LocalServer


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.control = Controller(mode="demo")
        for _ in range(4):
            self.control.step()
        self.server = LocalServer(("127.0.0.1", 0), self.control)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.control.close()

    def request(self, method="GET", path="/api/status", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None, headers or {})
            response = connection.getresponse()
            payload = response.read()
            return response.status, json.loads(payload) if payload else None, dict(response.getheaders())
        finally:
            connection.close()

    def auth(self):
        return {"Content-Type": "application/json", "Origin": "http://127.0.0.1:" + str(self.port),
                "X-CSRF-Token": self.control.csrf_token}

    def test_status_is_read_only_and_has_connection_fence(self):
        status, data, headers = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(data["board"]["state"], "ready")
        self.assertEqual(len(data["board"]["token"]), 32)
        self.assertFalse(data["tractor"]["confirmed"])
        self.assertEqual(self.control.memory_store.next_counter, 0xF6)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_post_requires_loopback_host_origin_and_csrf(self):
        body = {"confirmed": True, "board_token": self.control.board["token"]}
        for headers in ({}, {"Content-Type": "application/json"},
                        dict(self.auth(), Origin="http://evil.example"),
                        dict(self.auth(), Host="evil.example"),
                        dict(self.auth(), **{"X-CSRF-Token": "wrong"})):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/api/power", body, headers)[0], 403)
        self.assertFalse(self.control.confirmed)
        self.assertEqual(self.request(headers={"Host": "evil.example"})[0], 403)

    def test_power_hold_release_and_stale_connection_token(self):
        board = self.control.board["token"]
        self.assertEqual(self.request("POST", "/api/power", {"confirmed": True, "board_token": board}, self.auth())[0], 200)
        hold = {"client_id": "a" * 32, "seq": 1, "action": "tower_right", "board_token": board}
        self.assertEqual(self.request("POST", "/api/control", hold, self.auth())[0], 200)
        self.assertEqual(self.request("POST", "/api/control", dict(hold, seq=2, board_token="0" * 32), self.auth())[0], 409)
        self.assertEqual(self.request("POST", "/api/stop", {"client_id": "a" * 32, "seq": 3}, self.auth())[0], 200)
        self.assertIsNone(self.control.action)
        self.assertEqual(self.request("POST", "/api/control", dict(hold, seq=2), self.auth())[0], 409)

    def test_invalid_json_and_actions_do_not_allocate_counters(self):
        headers = self.auth()
        self.assertEqual(self.request("POST", "/api/power", {"confirmed": "yes", "board_token": self.control.board["token"]}, headers)[0], 400)
        body = {"client_id": "a" * 32, "seq": 1, "action": "arbitrary-payload", "board_token": self.control.board["token"]}
        self.assertEqual(self.request("POST", "/api/control", body, headers)[0], 400)
        self.assertEqual(self.control.memory_store.next_counter, 0xF6)

    def test_server_refuses_non_loopback_bind(self):
        with self.assertRaises(ValueError):
            LocalServer(("0.0.0.0", 0), self.control)


if __name__ == "__main__":
    unittest.main()
