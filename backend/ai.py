"""AI "describe the room" → proposed equipment list.

Uses the Claude API when ``ANTHROPIC_API_KEY`` is set and the ``anthropic``
package is installed; otherwise falls back to a keyword heuristic so the
feature still produces a useful starting point offline (local-first).

Either way it returns device dicts in the same shape the proposal/review screen
and the build endpoints consume, so the downstream pipeline is unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger("avdraw.ai")

# Device categories the pipeline understands (DEFAULT_PORTS_BY_TYPE + aliases).
KNOWN_TYPES = [
    "codec", "camera", "display", "projector", "microphone", "wireless-mic",
    "wireless-receiver", "speaker", "amplifier", "audio-interface", "dsp",
    "control-panel", "touch-panel", "network-switch", "matrix-router",
    "switcher", "computer", "encoder", "decoder",
]

_MODELS = ("claude-sonnet-4-6", "claude-opus-4-8", "claude-haiku-4-5-20251001")

_PROMPT = """You are an expert AV systems engineer. From the room brief below, \
propose a realistic equipment list for a Cisco-friendly enterprise AV build.

Return ONLY a JSON array (no prose) of objects with these keys:
  "name"  : short device label
  "type"  : one of {types}
  "model" : a plausible real model number (or "")
  "quantity": integer >= 1

Brief:
{brief}
"""


def describe_room(brief: str) -> dict[str, Any]:
    """Return {devices: [...], source: 'ai'|'heuristic', model?, note?}."""
    brief = (brief or "").strip()
    if not brief:
        return {"devices": [], "source": "heuristic", "note": "empty brief"}

    ai = _ai_devices(brief)
    if ai is not None:
        return {"devices": _normalise(ai["devices"], confidence="high"),
                "source": "ai", "model": ai["model"]}

    return {"devices": _normalise(_heuristic_devices(brief), confidence="unknown"),
            "source": "heuristic",
            "note": "AI unavailable (set ANTHROPIC_API_KEY for smarter proposals) — "
                    "used a keyword starter. Edit freely."}


# ── Claude path ──────────────────────────────────────────────────────────────
def _ai_devices(brief: str):
    try:
        import anthropic
    except ModuleNotFoundError:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    client = anthropic.Anthropic(api_key=api_key)
    prompt = _PROMPT.format(types=", ".join(KNOWN_TYPES), brief=brief)
    for model in _MODELS:
        try:
            resp = client.messages.create(
                model=model, max_tokens=1500,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text
            devices = _extract_json_array(text)
            if devices:
                return {"devices": devices, "model": resp.model}
        except Exception as exc:  # try next model
            logger.warning("AI model %s failed: %s", model, exc)
    return None


def _extract_json_array(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


# ── Heuristic fallback ───────────────────────────────────────────────────────
def _heuristic_devices(brief: str) -> list[dict]:
    b = brief.lower()
    out: list[dict] = []

    def add(name, type_, model="", qty=1):
        out.append({"name": name, "type": type_, "model": model, "quantity": qty})

    def count_near(keyword, default):
        # "4 ceiling mics" / "dual display" → pull a leading number if present
        m = re.search(r"(\d+)\s*(?:x\s*)?[\w-]*\s*" + keyword, b)
        if m:
            return int(m.group(1))
        if "dual" in b and keyword in b:
            return 2
        return default

    # Codec / conferencing core for most rooms
    if any(k in b for k in ("conference", "boardroom", "meeting", "huddle", "webex", "zoom", "teams", "video call", "vc")):
        add("Video Codec", "codec", "CS-CODEC-PRO")
        add("PTZ Camera", "camera", "", count_near("camera", 1))

    # Displays / projection
    if "projector" in b or "projection" in b:
        add("Projector", "projector", "", count_near("projector", 1))
        if "screen" in b:
            add("Motorized Screen", "display", "")
    if "display" in b or "screen" in b or "tv" in b or "monitor" in b:
        add("Display", "display", "", count_near("display", 2 if "dual" in b else 1))

    # Microphones
    if "ceiling mic" in b or "ceiling microphone" in b:
        add("Ceiling Microphone", "microphone", "", count_near("ceiling mic", 2))
    if "wireless mic" in b or "handheld" in b or "lavalier" in b or "lav" in b:
        add("Wireless Mic", "wireless-mic", "", count_near("mic", 2))
        add("Wireless Receiver", "wireless-receiver", "")
    if "podium" in b or "lectern" in b:
        add("Gooseneck Mic", "microphone", "")

    # Audio
    if any(k in b for k in ("speaker", "audio", "sound", "ceiling speaker")):
        add("Loudspeaker", "speaker", "", count_near("speaker", 2))
        add("Amplifier", "amplifier", "")
    if "dsp" in b or "mixer" in b or "q-sys" in b or "tesira" in b:
        add("DSP", "audio-interface", "")

    # Switching / control
    if "switcher" in b or "matrix" in b or "multiple source" in b or "sources" in b:
        add("Matrix Switcher", "matrix-router", "")
    if "touch" in b or "control" in b or "control panel" in b:
        add("Touch Control Panel", "control-panel", "")

    # Sources
    if "pc" in b or "computer" in b or "room pc" in b:
        add("Room PC", "computer", "")
    if "laptop" in b or "byod" in b or "cubby" in b or "cable cubby" in b:
        add("Table Connection (BYOD)", "computer", "")

    # Networking
    if "network" in b or "switch" in b or "poe" in b or "dante" in b:
        add("Network Switch", "network-switch", "")

    if not out:  # generic boardroom starter
        add("Video Codec", "codec", "CS-CODEC-PRO")
        add("PTZ Camera", "camera", "")
        add("Display", "display", "", 2)
        add("Ceiling Microphone", "microphone", "", 2)
        add("Matrix Switcher", "matrix-router", "")
    return out


def _normalise(devices: list[dict], *, confidence: str) -> list[dict]:
    norm = []
    for d in devices:
        t = str(d.get("type", "")).strip().lower()
        norm.append({
            "name": str(d.get("name", "Device")).strip() or "Device",
            "type": t,
            "model": str(d.get("model", "")).strip(),
            "quantity": max(1, int(d.get("quantity", 1) or 1)),
            "confidence": confidence if t else "unknown",
            "attrs": {},
        })
    return norm
