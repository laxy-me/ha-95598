"""LLM-based vision solver for Tencent-style point-click CAPTCHAs.

The local image solver (`PointClickImageSolver`) uses a combination of
shape/mask matching and OCR. It works on simple captchas but struggles
with mixed glyphs, partially occluded targets, and font variations the
背景图 throws at it. When that pipeline returns no high-confidence
solution we fall back to this LLM solver: ask a vision-capable LLM to
read both the answer prompt and the candidate background image, return
pixel coordinates for each clickable target in the order specified by
the prompt.

The solver is provider-agnostic but defaults to 智谱 ``glm-4v-flash``
(free tier, OpenAI-compatible API, accessible from inside China without
a VPN). Configure via env vars:

* ``LLM_API_KEY`` — required to enable LLM fallback.
* ``LLM_PROVIDER`` — one of ``zhipu`` (default), ``openai``,
  ``custom``. ``custom`` requires ``LLM_BASE_URL``.
* ``LLM_MODEL`` — override the default model name for the provider.
* ``LLM_BASE_URL`` — override the API base URL.

Empty ``LLM_API_KEY`` disables the solver — callers should fall through
to the local solver / QR-code fallback.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

from PIL import Image


_PROVIDER_DEFAULTS = {
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4v-flash",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
}


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    timeout: float = 30.0


def load_llm_config_from_env() -> Optional[LLMConfig]:
    api_key = (os.getenv("LLM_API_KEY") or "").strip()
    if not api_key:
        return None
    provider = (os.getenv("LLM_PROVIDER") or "zhipu").strip().lower()
    defaults = _PROVIDER_DEFAULTS.get(provider, _PROVIDER_DEFAULTS["zhipu"])
    base_url = (os.getenv("LLM_BASE_URL") or defaults["base_url"]).rstrip("/")
    model = (os.getenv("LLM_MODEL") or defaults["model"]).strip()
    return LLMConfig(api_key=api_key, base_url=base_url, model=model)


class LLMPointClickSolver:
    """Ask a vision LLM where to click on a point-click captcha background."""

    DEFAULT_PROMPT = (
        "你是一个验证码识别助手。下面会给你两张图：\n"
        "- 第一张是「目标」：腾讯防水墙点选验证码顶部要求点击的字 / 物体序列。\n"
        "- 第二张是「背景」：包含若干候选字 / 物体的背景图。\n"
        "请按目标序列从左到右的顺序，找到背景图中对应每一个字 / 物体的中心点像素坐标。\n"
        "只输出 JSON，不要任何解释。格式必须严格如下：\n"
        '{"points": [{"x": int, "y": int}, ...]}\n'
        "x 和 y 是相对背景图左上角 (0,0) 的像素坐标。\n"
        "如果背景中找不到某个目标，请返回空 points 数组：{\"points\": []}。\n"
    )

    def __init__(self, config: LLMConfig, prompt: Optional[str] = None):
        self._config = config
        self._prompt = prompt or self.DEFAULT_PROMPT

    @staticmethod
    def _encode_png(image: Image.Image) -> str:
        buf = BytesIO()
        image.convert("RGB").save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def _call(self, answer_image: Image.Image, bg_image: Image.Image) -> dict:
        answer_b64 = self._encode_png(answer_image)
        bg_b64 = self._encode_png(bg_image)
        endpoint = f"{self._config.base_url}/chat/completions"
        payload = {
            "model": self._config.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._prompt},
                        {"type": "text", "text": "目标图（要点击的字 / 物体序列）："},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{answer_b64}"},
                        },
                        {"type": "text", "text": "背景图："},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{bg_b64}"},
                        },
                    ],
                }
            ],
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._config.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _extract_text(api_response: dict) -> str:
        try:
            return api_response["choices"][0]["message"]["content"] or ""
        except Exception:
            return ""

    @staticmethod
    def _parse_points(text: str) -> list[tuple[int, int]]:
        if not text:
            return []
        # LLMs (especially glm-4v) commonly wrap their JSON answer in a
        # ```json … ``` markdown fence even when prompted "JSON only".
        # Strip the fence first so the body can be parsed directly.
        cleaned = text.strip()
        fence = re.match(r"^```(?:json|JSON)?\s*\n?(.*?)\n?```\s*$", cleaned, re.DOTALL)
        if fence:
            cleaned = fence.group(1).strip()

        data = None
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            # Fall back to scanning for the outermost balanced `{ … }` that
            # contains `"points"`. The previous non-greedy regex matched the
            # *innermost* brace pair instead — e.g. `{"x":1,"y":2}` from
            # within the points array — so json.loads would always fail.
            start = cleaned.find("{")
            while start != -1 and data is None:
                depth = 0
                for i in range(start, len(cleaned)):
                    ch = cleaned[i]
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            candidate = cleaned[start : i + 1]
                            if '"points"' in candidate:
                                try:
                                    data = json.loads(candidate)
                                except json.JSONDecodeError:
                                    pass
                            break
                if data is not None:
                    break
                start = cleaned.find("{", start + 1)
        if not isinstance(data, dict):
            return []
        points = data.get("points") or []
        out: list[tuple[int, int]] = []
        for p in points:
            try:
                x = int(round(float(p["x"])))
                y = int(round(float(p["y"])))
                out.append((x, y))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def solve(
        self,
        answer_image: Image.Image,
        bg_image: Image.Image,
    ) -> list[tuple[int, int]]:
        """Return a list of ``(x, y)`` click points in target order.

        Coordinates are in the **background image's pixel space**
        (origin = top-left). Caller is responsible for any scaling to
        the actual rendered element size.
        """
        try:
            response = self._call(answer_image, bg_image)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            logging.warning("LLM captcha solver network error: %s", exc)
            return []
        except Exception as exc:  # pragma: no cover - defensive
            logging.warning("LLM captcha solver unexpected error: %s", exc)
            return []
        text = self._extract_text(response)
        points = self._parse_points(text)
        if not points:
            logging.info(
                "LLM solver returned no parseable points. model=%s, raw_text=%r",
                self._config.model,
                text[:200],
            )
        else:
            logging.info(
                "LLM solver predicted %s point(s): %s",
                len(points),
                points,
            )
        return points
