import json
import logging
import socket
from typing import Any, Dict, Optional
from urllib import error, request

logger = logging.getLogger(__name__)

# 固定随机种子：同一提示词必须产出同一份 JSON，避免"同一句话一次被接受一次被拒绝"。
DEFAULT_SEED = 20240617
# 网络抖动时的重试次数（不含首次尝试）。
_MAX_ATTEMPTS = 2
# 单次请求超时。Ollama 首次加载模型时可能较慢，因此放宽到 60s，且超时后不再重试
# （否则一次卡死会把调度线程拖住两倍时间）。
DEFAULT_TIMEOUT = 60.0


class OllamaClient:
    """与本地 Ollama 服务交互的简单封装。"""

    def __init__(self, base_url: str = "http://localhost:11434", model: str = "qwen3:8b"):
        self.base_url = base_url.rstrip("/")
        self.model = model

    def call(
        self,
        prompt: str,
        *,
        json_mode: bool = True,
        options: Optional[Dict[str, Any]] = None,
        schema: Optional[Dict[str, Any]] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_attempts: int = _MAX_ATTEMPTS,
    ) -> Dict[str, Any]:
        """调用 Ollama /api/generate，并尝试把返回解析为 JSON。

        ``schema`` 传入 JSON Schema 时会交给 Ollama 的 ``format`` 字段做**结构化输出约束**，
        模型必须按 schema 补齐 required 键，从根上消除"缺 avoidance/task_order 就判失败"
        这类由自由生成导致的伪失败；若服务端不接受 schema（旧版本），自动降级为 ``"json"``。

        说明：此处不使用任何额外依赖，避免 Windows 环境缺少 requests。
        """
        # format 的候选顺序：先约束最强的 JSON Schema，再退回宽松的 JSON 模式。
        formats = ["json"]
        if json_mode and schema:
            formats.insert(0, schema)
        elif not json_mode:
            formats = [None]

        last_exc: Optional[Exception] = None
        for attempt in range(max(1, max_attempts)):
            retryable = True
            for fmt in formats:
                try:
                    return self._request(prompt, fmt, options, timeout)
                except _FormatUnsupported:
                    # schema 不被支持 → 换下一个更宽松的 format。
                    continue
                except _Timeout as exc:
                    # 超时通常意味着服务端在读模型/被占满，立刻重试只会再等一个超时。
                    last_exc, retryable = exc, False
                    break
                except RuntimeError as exc:
                    last_exc = exc
                    break
            if last_exc is not None:
                logger.warning("Ollama 调用失败（第 %d 次尝试）：%s", attempt + 1, last_exc)
            if not retryable:
                break
        raise last_exc or RuntimeError("Ollama 调用失败: 未知错误")

    def _request(
        self,
        prompt: str,
        fmt: Any,
        options: Optional[Dict[str, Any]],
        timeout: float,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_ctx": 8192, "seed": DEFAULT_SEED, **(options or {})},
        }
        if fmt is not None:
            payload["format"] = fmt

        req = request.Request(
            f"{self.base_url}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=timeout) as response:
                content = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            # 400 且请求带 schema：判定为服务端不支持 JSON Schema，交由上层降级。
            if exc.code == 400 and not isinstance(fmt, str):
                raise _FormatUnsupported(detail) from exc
            raise RuntimeError(f"Ollama 调用失败: HTTP {exc.code} {detail}") from exc
        except socket.timeout as exc:
            raise _Timeout(f"Ollama 调用超时（{timeout:.0f}s）") from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Ollama 调用失败: {exc}") from exc

        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Ollama 返回不是合法 JSON: {content}") from exc

        raw_text = (data.get("response") or "").strip()
        if not raw_text:
            return {"status": "error", "message": "empty_response"}

        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`\n ")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].lstrip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {"raw_response": raw_text, "status": "raw"}


class _FormatUnsupported(RuntimeError):
    """服务端不接受本次 ``format``（通常是旧版本 Ollama 不支持 JSON Schema）。"""


class _Timeout(RuntimeError):
    """请求超时：重试没有意义，直接交给上层走本地兜底。"""
