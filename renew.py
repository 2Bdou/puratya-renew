#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MWS (cloud.m-ws.cc) 自动续期脚本

原理：MWS 的 Bot/Site 有 7 天倒计时，到期自动停止。
     点一次 Renew 按钮 = POST /api/bots/{id}/renew，把倒计时重置回 7 天。
     本脚本每周一、三、五跑一次，把所有 Bot/Site 全部续期，永不停止。

登录态：__Host-mrtcloud_token（JWT，约 26 天有效）。
       过期或剩余不足 MWS_REFRESH_BEFORE_DAYS（默认 7 天）时，
       若配置了 DISCORD_TOKEN，则走 Discord OAuth 换新 JWT（和 bothosting 同一套路），
       再用 GH_TOKEN 写回 GitHub Secret MWS_TOKEN。
       也接受 Authorization: Bearer <token>。

通知：走 notify-gateway（notify.py 上报结构化结果，网关统一发邮件 + Telegram）。
     仓库只需配 NOTIFY_URL / NOTIFY_TOKEN，不内置 SMTP / TG。
依赖：requests（pip install requests）
"""

import os
import sys
import json
import urllib.error
from datetime import datetime

import requests

from auth import (
    discord_relogin,
    discord_token,
    jwt_seconds_left,
    persist_mws_token,
)
from notify import notify

# 面板：https://cloud.m-ws.cc ；后端也可以直连 https://cloud-api.m-ws.cc（无 /api 前缀）
FRONTEND = "https://cloud.m-ws.cc"
API = os.environ.get("MWS_API", FRONTEND + "/api").rstrip("/")

# Cloudflare 对 Python 默认 UA 返回 403 (error 1010)，需伪装成浏览器
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

COOKIE_NAME = "__Host-mrtcloud_token"


def http(path, method="GET", token=None):
    """请求 API，返回 (status_code, body)。网络异常返回 (0, 错误信息)。"""
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Origin": FRONTEND,
        "Referer": FRONTEND + "/",
    }
    if token:
        headers["Cookie"] = COOKIE_NAME + "=" + token
        # 新后端同时认 Cookie 和 Bearer，双通道更稳
        headers["Authorization"] = "Bearer " + token
    if method != "GET":
        headers["Content-Type"] = "application/json"
    try:
        resp = requests.request(method, API + path, headers=headers, timeout=30)
        return resp.status_code, resp.text
    except requests.RequestException as e:
        return 0, str(e)


def now_str():
    """本地时间（workflow 里设 TZ=Asia/Shanghai 则显示北京时间）。"""
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")


def collect(token):
    """拉取所有 Bot / Site，返回 [(kind, id, name, remaining_hours), ...]。"""
    items = []
    for kind, path, key in [("Bot", "/bots", "bots"), ("Site", "/sites", "sites")]:
        status, body = http(path, token=token)
        if status != 200:
            print("[!] 获取 {} 失败: HTTP {} {}".format(path, status, body))
            items.append((kind, None, "<列表获取失败 HTTP {}>".format(status), None))
            continue
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = []
        lst = data if isinstance(data, list) else data.get(key, [])
        for obj in lst or []:
            oid = obj.get("id")
            name = obj.get("name") or obj.get("username") or "id:{}".format(oid)
            timer = obj.get("timer") or {}
            rem = timer.get("remaining_hours")
            items.append((kind, oid, name, rem))
    return items


def renew_one(token, kind, oid):
    """续期单个对象，返回 (ok, status, body)。"""
    path = "/bots/{}/renew".format(oid) if kind == "Bot" else "/sites/{}/renew".format(oid)
    status, body = http(path, method="POST", token=token)
    return (status == 200), status, body


def _report(level, title, content, details):
    """上报 notify-gateway；失败只打日志，不阻断续期主流程。"""
    try:
        notify(title, content, level=level, details=details)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print("::warning::通知上报失败 HTTP {}: {}".format(e.code, body))
    except Exception as e:
        print("::warning::通知上报失败: {}".format(e))


def _manual_token_help():
    return (
        "请重新登录 {}，F12 抓取 {}，更新到 GitHub Secret MWS_TOKEN。"
        "若要自动换票，再配 DISCORD_TOKEN + GH_TOKEN（与 bothosting 相同）。"
    ).format(FRONTEND, COOKIE_NAME)


def ensure_token():
    """返回可用的 MWS JWT；必要时用 Discord OAuth 换新并写回 Secrets。"""
    token = os.environ.get("MWS_TOKEN", "").strip()
    dc = discord_token()
    refresh_days = int(os.environ.get("MWS_REFRESH_BEFORE_DAYS", "7") or "7")
    need_refresh = False
    reason = ""
    me_body = ""

    if not token:
        need_refresh = True
        reason = "未设置 MWS_TOKEN"
    else:
        status, me_body = http("/auth/me", token=token)
        if status == 401:
            need_refresh = True
            reason = "MWS_TOKEN 已失效（HTTP 401）"
        elif status != 200:
            print("[✗] 验证 token 异常: HTTP {} {}".format(status, me_body))
            sys.exit(1)
        else:
            left = jwt_seconds_left(token)
            if left is not None and left < refresh_days * 86400:
                need_refresh = True
                reason = "MWS_TOKEN 将在 {:.1f} 天后过期，提前换票".format(left / 86400.0)
            elif left is not None:
                print("[i] MWS_TOKEN 剩余约 {:.1f} 天".format(left / 86400.0))

    if not need_refresh:
        return token, me_body

    print("[!] {}".format(reason))
    if not dc:
        title = "⚠️ MWS token 需要更新 ({})".format(now_str())
        content = reason + "\n" + _manual_token_help()
        print(title)
        print(content)
        _report("failed", title, content, None)
        sys.exit(1)

    try:
        token = discord_relogin(dc)
    except Exception as e:
        title = "⚠️ MWS Discord 自动登录失败 ({})".format(now_str())
        content = "{}\n{}\n{}".format(reason, e, _manual_token_help())
        print(title)
        print(content)
        _report("failed", title, content, None)
        sys.exit(1)

    wrote = persist_mws_token(token)
    if not wrote:
        print("[!] 新 token 仅用于本次运行；下次仍可能 401，请检查 GH_TOKEN")

    status, me_body = http("/auth/me", token=token)
    if status != 200:
        title = "⚠️ 新 MWS token 验证失败 ({})".format(now_str())
        content = "Discord 登录后 /auth/me HTTP {} {}".format(status, me_body)
        print(title)
        print(content)
        _report("failed", title, content, None)
        sys.exit(1)

    extra = "已写回 GitHub Secret" if wrote else "未写回 Secret（缺 GH_TOKEN 或 gh 失败）"
    _report(
        "success",
        "MWS token 已自动更新 ({})".format(now_str()),
        "{}\n{}".format(reason, extra),
        None,
    )
    return token, me_body


def main():
    token, body = ensure_token()

    try:
        who = json.loads(body).get("username")
    except (json.JSONDecodeError, AttributeError):
        who = "?"
    print("[✓] 登录有效: {}".format(who))
    print("[i] API: {}".format(API))

    # 1) 拉取并续期
    items = collect(token)
    if not items:
        title = "MWS 续期报告 ({}) · 无对象".format(now_str())
        print(title)
        _report("success", title, "账号下没有 Bot / Site，跳过。", None)
        return

    lines = []
    failed = 0
    detail_items = []
    for kind, oid, name, rem in items:
        if oid is None:
            lines.append("  · {} {}：列表获取失败".format(kind, name))
            detail_items.append({"id": "unknown", "name": name, "status": "failed",
                                 "error": "列表获取失败"})
            failed += 1
            continue
        ok, status, body = renew_one(token, kind, oid)
        rem_txt = "，续期前剩 {}h".format(int(rem)) if rem is not None else ""
        if ok:
            lines.append("  · {} {}：续期成功{}".format(kind, name, rem_txt))
            msg = rem_txt.strip().lstrip("，") if rem is not None else ""
            detail_items.append({"id": str(oid), "name": name, "status": "success",
                                 "message": msg})
            print("[✓] {} {} (id:{}) 续期成功{}".format(kind, name, oid, rem_txt))
        else:
            lines.append("  · {} {}：续期失败 HTTP {} {}".format(kind, name, status, body.strip()))
            detail_items.append({"id": str(oid), "name": name, "status": "failed",
                                 "error": "HTTP {} {}".format(status, body.strip())})
            failed += 1
            print("[✗] {} {} (id:{}) 续期失败 HTTP {}".format(kind, name, oid, status))

    # 2) 汇总 + 通知
    total = len(items)
    success = total - failed
    level = "success" if failed == 0 else "partial"
    status_word = "完成" if failed == 0 else "部分失败"
    title = "MWS 续期报告 ({}) · {}".format(now_str(), status_word)
    content = "\n".join(lines)
    print("\n" + title + "\n" + content)
    _report(
        level,
        title,
        content,
        {"total": total, "success": success, "failed": failed, "details": detail_items},
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
