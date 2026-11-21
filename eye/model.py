"""Optional reasoning providers share a JSON-in, JSON-out contract."""

import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from eye.devin import DevinReasoner

log = logging.getLogger(__name__)


def ask(model, role, system, prompt):
    if isinstance(model, DevinReasoner):
        return model.reason(role, system, prompt)
    return (model(system, prompt) if model else {}), {}


def configured_devin():
    key, org_id = os.getenv("DEVIN_API_KEY"), os.getenv("DEVIN_ORG_ID")
    if key and org_id:
        return DevinReasoner(key, org_id, mode=os.getenv("DEVIN_MODE"))
    return None


def configured_model():
    mode = os.getenv("EYE_MODEL")
    if mode == "devin":
        devin = configured_devin()
        if not devin:
            log.warning("Devin requires DEVIN_API_KEY and DEVIN_ORG_ID. Using local reasoning fallback.")
        return devin
    name = os.getenv("MODEL_NAME")
    key = os.getenv("MODEL_API_KEY") or os.getenv("OPENAI_API_KEY")
    base = os.getenv("MODEL_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    if mode != "codex" and not name:
        return None

    retry_at = 0.0

    def reason(system, prompt):
        nonlocal retry_at
        if time.monotonic() < retry_at:
            return {}
        content = json.dumps(prompt, default=str)
        try:
            if mode == "codex":
                with tempfile.TemporaryDirectory(prefix="eye-reasoning-") as directory:
                    output = Path(directory) / "answer.json"
                    subprocess.run(
                        ["codex", "exec", "--ephemeral", "--ignore-user-config",
                         "--sandbox", "read-only", "--skip-git-repo-check", "-C", directory,
                         "-o", str(output), "-"],
                        input=f"{system}\nUse only the supplied observation JSON. "
                              f"Do not use tools. Return only a JSON object.\n{content}",
                        text=True, capture_output=True, check=True, timeout=45, cwd=directory,
                    )
                    result = output.read_text()
            else:
                headers = {"Authorization": f"Bearer {key}"} if key else {}
                response = httpx.post(
                    f"{base}/chat/completions", headers=headers, timeout=30,
                    json={"model": name, "response_format": {"type": "json_object"},
                          "messages": [{"role": "system", "content": system},
                                       {"role": "user", "content": content}]},
                )
                response.raise_for_status()
                result = response.json()["choices"][0]["message"]["content"]
            if result.strip().startswith("```"):
                result = result.strip().split("\n", 1)[1].rsplit("```", 1)[0]
            answer = json.loads(result)
            return answer if isinstance(answer, dict) else {}
        except (httpx.HTTPError, subprocess.SubprocessError, OSError, ValueError, KeyError,
                IndexError, TypeError) as exc:
            log.warning("Reasoning provider unavailable (%s); using observed evidence.",
                        type(exc).__name__)
            retry_at = time.monotonic() + 60
            return {}

    return reason
