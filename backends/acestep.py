"""ACE-Step audio backend integration.

The web app stays apt-only (just `requests`); ACE-Step itself runs in its own
environment behind a small HTTP adapter. The adapter contract this client
expects is intentionally minimal:

  POST {acestep_base_url}/generate
    body: {"tags": "...", "lyrics": "...", "duration": 180,
           "seed": 12345, "format": "wav"}
    -> 200 with audio bytes (Content-Type audio/*)   OR
    -> 200 JSON {"audio_base64": "..."} / {"audio_path": "/abs/path"}

  GET  {acestep_base_url}/health -> 200

A reference adapter implementing this contract ships as `acestep_adapter.py`.

When `mock_mode` is on, no network call is made: a short synthesized tone is
written locally so every downstream step (candidates, QA, playback) still works.
"""

import base64
import json
import math
import os
import struct
import wave
from email.parser import BytesParser
from email.policy import default as _email_policy

import requests

from database import all_settings


class ACEStepError(RuntimeError):
    pass


class ACEStepClient:
    def __init__(self, settings=None):
        s = settings or all_settings()
        self.base_url = s.get("acestep_base_url", "http://localhost:8765").rstrip("/")
        raw_fmt = (s.get("acestep_format", "wav16") or "wav16").lower()
        # acestep.cpp only accepts: mp3, wav16, wav24, wav32. Map the app's
        # value to a valid output_format, and separately keep a browser-/QA-
        # friendly file extension (.mp3 or .wav) for what we save on disk.
        _fmt_map = {"wav": "wav16", "wav16": "wav16", "wav24": "wav24",
                    "wav32": "wav32", "mp3": "mp3", "flac": "mp3"}
        self.synth_format = _fmt_map.get(raw_fmt, "mp3")
        self.fmt = "wav" if self.synth_format.startswith("wav") else "mp3"
        self.mock = str(s.get("mock_mode", "1")) in ("1", "true", "True", "on")
        try:
            self.duration_ceiling = int(s.get("acestep_duration_ceiling", 240))
        except (TypeError, ValueError):
            self.duration_ceiling = 240

    def ping(self, timeout=8):
        if self.mock:
            return True, "Mock mode (no backend required)"
        try:
            r = requests.get(f"{self.base_url}/health", timeout=timeout)
            r.raise_for_status()
            return True, f"Connected ({r.status_code})"
        except requests.RequestException as exc:
            return False, str(exc)

    def generate(self, tags, lyrics, duration, seed, out_path, timeout=600,
                 reference_audio=None, cover_strength=None, cover_noise=None,
                 vocal_language=None):
        """Render one audio file to out_path. Returns out_path.

        acestep.cpp's HTTP API is asynchronous (validated against ace-server.cpp):
          POST /synth                  -> {"id": "N"}     (queues a job)
          GET  /job?id=N               -> {"status": "running|done|failed|cancelled"}
          GET  /job?id=N&result=1      -> result body once done (multipart/mixed:
                                          one audio part + one latent part)
        We render text2music directly (no /lm) so the artist's own lyrics are
        used verbatim rather than being rewritten by acestep's LM planner.

        When `reference_audio` (a local file path) is given, this renders a
        *cover* (audio2audio): task_type="cover", with the source audio sent as
        the multipart `audio` part. `cover_strength` maps to
        audio_cover_strength (fraction of DiT steps that see the source) and
        `cover_noise` to cover_noise_strength.
        """
        duration = max(8, min(int(duration or 180), self.duration_ceiling))
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        if self.mock:
            _synthesize_placeholder(out_path, duration=min(duration, 8), seed=seed)
            return out_path

        req = {
            "caption": tags,
            "lyrics": lyrics or "",
            "duration": float(duration),
            "seed": seed,
            "output_format": self.synth_format,
            "task_type": "text2music",
        }
        if vocal_language:
            req["vocal_language"] = vocal_language
        src_audio = None
        if reference_audio and os.path.exists(reference_audio):
            req["task_type"] = "cover"
            if cover_strength is not None:
                try:
                    req["audio_cover_strength"] = float(cover_strength)
                except (TypeError, ValueError):
                    pass
            if cover_noise is not None:
                try:
                    req["cover_noise_strength"] = float(cover_noise)
                except (TypeError, ValueError):
                    pass
            with open(reference_audio, "rb") as fh:
                src_audio = (os.path.basename(reference_audio), fh.read())

        audio = self._synth(req, timeout, src_audio=src_audio)
        with open(out_path, "wb") as fh:
            fh.write(audio)
        return out_path

    def _synth(self, req, timeout, src_audio=None):
        """POST /synth and resolve the result to audio bytes, transparently
        handling both the async (job-id) build and any sync build that returns
        audio inline. When `src_audio` (name, bytes) is supplied, the request is
        sent as multipart with a `request` JSON part and an `audio` part — the
        source-audio field ace-server.cpp reads for cover/audio2audio (validated
        against tools/ace-server.cpp: has_file("audio"))."""
        if src_audio is not None:
            files = {
                "request": ("request.json", json.dumps(req), "application/json"),
                "audio": (src_audio[0] or "source.wav", src_audio[1],
                          "application/octet-stream"),
            }
            r = self._post_multipart("/synth", files, timeout)
        else:
            r = self._post("/synth", req, timeout)
        ctype = r.headers.get("Content-Type", "")
        if ctype.startswith("application/json"):
            try:
                data = r.json()
            except ValueError as exc:
                raise ACEStepError(f"/synth bad JSON: {(r.text or '')[:300]}") from exc
            job_id = data.get("id") if isinstance(data, dict) else None
            if job_id:                                   # async path
                mime, body = self._await_job(job_id, timeout)
                return self._result_to_audio(mime, body, timeout)
            audio = self._audio_from_json(data, timeout)  # inline json fallback
            if audio is None:
                raise ACEStepError("/synth returned JSON without audio: "
                                   + json.dumps(data)[:400])
            return audio
        return self._result_to_audio(ctype, r.content, timeout)  # sync build

    def _await_job(self, job_id, timeout):
        """Poll GET /job?id=... until done, then fetch GET /job?id=...&result=1.
        Returns (content_type, body_bytes)."""
        import time
        url = f"{self.base_url}/job"
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                s = requests.get(url, params={"id": job_id}, timeout=30)
            except requests.RequestException as exc:
                raise ACEStepError(f"/job poll failed: {exc}") from exc
            if s.status_code >= 400:
                raise ACEStepError(f"/job {s.status_code}: "
                                   + " ".join((s.text or "").split())[:200])
            try:
                status = (s.json() or {}).get("status", "")
            except ValueError:
                status = ""
            if status == "done":
                r = requests.get(url, params={"id": job_id, "result": "1"},
                                 timeout=timeout)
                if r.status_code >= 400:
                    raise ACEStepError(f"/job result {r.status_code}: "
                                       + " ".join((r.text or "").split())[:200])
                return r.headers.get("Content-Type", ""), r.content
            if status in ("failed", "cancelled"):
                raise ACEStepError(f"acestep job {status}")
            time.sleep(1.5)
        raise ACEStepError(f"acestep job timed out after {timeout}s")

    def _result_to_audio(self, mime, body, timeout):
        """Turn a synth result body into audio bytes by its content-type."""
        if mime.startswith("multipart/"):
            audio = _extract_audio_part(mime, body)
            if not audio:
                raise ACEStepError("no audio part in synth result")
            return audio
        if mime.startswith("audio/") or mime == "application/octet-stream":
            return body
        if mime.startswith("application/json"):
            try:
                data = json.loads(body.decode("utf-8", "replace"))
            except ValueError as exc:
                raise ACEStepError(f"synth result bad JSON: {body[:200]!r}") from exc
            audio = self._audio_from_json(data, timeout)
            if audio is None:
                raise ACEStepError("synth result JSON had no audio: "
                                   + json.dumps(data)[:400])
            return audio
        raise ACEStepError(f"unexpected synth result content-type: {mime or '(none)'}")

    def _audio_from_json(self, data, timeout):
        """Pull audio out of a JSON /synth response. Handles three shapes:
        an explicit error, inline base64, or a file path / URL reference.
        Returns audio bytes, or None if no audio could be found (the caller
        surfaces the raw body so the exact shape is visible)."""
        scopes = [data]
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            scopes.append(data["data"])

        for d in scopes:
            if not isinstance(d, dict):
                continue
            err = d.get("error") or d.get("detail") or d.get("message")
            if err and not (d.get("file") or d.get("audio_base64")):
                raise ACEStepError(f"/synth error: {err}")

        for d in scopes:                       # inline base64
            if not isinstance(d, dict):
                continue
            for k in ("audio_base64", "audioBase64", "base64", "audio", "wave"):
                v = d.get(k)
                if isinstance(v, str) and len(v) >= 16:
                    try:
                        raw = base64.b64decode(v, validate=True)
                    except ValueError:
                        continue
                    if len(raw) >= 8:
                        return raw

        for d in scopes:                       # file path or URL reference
            if not isinstance(d, dict):
                continue
            for k in ("file", "path", "audio_path", "url", "track", "output"):
                ref = d.get(k)
                if isinstance(ref, str) and ref:
                    got = self._fetch_audio_ref(ref, timeout)
                    if got:
                        return got
        return None

    def _fetch_audio_ref(self, ref, timeout):
        """Resolve a file/URL reference returned by /synth into audio bytes:
        an http(s) URL, a server route on the acestep host, or a local path
        (the app and acestep.cpp run on the same machine)."""
        try:
            if ref.startswith("http://") or ref.startswith("https://"):
                rr = requests.get(ref, timeout=timeout)
                if rr.status_code == 200 and rr.content:
                    return rr.content
            elif ref.startswith("/"):
                rr = requests.get(f"{self.base_url}{ref}", timeout=timeout)
                if rr.status_code == 200 and rr.content:
                    return rr.content
        except requests.RequestException:
            pass
        if os.path.exists(ref):                # same-host filesystem fallback
            with open(ref, "rb") as fh:
                return fh.read()
        return None

    def _post(self, path, body, timeout):
        """POST JSON, raising an ACEStepError that includes the server's own
        error text on a 4xx/5xx so failures are diagnosable, not opaque."""
        try:
            r = requests.post(f"{self.base_url}{path}", json=body, timeout=timeout)
        except requests.RequestException as exc:
            raise ACEStepError(f"{path} connection failed: {exc}") from exc
        if r.status_code >= 400:
            detail = " ".join((r.text or "").split())[:400] or "(no body)"
            raise ACEStepError(f"{path} {r.status_code}: {detail}")
        return r

    def _post_multipart(self, path, files, timeout):
        """POST a multipart/form-data body (used for cover/audio2audio, where
        the source audio rides alongside the JSON request)."""
        try:
            r = requests.post(f"{self.base_url}{path}", files=files, timeout=timeout)
        except requests.RequestException as exc:
            raise ACEStepError(f"{path} connection failed: {exc}") from exc
        if r.status_code >= 400:
            detail = " ".join((r.text or "").split())[:400] or "(no body)"
            raise ACEStepError(f"{path} {r.status_code}: {detail}")
        return r


