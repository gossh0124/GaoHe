from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Settings, load_settings


def render_status_page(settings: Settings) -> str:
    rows = "".join(
        f"<tr><th>{escape(label)}</th><td>{escape(value)}</td></tr>"
        for label, value in (
            ("LLM provider", settings.llm_provider),
            ("Gemini model", settings.gemini_model),
            ("Search provider", settings.search_provider),
            ("Data directory", str(settings.data_dir)),
            ("API key", "present" if settings.has_api_key else "missing"),
        )
    )
    return (
        "<!doctype html>"
        "<html lang='en'><head><meta charset='utf-8'>"
        "<title>GaoHe local runtime</title></head><body>"
        "<main><h1>GaoHe local runtime</h1>"
        f"<table><tbody>{rows}</tbody></table></main>"
        "</body></html>"
    )


def _handler_for(settings: Settings):
    class StatusHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/":
                self.send_error(404)
                return
            payload = render_status_page(settings).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            return

    return StatusHandler


def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    env_file: Path = Path(".env"),
) -> None:
    settings = load_settings(env_file)
    server = ThreadingHTTPServer((host, port), _handler_for(settings))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
