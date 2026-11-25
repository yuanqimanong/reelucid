"""Test-only HTTP CONNECT fixture: return HTTP 412 without connecting upstream."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Fixture(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        # Never log the synthetic query string, or any Authorization header.
        pass

    def do_CONNECT(self) -> None:
        self.close_connection = True
        if self.path != "93.184.216.34:80":
            self.send_error(403)
            return
        self.connection.settimeout(10)
        self.send_response_only(200, "Connection Established")
        self.end_headers()
        self.wfile.flush()
        request = self.rfile.readline(8192)
        for _ in range(100):
            line = self.rfile.readline(8192)
            if line in (b"\r\n", b""):
                break
        else:
            return
        if not request.startswith(b"GET /reelucid-smoke.mp4?access=release-smoke-secret "):
            return
        body = b"synthetic HTTP 412; release-smoke-secret"
        self.wfile.write(
            b"HTTP/1.1 412 Precondition Failed\r\nContent-Type: text/plain\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
            + body
        )
        self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 18081), Fixture).serve_forever()
