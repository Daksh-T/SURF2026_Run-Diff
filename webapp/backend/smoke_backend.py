"""Start the frozen sidecar with temporary data and check health and API-key storage."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


def check(binary: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    def request(path: str, value: dict | None = None):
        body = json.dumps(value).encode() if value is not None else None
        req = urllib.request.Request(base + path, data=body, method="PUT" if body is not None else "GET",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as response:
            return json.load(response)

    with tempfile.TemporaryDirectory(prefix="rundiff-sidecar-check-") as directory:
        data = Path(directory) / "data"
        env = {**os.environ, "HOST": "127.0.0.1", "PORT": str(port), "TUTOR_DATA_DIR": str(data),
               "groq_api_key": "", "aistudio_api_key": ""}
        with (Path(directory) / "backend.log").open("w+") as log:
            proc = subprocess.Popen([str(binary)], cwd=binary.parent, env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        raise RuntimeError(f"Sidecar stopped during startup ({proc.returncode}).")
                    try:
                        request("/api/health")
                        break
                    except OSError:
                        time.sleep(0.2)
                else:
                    raise RuntimeError("Sidecar did not become healthy within 30 seconds.")
                assert request("/api/instructor/api-key") == {"configured": False}
                key = "rundiff-smoke-test"
                assert request("/api/instructor/api-key", {"api_key": key}) == {"configured": True}
                assert request("/api/instructor/api-key") == {"configured": True}
                assert json.loads((data / "config.json").read_text())["groq_api_key"] == key
                assert key not in json.dumps(request("/api/instructor/config"))
                print("Verified frozen-backend startup, health, and local API-key storage.")
            except Exception:
                log.seek(0)
                print(log.read())
                raise
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("binary", nargs="?", type=Path,
                        default=Path(__file__).parent / "dist_backend" / "rundiff-backend" /
                        ("rundiff-backend.exe" if os.name == "nt" else "rundiff-backend"))
    check(parser.parse_args().binary.resolve())
