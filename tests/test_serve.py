import http.client
import tempfile
import threading
import unittest
import http.server
import os

from nzfleet import serve


class TheAddress(unittest.TestCase):
    def test_a_tailnet_address_is_taken(self):
        self.assertEqual(serve.tailnet_address("100.64.12.34\n"), "100.64.12.34")

    def test_a_wildcard_is_never_taken(self):
        self.assertIsNone(serve.tailnet_address("0.0.0.0\n"))

    def test_a_lan_address_is_never_taken(self):
        self.assertIsNone(serve.tailnet_address("192.168.1.5\n"))

    def test_no_output_means_no_address(self):
        self.assertIsNone(serve.tailnet_address(""))

    def test_without_an_address_it_refuses_to_start(self):
        from unittest import mock
        with mock.patch.object(serve, "find_address", return_value=None), mock.patch.object(http.server, "ThreadingHTTPServer") as server:
            self.assertEqual(serve.run(0), 1)
        server.assert_not_called()


class TheServer(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "status.html")
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.handler(self.path))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def ask(self, method, path="/"):
        c = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1])
        c.request(method, path)
        r = c.getresponse()
        return r.status, r.read()

    def test_it_serves_the_page_as_written(self):
        with open(self.path, "w") as f:
            f.write("<p>hi</p>")
        self.assertEqual(self.ask("GET"), (200, b"<p>hi</p>"))

    def test_before_the_first_tick_it_says_so(self):
        self.assertEqual(self.ask("GET")[0], 503)

    def test_it_will_not_serve_any_other_file(self):
        self.assertEqual(self.ask("GET", "/../etc/passwd")[0], 404)

    def test_it_accepts_no_write(self):
        self.assertEqual([self.ask(m)[0] for m in ("POST", "PUT", "DELETE")], [405, 405, 405])


if __name__ == "__main__":
    unittest.main()
