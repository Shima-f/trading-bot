import os, sys, json, subprocess, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PORT = int(os.environ.get("PORT", 3001))

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/long":
            self._json("bot_state.json")
        elif path == "/short":
            self._json("bot_short_state.json")
        elif path in ("/", "/dashboard.html"):
            self._html("dashboard.html")
        else:
            self.send_response(404); self.end_headers()

    def _json(self, filename):
        try:
            data = Path(filename).read_text(encoding="utf-8")
        except FileNotFoundError:
            data = "{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data.encode())

    def _html(self, filename):
        try:
            data = Path(filename).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404); self.end_headers()

    def log_message(self, format, *args):
        pass

def run_server():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[WEB] Dashboard en http://0.0.0.0:{PORT}/", flush=True)
    server.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    python = sys.executable
    long_proc  = subprocess.Popen([python, "smart_bot.py"])
    short_proc = subprocess.Popen([python, "smart_bot_short.py"])
    print("[LAUNCHER] Bot LONG  PID:", long_proc.pid, flush=True)
    print("[LAUNCHER] Bot SHORT PID:", short_proc.pid, flush=True)
    long_proc.wait()
    short_proc.wait()