def _extract_audio_part(content_type, body):
    """Pull the audio payload out of a multipart/mixed /synth response.

    acestep.cpp returns one audio part and one latent part per track. Prefer a
    part whose Content-Type is audio/*; if the server doesn't label it, fall
    back to the part not marked as the latent, then to the largest part.
    """
    msg = BytesParser(policy=_email_policy).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    parts = list(msg.iter_parts()) if msg.is_multipart() else [msg]

    for p in parts:                                   # 1. explicit audio/* type
        if p.get_content_type().startswith("audio/"):
            payload = p.get_payload(decode=True)
            if payload:
                return payload
    candidates = []                                   # 2. anything not a latent
    for p in parts:
        name = (p.get_param("name", "", header="content-disposition") or
                p.get_filename() or "").lower()
        payload = p.get_payload(decode=True)
        if not payload:
            continue
        if "latent" in name or p.get_content_type() == "application/x-latent":
            continue
        candidates.append(payload)
    if candidates:                                    # 3. largest remaining part
        return max(candidates, key=len)
    return None


def _synthesize_placeholder(out_path, duration=6, seed=0):
    """Write a short distinct tone as a stand-in render (16-bit mono WAV)."""
    framerate = 22050
    # Map the seed to a base pitch so different candidates sound different.
    base = 196.0 * (2 ** (((seed or 0) % 12) / 12.0))  # G3 up a chromatic step
    n = int(framerate * duration)
    amp = 12000
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(framerate)
        frames = bytearray()
        for i in range(n):
            t = i / framerate
            env = min(1.0, t * 4) * min(1.0, (duration - t) * 4)  # fade in/out
            wobble = 1 + 0.02 * math.sin(2 * math.pi * 5 * t)
            sample = amp * env * (
                0.6 * math.sin(2 * math.pi * base * wobble * t)
                + 0.3 * math.sin(2 * math.pi * base * 2 * t)
                + 0.1 * math.sin(2 * math.pi * base * 3 * t)
            )
            frames += struct.pack("<h", int(max(-32767, min(32767, sample))))
        wf.writeframes(bytes(frames))
    return out_path
