#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MWS 登录态：Discord OAuth 换新 JWT，并写回 GitHub Secret MWS_TOKEN。

和 bothosting 同一条链路：MWS 没有 refresh 接口，SESSION/JWT 过期后
用浏览器里抓的 Discord 用户 Token 调 Discord OAuth2 authorize，拿到
callback code，再换 __Host-mrtcloud_token。
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import urllib.parse

import requests

FRONTEND = "https://cloud.m-ws.cc"
AUTH_HOST = "https://cloud-api.m-ws.cc"
COOKIE_NAME = "__Host-mrtcloud_token"
OAUTH_STATE_COOKIE = "__Host-mrtcloud_oauth_state"

DISCORD_CLIENT_ID = "1508034084377464903"
OAUTH_REDIRECT_URI = AUTH_HOST + "/auth/callback"
OAUTH_SCOPE = "identify"
DISCORD_AUTHORIZE = "https://discord.com/api/v9/oauth2/authorize"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

TOKEN_SET_COOKIE_RE = re.compile(
    r"(?:^|[,\n]\s*)" + re.escape(COOKIE_NAME) + r"=([^;\s]+)"
)


def discord_token() -> str:
    """DISCORD_TOKEN；支持 bothosting 那种 `备注,token` 写法。"""
    raw = (os.environ.get("DISCORD_TOKEN") or "").strip()
    if not raw:
        return ""
    if "," in raw:
        raw = raw.split(",", 1)[-1].strip()
    return raw


def jwt_seconds_left(token: str):
    """读 JWT exp，不校验签名。解析失败返回 None。"""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        exp = data.get("exp")
        if exp is None:
            return None
        return int(exp) - int(time.time())
    except Exception:
        return None


def extract_mws_token(cookie_header: str) -> str:
    """从 Set-Cookie 头里抠 __Host-mrtcloud_token。"""
    if not cookie_header:
        return ""
    m = TOKEN_SET_COOKIE_RE.search(cookie_header)
    return urllib.parse.unquote(m.group(1)) if m else ""


def _token_from_cookies(jar) -> str:
    for c in jar:
        if c.name == COOKIE_NAME:
            return c.value or ""
    return ""


def _new_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/json,*/*",
    })
    return sess


def _start_oauth(sess: requests.Session):
    """GET /auth/login，拿到 state 和 oauth_state cookie。"""
    resp = sess.get(AUTH_HOST + "/auth/login", allow_redirects=False, timeout=30)
    loc = resp.headers.get("Location") or ""
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    state = (qs.get("state") or [""])[0]
    if not state:
        raise RuntimeError("MWS /auth/login 没有返回 OAuth state（HTTP {}）".format(resp.status_code))
    cookie = sess.cookies.get(OAUTH_STATE_COOKIE) or ""
    if not cookie:
        # 有的环境 jar 不收 __Host- 前缀，手工补
        raw = resp.headers.get("Set-Cookie") or ""
        m = re.search(re.escape(OAUTH_STATE_COOKIE) + r"=([^;]+)", raw)
        if m:
            cookie = urllib.parse.unquote(m.group(1))
            sess.cookies.set(OAUTH_STATE_COOKIE, cookie, domain="cloud-api.m-ws.cc", path="/")
    return state, loc


def _discord_authorize(dc_token: str, state: str) -> str:
    """用 Discord 用户 Token 授权，返回 MWS callback URL（带 code）。"""
    query = urllib.parse.urlencode({
        "client_id": DISCORD_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": OAUTH_SCOPE,
        "state": state,
    })
    referer = "https://discord.com/oauth2/authorize?" + urllib.parse.urlencode({
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": OAUTH_SCOPE,
        "state": state,
    })
    headers = {
        "accept": "*/*",
        "authorization": dc_token,
        "content-type": "application/json",
        "origin": "https://discord.com",
        "referer": referer,
        "user-agent": UA,
        "x-discord-locale": "zh-CN",
    }
    body = {
        "permissions": "0",
        "authorize": True,
        "integration_type": 0,
        "location_context": {
            "guild_id": "10000",
            "channel_id": "10000",
            "channel_type": 10000,
        },
    }
    resp = requests.post(
        DISCORD_AUTHORIZE + "?" + query,
        headers=headers,
        json=body,
        timeout=20,
    )
    if resp.status_code == 401:
        raise RuntimeError("Discord Token 已失效（HTTP 401），需要更新 DISCORD_TOKEN")
    if resp.status_code != 200:
        snippet = (resp.text or "")[:300]
        raise RuntimeError("Discord OAuth2 授权失败: HTTP {} {}".format(resp.status_code, snippet))
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError("Discord 授权响应不是 JSON")
    location = data.get("location") or ""
    if not location:
        raise RuntimeError("Discord 授权响应没有 location（未拿到回调 code）")
    return location


