import time
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", 8000))
FAIL_MODE = os.environ.get("FAIL_MODE", "")

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            if FAIL_MODE == "404":
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error": "not found"}')
            elif FAIL_MODE == "500":
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b'{"error": "internal error"}')
            elif FAIL_MODE == "403":
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b'{"error": "forbidden"}')
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    def log_message(self, format, *args):
        print(f"[{self.address_string()}] {args[0]}")

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "service"
    print(f"Starting {name} on port {PORT} (fail_mode={FAIL_MODE})")
    server = HTTPServer(("127.0.0.1", PORT), HealthHandler)
    print(f"{name} ready and listening on :{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
