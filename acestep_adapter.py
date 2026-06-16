#!/usr/bin/env python3
"""Reference ACE-Step adapter for Music World.

Music World (the web app) is constrained to Debian apt packages and only ever
speaks HTTP to the audio backend. ACE-Step itself has a heavy ML dependency
stack (PyTorch, diffusers, etc.) that you install however ACE-Step recommends
— a venv, conda, or its Docker image. This thin adapter bridges the two: it
runs *inside* the ACE-Step environment and exposes the minimal HTTP contract
the web app expects.

    POST /generate
        {"tags": "...", "lyrics": "...", "duration": 180,
         "seed": 12345, "format": "wav"}
      -> 200, body = raw audio bytes, Content-Type: audio/wav

    GET /health
      -> 200 {"status": "ok", "pipeline": "loaded" | "lazy"}

Run it next to your ACE-Step checkpoints, then point Music World's Admin page
at this server's URL (default http://localhost:8765) and turn Mock mode OFF.

    pip install flask              # in the ACE-Step environment
    python3 acestep_adapter.py --checkpoint /path/to/ACE-Step-checkpoints

This file deliberately lives outside the apt-only web app and is the *only*
place the real ACE-Step package is imported.
"""

import argparse
import io
import os
import tempfile
import threading

from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

# The pipeline is loaded lazily on first request so /health works immediately
# and startup doesn't block on model weights.
_pipeline = None
_pipeline_lock = threading.Lock()
_config = {"checkpoint": None, "dtype": "bfloat16", "infer_steps": 60,
           "guidance_scale": 15.0}


def get_pipeline():
    """Load and cache the ACE-Step pipeline.

    Adjust this to match the ACE-Step version you have installed. The shape
    below follows the public `ACEStepPipeline` interface; if your build exposes
    a different entry point, this is the one function to change.
    """
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    with _pipeline_lock:
        if _pipeline is None:
            from acestep.pipeline_ace_step import ACEStepPipeline  # type: ignore
            _pipeline = ACEStepPipeline(
                checkpoint_dir=_config["checkpoint"],
                dtype=_config["dtype"],
                torch_compile=False,
            )
    return _pipeline


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "pipeline": "loaded" if _pipeline is not None else "lazy",
        "checkpoint": _config["checkpoint"],
    })


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json(force=True, silent=True) or {}
    tags = data.get("tags", "")
    lyrics = data.get("lyrics", "")
    duration = int(data.get("duration", 180))
    seed = int(data.get("seed", 0))
    fmt = data.get("format", "wav")

    if not tags and not lyrics:
        return jsonify({"error": "need tags or lyrics"}), 400

    out_dir = tempfile.mkdtemp(prefix="acestep_")
    out_path = os.path.join(out_dir, f"out.{fmt}")

    try:
        pipeline = get_pipeline()
        # ACE-Step's pipeline signature. `prompt` carries the style tags;
        # `lyrics` carries the lyric sheet (with [verse]/[chorus] markers).
        # `manual_seeds` makes a render reproducible for candidate selection.
        pipeline(
            prompt=tags,
            lyrics=lyrics,
            audio_duration=float(duration),
            infer_step=_config["infer_steps"],
            guidance_scale=_config["guidance_scale"],
            manual_seeds=str(seed),
            save_path=out_path,
            format=fmt,
        )
    except Exception as exc:  # surface a clean error to the web app
        return jsonify({"error": f"generation failed: {exc}"}), 500

    if not os.path.exists(out_path):
        return jsonify({"error": "pipeline produced no file"}), 500

    mimetype = {"wav": "audio/wav", "mp3": "audio/mpeg",
                "flac": "audio/flac"}.get(fmt, "application/octet-stream")
    with open(out_path, "rb") as fh:
        payload = fh.read()
    return send_file(io.BytesIO(payload), mimetype=mimetype,
                     download_name=f"render.{fmt}")


def main():
    ap = argparse.ArgumentParser(description="ACE-Step HTTP adapter for Music World")
    ap.add_argument("--checkpoint", help="Path to ACE-Step checkpoint directory")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--dtype", default="bfloat16")
    ap.add_argument("--infer-steps", type=int, default=60)
    ap.add_argument("--guidance-scale", type=float, default=15.0)
    ap.add_argument("--preload", action="store_true",
                    help="Load the pipeline at startup instead of on first request")
    args = ap.parse_args()

    _config["checkpoint"] = args.checkpoint or os.environ.get("ACESTEP_CHECKPOINT")
    _config["dtype"] = args.dtype
    _config["infer_steps"] = args.infer_steps
    _config["guidance_scale"] = args.guidance_scale

    if args.preload:
        get_pipeline()

    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
