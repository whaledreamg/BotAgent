#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agnes AI 多模态模型客户端。

OpenAI 兼容接口, 支持:
  - 文本对话
  - 流式输出
  - 工具调用 (function calling)
  - 图像理解 (image_url: 公共 URL 或 base64 data URI)
  - Thinking 模式

用法:
    from agnes_client import AgnesClient
    client = AgnesClient()
    print(client.chat("你好"))
    print(client.chat_with_image("这张图里是什么?", "photo.jpg"))
    for chunk in client.stream("写首诗"):
        print(chunk, end="", flush=True)
"""

import base64
import json
import mimetypes
import os
import sys
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_BASE_URL = "https://api.agnes-ai.cn/v1"
DEFAULT_MODEL = "agnes-3.0-flash"

# 文档里列出的模型, 用于 --model 校验
KNOWN_MODELS = [
    "agnes-3.0-flash",
    "agnes-3.0-pro",
    "agnes-2.5-flash",
    "agnes-2.5-pro",
    "agnes-2.5-pro-beta",
    "agnes-2.0-flash",  # 已废弃
]

RETRY_STATUS = {429, 500, 502, 503, 504, 520, 522, 524}


def load_dotenv(path=None):
    """极简 .env 加载, 不引入额外依赖。"""
    path = Path(path or Path(__file__).resolve().parent / ".env")
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def image_to_data_uri(path):
    """把本地图片转成 base64 data URI。"""
    path = Path(path)
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


class AgnesError(RuntimeError):
    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class AgnesClient:
    """Agnes AI 多模态模型客户端。"""

    def __init__(self, api_key=None, base_url=None, model=None,
                 timeout=120, max_retries=3, verbose=True):
        load_dotenv()
        self.api_key = api_key or os.environ.get("AGNES_API_KEY", "")
        self.base_url = (base_url or os.environ.get("AGNES_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("AGNES_MODEL") or DEFAULT_MODEL
        self.timeout = timeout
        self.max_retries = max_retries
        self.verbose = verbose

        if not self.api_key:
            raise AgnesError(
                "缺少 API Key。请设置环境变量 AGNES_API_KEY, "
                "或在项目根目录的 .env 中写入 AGNES_API_KEY=..."
            )

        # 复用 Session, 保持 TCP/TLS 连接, 减少 SSL 握手失败概率
        self._session = requests.Session()
        # 显式挂载适配器, 允许对连接层错误做重试
        self._adapter = HTTPAdapter(
            max_retries=Retry(
                total=0,  # 重试逻辑由本类统一控制, 避免两层重试叠加
                connect=0, read=0, status=0,
                backoff_factor=0,
            ),
            pool_connections=4,
            pool_maxsize=8,
        )
        self._session.mount("https://", self._adapter)
        self._session.mount("http://", self._adapter)

    # ------------------------------------------------------------ 内部

    def _log(self, msg):
        if self.verbose:
            print(f"[agnes] {msg}")

    def _post(self, path, payload, stream=False):
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_err = None

        for attempt in range(self.max_retries):
            try:
                resp = self._session.post(
                    url, headers=headers, json=payload,
                    timeout=self.timeout, stream=stream,
                )
            except requests.RequestException as exc:
                # 网络层异常(SSL EOF / 连接重置 / 超时)同样是瞬时故障, 需要重试
                last_err = AgnesError(f"网络请求失败: {exc}")
                if attempt < self.max_retries - 1:
                    delay = 2 ** attempt
                    self._log(f"网络异常 ({type(exc).__name__}), {delay}s 后重试 "
                              f"({attempt + 1}/{self.max_retries})")
                    time.sleep(delay)
                    continue
            else:
                if resp.status_code == 200:
                    return resp
                body = resp.text[:2000]
                # 4xx(除 429)属于请求本身的问题, 不重试
                if resp.status_code in RETRY_STATUS and attempt < self.max_retries - 1:
                    delay = 2 ** attempt
                    self._log(f"HTTP {resp.status_code}, {delay}s 后重试 "
                              f"({attempt + 1}/{self.max_retries})")
                    time.sleep(delay)
                    last_err = AgnesError(f"HTTP {resp.status_code}: {body}",
                                          resp.status_code, body)
                    continue
                raise AgnesError(f"HTTP {resp.status_code}: {body}",
                                  resp.status_code, body)

        raise last_err or AgnesError("请求失败")

    # ------------------------------------------------------------ 公开 API

    def chat(self, user_content, system=None, tools=None, tool_choice=None,
             image=None, images=None, thinking=False, max_tokens=2048,
             temperature=0.7, model=None, history=None):
        """文本/多模态对话, 返回回复文本。

        user_content: 字符串, 或 content blocks 数组
        image:         单张图片路径/URL
        images:        多张图片路径/URL 列表
        thinking:      开启 Thinking 模式
        history:       历史消息列表, 形如 [{"role":..., "content":...}]
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        if history:
            messages.extend(history)

        content = self._build_content(user_content, image, images)
        messages.append({"role": "user", "content": content})

        payload = {
            "model": model or self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
        if thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": True}

        resp = self._post("/chat/completions", payload)
        data = resp.json()
        return data["choices"][0]["message"].get("content", "")

    def chat_full(self, user_content, **kwargs):
        """和 chat 一样, 但返回完整的 message 字典 (含 tool_calls)。"""
        kwargs.setdefault("max_tokens", 2048)
        payload = {
            "model": kwargs.pop("model", None) or self.model,
            "messages": self._build_messages(user_content, kwargs),
            "max_tokens": kwargs.pop("max_tokens"),
            "temperature": kwargs.pop("temperature", 0.7),
        }
        tools = kwargs.pop("tools", None)
        if tools:
            payload["tools"] = tools
        tool_choice = kwargs.pop("tool_choice", None)
        if tool_choice:
            payload["tool_choice"] = tool_choice
        if kwargs.pop("thinking", False):
            payload["chat_template_kwargs"] = {"enable_thinking": True}

        resp = self._post("/chat/completions", payload)
        return resp.json()["choices"][0]["message"]

    def stream(self, user_content, system=None, image=None, images=None,
               thinking=False, max_tokens=2048, temperature=0.7, model=None):
        """流式对话, 逐块 yield 文本增量。"""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({
            "role": "user",
            "content": self._build_content(user_content, image, images),
        })
        payload = {
            "model": model or self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": True}

        resp = self._post("/chat/completions", payload, stream=True)
        # 显式指定 UTF-8, 否则 requests 会按 ISO-8859-1 解码导致中文乱码
        resp.encoding = "utf-8"
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line or line == "[DONE]":
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            for choice in data.get("choices", []):
                delta = choice.get("delta", {})
                piece = delta.get("content")
                if piece:
                    yield piece

    def chat_with_image(self, prompt, image, system=None, **kwargs):
        """图像理解: image 可以是本地路径或公共 URL。"""
        return self.chat(prompt, system=system, image=image, **kwargs)

    def generate_image(self, prompt, size="1K", ratio=None, image=None,
                       model="agnes-image-2.5-flash", response_format="url"):
        """调用 Agnes 图像生成模型。"""
        payload = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "extra_body": {"response_format": response_format},
        }
        if ratio:
            payload["extra_body"]["ratio"] = ratio
        if image:
            imgs = image if isinstance(image, (list, tuple)) else [image]
            payload["extra_body"]["image"] = [
                i if str(i).startswith(("http://", "https://", "data:"))
                else image_to_data_uri(i)
                for i in imgs
            ]
        resp = self._post("/images/generations", payload)
        item = resp.json()["data"][0]
        return item.get("url") or item.get("b64_json")

    def list_models(self):
        resp = self._session.get(f"{self.base_url}/models",
                                 headers={"Authorization": f"Bearer {self.api_key}"},
                                 timeout=self.timeout)
        resp.raise_for_status()
        return [m.get("id") for m in resp.json().get("data", [])]

    # ------------------------------------------------------------ 辅助

    def _build_messages(self, user_content, kwargs):
        messages = []
        system = kwargs.get("system")
        if system:
            messages.append({"role": "system", "content": system})
        history = kwargs.get("history")
        if history:
            messages.extend(history)
        messages.append({
            "role": "user",
            "content": self._build_content(user_content,
                                           kwargs.get("image"),
                                           kwargs.get("images")),
        })
        return messages

    @staticmethod
    def _build_content(user_content, image=None, images=None):
        """构造 content: 纯文本时返回 str, 含图时返回 blocks 数组。"""
        urls = []
        for it in (images or []):
            urls.append(it if str(it).startswith(("http://", "https://", "data:"))
                       else image_to_data_uri(it))
        if image:
            urls.append(image if str(image).startswith(("http://", "https://", "data:"))
                        else image_to_data_uri(image))

        if not urls:
            return user_content

        if isinstance(user_content, str):
            blocks = [{"type": "text", "text": user_content}]
        else:
            blocks = list(user_content)
        for u in urls:
            blocks.append({"type": "image_url", "image_url": {"url": u}})
        return blocks


# ------------------------------------------------------------------ CLI

def main():
    import argparse

    p = argparse.ArgumentParser(description="Agnes AI 多模态客户端测试")
    p.add_argument("--model", default=None, help="模型名")
    p.add_argument("--image", default=None, help="本地图片路径")
    p.add_argument("--stream", action="store_true", help="流式输出")
    p.add_argument("--thinking", action="store_true", help="开启 Thinking")
    p.add_argument("--tools", action="store_true", help="演示工具调用")
    p.add_argument("--list-models", action="store_true", help="列出可用模型")
    p.add_argument("prompt", nargs="*", default=["用一句话介绍你自己"])
    args = p.parse_args()

    client = AgnesClient(model=args.model)

    if args.list_models:
        for m in client.list_models():
            print(m)
        return

    prompt = " ".join(args.prompt) or "用一句话介绍你自己"

    if args.tools:
        tools = [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "获取指定城市的当前天气",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "城市名"},
                    },
                    "required": ["city"],
                },
            },
        }]
        msg = client.chat_full(prompt, tools=tools)
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                print(f"[工具调用] {tc['function']['name']} "
                      f"参数={tc['function']['arguments']}")
        else:
            print(msg.get("content", ""))
        return

    if args.stream:
        for piece in client.stream(prompt, image=args.image, thinking=args.thinking):
            print(piece, end="", flush=True)
        print()
        return

    print(client.chat(prompt, image=args.image, thinking=args.thinking))


if __name__ == "__main__":
    try:
        main()
    except AgnesError as exc:
        print(f"\n[错误] {exc}", file=sys.stderr)
        print("[提示] 若是网络/SSL 异常, 稍后重试即可; "
              "也可检查代理设置或用 --model 换一个模型。", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
