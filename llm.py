"""Shared local-LLM (ollama) plumbing for the categorize/tags/vision stages.

A dependency-free wrapper over ollama's HTTP API (stdlib only, no SDK):
  * _ollama_chat(system, user) — one /api/chat turn, returns message content
  * generate(prompt, images, model) — one /api/generate call (used by vision's
    VLM frame description)
  * signal_text(item) — join a reel's caption + transcript + visual into one
    text blob for the LLM stages

Env vars:
- OLLAMA_HOST: ollama base URL (default "http://localhost:11434").
- OLLAMA_MODEL: model name (default "llama3.2").
"""

import json
import os
import urllib.request

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")


def _ollama_chat(system, user):
    """One chat turn against ollama; returns the raw message content string."""
    payload = json.dumps(
        {
            "model": OLLAMA_MODEL,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_HOST.rstrip("/") + "/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body.get("message", {}).get("content", "")


def generate(prompt, images=None, model=None):
    """One /api/generate call against ollama; returns the raw response string.

    Used by vision's VLM frame description: pass base64-encoded `images`.
    """
    body_obj = {
        "model": model or OLLAMA_MODEL,
        "stream": False,
        "prompt": prompt,
    }
    if images is not None:
        body_obj["images"] = images
    payload = json.dumps(body_obj).encode()
    req = urllib.request.Request(
        OLLAMA_HOST.rstrip("/") + "/api/generate",
        data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = json.loads(resp.read().decode())
    return (body.get("response") or "").strip()


def signal_text(item):
    # Feed all three signals to the LLM: the caption, the spoken transcript,
    # and the VLM scene description (visual). Each is NULL-safe, so a missing
    # column simply contributes an empty line.
    return (
        (item.get("caption") or "")
        + "\n" + (item.get("transcript") or "")
        + "\n" + (item.get("visual") or "")
    )
