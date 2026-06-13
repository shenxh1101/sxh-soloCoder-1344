import time
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", 8000))

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"Hello from {sys.argv[1] if len(sys.argv) > 1 else 'service'}".encode())

    def log_message(self, format, *args):
        print(f"[HTTP] {args[0]} {args[1]}")

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "service"
    print(f"Starting {name} on port {PORT}")
    server = HTTPServer(("127.0.0.1", PORT), HealthHandler)
    print(f"{name} ready and listening on :{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"{name} shutting down")
        server.server_close()
