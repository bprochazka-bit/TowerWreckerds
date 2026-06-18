"""Image generation backend (stable-diffusion.cpp / sd.cpp).

Generates artwork — band/artist portraits and album covers — in one of three
ways, chosen by what's configured in Admin:

  1. HTTP web UI (sdcpp_url) — an AUTOMATIC1111-compatible server. We POST to
     `/sdapi/v1/txt2img` and read back a base64 PNG. Takes precedence when set.
  2. Local executable (sdcpp_path + sdcpp_model) — shell out to the `sd` binary.
  3. Placeholder — when nothing is configured or Mock mode is on, a small
     deterministic PNG is written (pure stdlib via zlib) so the publish/cover
     flow still works end to end with no backend.

Only `requests` (python3-requests, apt) and the stdlib are used.
"""

import base64
import os
import struct
import subprocess
import zlib

import requests

from database import all_settings


class ImageGenError(RuntimeError):
    pass


class ImageGenClient:
    def __init__(self, settings=None):
        s = settings or all_settings()
        self.url = (s.get("sdcpp_url", "") or "").strip().rstrip("/")
        self.exe = (s.get("sdcpp_path", "") or "").strip()
        self.model = (s.get("sdcpp_model", "") or "").strip()
        self.negative = (s.get("sdcpp_negative", "") or "").strip()
        self.mock = str(s.get("mock_mode", "1")) in ("1", "true", "True", "on")
        try:
            self.steps = max(1, int(s.get("sdcpp_steps", 20)))
        except (TypeError, ValueError):
            self.steps = 20
        try:
            self.size = max(64, int(s.get("sdcpp_size", 512)))
        except (TypeError, ValueError):
            self.size = 512
        try:
            self.cfg = float(s.get("sdcpp_cfg", 7.0))
        except (TypeError, ValueError):
            self.cfg = 7.0

    @property
    def mode(self):
        if self.mock:
            return "mock"
        if self.url:
            return "http"
        if self.exe and self.model and os.path.exists(self.exe):
            return "cli"
        return "mock"

    def ping(self, timeout=10):
        mode = self.mode
        if mode == "mock":
            if not self.mock and (self.exe or self.url):
                return False, "configured but unreachable (check URL or executable/model paths)"
            return True, "Mock mode (placeholder images, no backend required)"
        if mode == "http":
            try:
                r = requests.get(f"{self.url}/sdapi/v1/sd-models", timeout=timeout)
                if r.status_code == 404:  # endpoint differs but server is up
                    r = requests.get(f"{self.url}/", timeout=timeout)
                r.raise_for_status()
                return True, f"Web UI reachable ({self.url})"
            except requests.RequestException as exc:
                return False, str(exc)
        # cli
        if not os.path.exists(self.model):
            return False, f"model not found: {self.model}"
        try:
            subprocess.run([self.exe, "--help"], capture_output=True, timeout=timeout)
            return True, f"sd.cpp ready ({os.path.basename(self.exe)})"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)

    def generate(self, prompt, out_path, seed=0, timeout=900,
                 init_image=None, strength=0.6):
        """Render one image to out_path (PNG). Returns out_path.

        If `init_image` (a path to a source PNG) is given, runs img2img with
        `strength` as the denoising strength so the output is guided by it."""
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        if init_image and not os.path.exists(init_image):
            init_image = None
        mode = self.mode
        if mode == "http":
            return self._http_generate(prompt, out_path, seed, timeout,
                                       init_image, strength)
        if mode == "cli":
            return self._cli_generate(prompt, out_path, seed, timeout,
                                      init_image, strength)
        _placeholder_png(out_path, seed=seed, size=min(self.size, 512))
        return out_path

    def _http_generate(self, prompt, out_path, seed, timeout,
                       init_image=None, strength=0.6):
        body = {
            "prompt": prompt,
            "negative_prompt": self.negative,
            "steps": self.steps,
            "width": self.size,
            "height": self.size,
            "cfg_scale": self.cfg,
            "seed": int(seed) if seed is not None else -1,
            "batch_size": 1,
        }
        endpoint = "/sdapi/v1/txt2img"
        if init_image:
            with open(init_image, "rb") as fh:
                body["init_images"] = [base64.b64encode(fh.read()).decode("ascii")]
            body["denoising_strength"] = float(strength)
            endpoint = "/sdapi/v1/img2img"
        try:
            r = requests.post(f"{self.url}{endpoint}", json=body, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as exc:
            raise ImageGenError(f"sd web UI request failed: {exc}") from exc
        except ValueError as exc:
            raise ImageGenError(f"sd web UI returned non-JSON: {exc}") from exc
        images = data.get("images") if isinstance(data, dict) else None
        if not images:
            raise ImageGenError("sd web UI returned no images")
        b64 = images[0].split(",", 1)[-1]  # tolerate a data: URI prefix
        try:
            raw = base64.b64decode(b64)
        except (ValueError, TypeError) as exc:
            raise ImageGenError(f"sd web UI image not decodable: {exc}") from exc
        if len(raw) < 64:
            raise ImageGenError("sd web UI image too small")
        with open(out_path, "wb") as fh:
            fh.write(raw)
        return out_path

    def _cli_generate(self, prompt, out_path, seed, timeout,
                      init_image=None, strength=0.6):
        cmd = [
            self.exe, "-m", self.model, "-p", prompt, "-o", out_path,
            "--steps", str(self.steps), "-W", str(self.size),
            "-H", str(self.size), "--cfg-scale", str(self.cfg),
        ]
        if init_image:
            cmd += ["-M", "img2img", "-i", init_image, "--strength", str(strength)]
        if self.negative:
            cmd += ["-n", self.negative]
        if seed is not None:
            cmd += ["-s", str(int(seed))]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ImageGenError(f"sd.cpp failed to run: {exc}") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace")
            raise ImageGenError(f"sd.cpp exited {proc.returncode}: "
                                + " ".join(tail.split())[:400])
        if not os.path.exists(out_path) or os.path.getsize(out_path) < 64:
            raise ImageGenError("sd.cpp produced no image")
        return out_path


def _placeholder_png(out_path, seed=0, size=512):
    """Write a deterministic colored-gradient PNG using only the stdlib.

    No PIL: we build raw RGB scanlines, zlib-compress them, and frame the
    PNG chunks by hand. The seed picks a hue so different subjects look
    different at a glance.
    """
    seed = int(seed or 0)
    # Two corner colors derived from the seed for a simple diagonal gradient.
    r0, g0, b0 = (50 + seed * 53) % 206, (40 + seed * 97) % 206, (60 + seed * 29) % 206
    r1, g1, b1 = (120 + seed * 17) % 206 + 40, (90 + seed * 41) % 166 + 40, (110 + seed * 73) % 166 + 40

    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type 0 (None) for this scanline
        fy = y / (size - 1) if size > 1 else 0
        for x in range(size):
            fx = x / (size - 1) if size > 1 else 0
            t = (fx + fy) / 2
            raw.append(int(r0 + (r1 - r0) * t) & 0xFF)
            raw.append(int(g0 + (g1 - g0) * t) & 0xFF)
            raw.append(int(b0 + (b1 - b0) * t) & 0xFF)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
           + chunk(b"IEND", b""))
    with open(out_path, "wb") as fh:
        fh.write(png)
    return out_path
