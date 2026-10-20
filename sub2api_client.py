#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sub2api 用量查询客户端（纯标准库，无 tkinter 依赖，可独立复用 / 测试）。

针对「API Key 持有者」场景，走网关接口（Authorization: Bearer <sk-...>）：
  - GET /v1/usage              当前 key 的用量 / 订阅额度 / 分模型 / 每日明细
  - GET /v1/models             可用模型列表（可选）

返回结构随 key 的计费模式变化：
  - 订阅模式（subscription）: mode=unrestricted，含 subscription.{daily/weekly/monthly}_* 额度
  - 额度模式（quota）:        mode=quota_limited，含 quota.{limit,used,remaining} 与 rate_limits[]
  - 余额模式（balance）:      含 balance / remaining 等
"""

import json
import urllib.request
import urllib.error


class ApiError(Exception):
    """网关返回的业务 / HTTP 错误。"""

    def __init__(self, status, message, reason=""):
        super().__init__(f"[{status}] {message}" + (f" ({reason})" if reason else ""))
        self.status = status
        self.message = message
        self.reason = reason


class Sub2APIClient:
    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    # ---- 底层请求 ----
    def _request(self, method, path):
        url = self.base_url + path
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", "Bearer " + self.api_key)
        req.add_header("Accept", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            raise ApiError(e.code, *_parse_error(raw, e.reason))
        except urllib.error.URLError as e:
            raise ApiError(0, f"无法连接服务器: {e.reason}")

        if not raw:
            return None
        return json.loads(raw)

    # ---- 网关接口 ----
    def usage(self):
        """GET /v1/usage，返回该 key 的用量与额度。"""
        return self._request("GET", "/v1/usage")

    def models(self):
        """GET /v1/models，返回可用模型列表。"""
        data = self._request("GET", "/v1/models")
        return (data or {}).get("data") or []

    def fetch_snapshot(self):
        return {"usage": self.usage() or {}}


def _parse_error(raw, fallback):
    """兼容多种错误格式：OpenAI 风格 / 统一响应包 / 纯文本。"""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return (raw or fallback), ""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return err.get("message", ""), err.get("type", "")
        if payload.get("code") or payload.get("message"):
            return payload.get("message", ""), payload.get("reason", "")
    return str(payload), ""
