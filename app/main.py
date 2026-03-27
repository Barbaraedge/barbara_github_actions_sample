from http.server import BaseHTTPRequestHandler, HTTPServer
import os
import subprocess


def read_version() -> str:
    return os.environ.get("BARBARA_APP_VERSION", "unknown").strip()


def read_git_short_sha() -> str | None:
    env_sha = os.environ.get("BARBARA_GIT_SHA_SHORT", "").strip()
    if env_sha:
        return env_sha

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    sha = result.stdout.strip()
    return sha or None


def read_full_version() -> str:
    version = read_version()
    git_sha = read_git_short_sha()
    if not git_sha:
        return version
    return f"{version}+{git_sha}"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            version = read_full_version()
            body = f"Everything works fine, version {version}\n".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if self.path == "/health":
            body = b"OK\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8080), Handler)
    server.serve_forever()