def _set_cookie_blob(resp) -> str:
    headers = resp.headers
    if hasattr(headers, "get_all"):
        vals = headers.get_all("Set-Cookie") or []
        if vals:
            return "\n".join(vals)
    if hasattr(headers, "getlist"):
        vals = headers.getlist("Set-Cookie") or []
        if vals:
            return "\n".join(vals)
    raw = headers.get("Set-Cookie") or ""
    if isinstance(raw, list):
        return "\n".join(raw)
    return raw


def _finish_callback(sess: requests.Session, location: str) -> str:
    """打开 Discord 返回的 callback，读取新的 MWS JWT。"""
    resp = sess.get(location, allow_redirects=False, timeout=30)
    token = _token_from_cookies(sess.cookies) or extract_mws_token(_set_cookie_blob(resp))
    if not token:
        # 再跟一跳（有时先 302 到前端）
        loc = resp.headers.get("Location") or ""
        if loc:
            nxt = urllib.parse.urljoin(location, loc)
            resp2 = sess.get(nxt, allow_redirects=False, timeout=30)
            token = _token_from_cookies(sess.cookies) or extract_mws_token(
                _set_cookie_blob(resp2)
            )
    if not token:
        loc = resp.headers.get("Location") or ""
        raise RuntimeError("OAuth 回调没有种下 {}（停留 {}）".format(COOKIE_NAME, loc or resp.status_code))
    return token


def discord_relogin(dc_token: str) -> str:
    """走完 Discord OAuth，返回新的 MWS JWT。失败抛 RuntimeError。"""
    sess = _new_session()
    state, _login_loc = _start_oauth(sess)
    print("[i] 已拿到 MWS OAuth state，向 Discord 申请授权")
    location = _discord_authorize(dc_token, state)
    masked = re.sub(r"code=[^&]+", "code=***", location)
    print("[i] 已拿到回调 URL:", masked)
    token = _finish_callback(sess, location)
    print("[✓] Discord OAuth 换到新的 MWS_TOKEN")
    return token


def persist_mws_token(token: str) -> bool:
    """用 GH_TOKEN（classic PAT，repo 权限）把 MWS_TOKEN 写回 GitHub Secrets。"""
    if not token:
        print("[!] 跳过写回：token 为空")
        return False
    gh_token = (os.environ.get("GH_TOKEN") or "").strip()
    if not gh_token:
        print("[!] 未设置 GH_TOKEN，无法写回 Secrets（本次进程内仍会用新 token 续期）")
        return False
    env = os.environ.copy()
    env["GH_TOKEN"] = gh_token
    try:
        proc = subprocess.run(
            ["gh", "secret", "set", "MWS_TOKEN", "--body", token],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        print("[!] 未安装 gh，无法写回 Secrets")
        return False
    except Exception as e:
        print("[!] 写回 Secrets 异常:", e)
        return False
    if proc.returncode == 0:
        print("[✓] MWS_TOKEN 已写回 GitHub Secrets")
        return True
    err = (proc.stderr or proc.stdout or "").strip()
    print("[!] 写回 Secrets 失败:", err)
    return False
