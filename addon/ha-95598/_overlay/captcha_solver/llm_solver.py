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
    # Anthropic uses its own Messages API (NOT OpenAI-compatible) — different
    # auth header (x-api-key + anthropic-version), different content block
    # shape (`{"type": "image", "source": {...}}` vs OpenAI's `image_url`),
    # different response format (`content[0].text` vs `choices[0].message`).
    # Picked claude-haiku-4-5 as default: cheapest current-gen Anthropic
    # vision model, ~$1/MTok input / ~$5/MTok output — captcha calls are
    # ~1-2k tokens so each attempt is fractions of a cent. Bump to
    # claude-sonnet-4-6 if haiku turns out underpowered on 95598's AI 3D
    # scenes (sonnet is ~10x cost but reliably solves visually-dense scenes).
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-haiku-4-5",
    },
}


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    provider: str = "zhipu"
    timeout: float = 30.0


def load_llm_config_from_env() -> Optional[LLMConfig]:
    api_key = (os.getenv("LLM_API_KEY") or "").strip()
    if not api_key:
        return None
    provider = (os.getenv("LLM_PROVIDER") or "zhipu").strip().lower()
    defaults = _PROVIDER_DEFAULTS.get(provider, _PROVIDER_DEFAULTS["zhipu"])
    base_url = (os.getenv("LLM_BASE_URL") or defaults["base_url"]).rstrip("/")
    model = (os.getenv("LLM_MODEL") or defaults["model"]).strip()
    return LLMConfig(api_key=api_key, base_url=base_url, model=model, provider=provider)


class LLMPointClickSolver:
    """Ask a vision LLM where to click on a point-click captcha background."""

    # NOTE: the prompt deliberately calls out a specific kind of captcha
    # (95598 / Tencent waterproof wall AI-generated 3D scene with mixed
    # digit + icon targets) because glm-4v-flash with a generic "find these"
    # prompt produces obvious hallucinations — equal-spaced sequences,
    # perfect diagonals, and coordinates well outside the image bounds
    # (observed y=580 for a 236-px-tall background). Being specific about
    # what's in the image and what the targets look like meaningfully
    # increases pickup. The image-dimension bounds are appended dynamically
    # in `_call()` so the LLM knows the valid coordinate range, and
    # `solve()` discards entire responses with any out-of-bounds point.
    DEFAULT_PROMPT = (
        "你是 95598 国家电网登录页 Tencent 防水墙点选验证码识别助手。\n"
        "下面会给你两张图：\n"
        "- 第一张是「目标」：顶部指示要按顺序点击的元素序列。元素可能是阿拉伯数字、"
        "中文字、英文字母,或小图标（房子/锁/钥匙/定位针/雨伞/植物等极简像素风线稿图标）。\n"
        "- 第二张是「背景」：AI 生成的 3D 场景渲染（球、锥、立方体、几何形状）,"
        "上面散落着候选元素（带描边的数字/字符/图标）需要被找到。\n"
        "\n"
        "任务：按目标序列从左到右的顺序,给出背景图中**每一个**目标元素的中心点"
        "像素坐标。坐标系原点 (0,0) 是背景图左上角,x 向右,y 向下。\n"
        "\n"
        "规则:\n"
        "1. 坐标必须严格在背景图尺寸范围内（具体尺寸见下方说明）。超出范围的"
        "坐标会被丢弃,整体被视为失败。\n"
        "2. 必须返回**恰好**和目标序列等量的点位；缺一个或多一个都视为失败。\n"
        "3. 如果某个目标在背景图中找不到（或无法可靠定位）,整体返回空数组：\n"
        "   {\"points\": []}\n"
        "   不要用占位/猜测坐标填充。\n"
        "4. 只输出 JSON,无任何解释/markdown 围栏/前后文字。格式必须严格如下：\n"
        "   {\"points\": [{\"x\": int, \"y\": int}, ...]}\n"
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
        dims_hint = (
            f"\n背景图实际尺寸：宽 {bg_image.width} 像素 × 高 {bg_image.height} 像素。\n"
            f"所有 x 必须在 [0, {bg_image.width - 1}] 内,所有 y 必须在 [0, {bg_image.height - 1}] 内。\n"
            f"目标图尺寸：宽 {answer_image.width} 像素 × 高 {answer_image.height} 像素（仅供识别目标元素,不要返回这张图上的坐标）。\n"
        )
        full_prompt = self._prompt + dims_hint
        if self._config.provider == "anthropic":
            endpoint, payload, headers = self._build_anthropic_request(
                full_prompt, answer_b64, bg_b64
            )
        else:
            endpoint, payload, headers = self._build_openai_request(
                full_prompt, answer_b64, bg_b64
            )
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self._config.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _build_openai_request(self, prompt: str, answer_b64: str, bg_b64: str):
        endpoint = f"{self._config.base_url}/chat/completions"
        payload = {
            "model": self._config.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "text", "text": "目标图（要按顺序点击的元素序列）："},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{answer_b64}"},
                        },
                        {"type": "text", "text": "背景图（在此图中定位每一个目标元素的中心点）："},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{bg_b64}"},
                        },
                    ],
                }
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        return endpoint, payload, headers

    def _build_anthropic_request(self, prompt: str, answer_b64: str, bg_b64: str):
        endpoint = f"{self._config.base_url}/messages"
        payload = {
            "model": self._config.model,
            # Anthropic requires max_tokens. Captcha answers are tiny JSON
            # — cap generously at 512 so we never truncate.
            "max_tokens": 512,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "text", "text": "目标图（要按顺序点击的元素序列）："},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": answer_b64,
                            },
                        },
                        {"type": "text", "text": "背景图（在此图中定位每一个目标元素的中心点）："},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": bg_b64,
                            },
                        },
                    ],
                }
            ],
        }
        headers = {
            "x-api-key": self._config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        return endpoint, payload, headers

    def _extract_text(self, api_response: dict) -> str:
        # Anthropic returns `content` as a list of typed blocks; OpenAI / Zhipu
        # return `choices[0].message.content` as a string.
        if self._config.provider == "anthropic":
            try:
                for block in api_response.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return block.get("text") or ""
            except Exception:
                pass
            return ""
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
            return []
        # Reject responses with any out-of-bounds point. glm-4v-flash routinely
        # hallucinates coordinates outside the image (e.g. y=580 for a 236-px
        # background, or perfectly equal-spaced sequences) — treating those as
        # real clicks just wastes captcha refresh budget. A single bad point
        # invalidates the whole response since point order matters.
        bg_w, bg_h = bg_image.width, bg_image.height
        bad = [(x, y) for (x, y) in points if not (0 <= x < bg_w and 0 <= y < bg_h)]
        if bad:
            logging.info(
                "LLM solver rejected: %s of %s point(s) out of bg bounds %sx%s: %s. raw_text=%r",
                len(bad),
                len(points),
                bg_w,
                bg_h,
                bad,
                text[:200],
            )
            return []
        logging.info(
            "LLM solver predicted %s point(s) in %sx%s bg: %s",
            len(points),
            bg_w,
            bg_h,
            points,
        )
        return points
