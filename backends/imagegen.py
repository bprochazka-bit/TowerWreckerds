"""Image generation backend (stable-diffusion.cpp / sd.cpp).

Generates artwork — band/artist portraits and album covers — by shelling out
to a local `sd` / sd.cpp executable. The app stays apt-only: the only hard
dependency is the standard library, and the heavy lifting happens in the
external binary the user points us at in Admin.

  <sdcpp_path> -m <model> -p "<prompt>" -o <out.png> --steps N -W S -H S [--cfg-scale C] [-s seed]

When no executable/model is configured, or when Mock mode is on, a small
deterministic placeholder PNG is written instead (pure stdlib via zlib) so the
whole publish/cover flow still works end to end with no backend.
"""

import os
import struct
import subprocess
import zlib

from database import all_settings


class ImageGenError(RuntimeError):
    pass


class ImageGenClient:
    def __init__(self, settings=None):
        s = settings or all_settings()
        self.exe = (s.get("sdcpp_path", "") or "").strip()
        self.model = (s.get("sdcpp_model", "") or "").strip()
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
    def available(self):
        """True when a real sd.cpp render can be attempted."""
        return bool(self.exe and self.model and os.path.exists(self.exe))

    def ping(self, timeout=10):
        if self.mock or not self.exe:
            return True, "Mock mode (placeholder images, no sd.cpp required)"
        if not os.path.exists(self.exe):
            return False, f"executable not found: {self.exe}"
        if not self.model:
            return False, "no model configured"
        if not os.path.exists(self.model):
            return False, f"model not found: {self.model}"
        try:
            subprocess.run([self.exe, "--help"], capture_output=True,
                           timeout=timeout)
            return True, f"sd.cpp ready ({os.path.basename(self.exe)})"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)

    def generate(self, prompt, out_path, seed=0, timeout=900):
        """Render one image to out_path (PNG). Returns out_path."""
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        if self.mock or not self.available:
            _placeholder_png(out_path, seed=seed, size=min(self.size, 512))
            return out_path
        cmd = [
            self.exe, "-m", self.model, "-p", prompt, "-o", out_path,
            "--steps", str(self.steps), "-W", str(self.size),
            "-H", str(self.size), "--cfg-scale", str(self.cfg),
        ]
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
