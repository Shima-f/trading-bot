import os, sys, json, subprocess, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("PORT", 8080))

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/long":
            self._json("bot_state.json")
        elif path == "/short":
            self._json("bot_short_state.json")
        elif path in ("/", "/dashboard.html"):
            self._html("dashboard.html")
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/pause":
            Path("PAUSA.txt").write_text("pause requested")
            self._respond(200, {"status": "pausa solicitada"})
        elif path == "/close":
            qs = parse_qs(parsed.query)
            symbol = qs.get("symbol", [""])[0]
            side = qs.get("side", [""])[0]
            fname = f"CERRAR_{symbol}_{side}.txt"
            Path(fname).write_text(f"close {symbol} {side}")
            self._respond(200, {"status": f"cierre solicitado: {symbol} {side}"})
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

    def _respond(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

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
