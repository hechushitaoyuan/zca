"""页面路由：登录、账号管理、设置。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import HTMLResponse

from .. import settings
from ..captcha import BrowserChallengeError, captcha_manager

router = APIRouter()

_TOKEN = "{{APP_VERSION}}"


def _html(name: str) -> HTMLResponse:
    path = settings.STATIC_DIR / "admin" / name
    if not path.exists():
        raise HTTPException(404, "页面不存在")
    body = path.read_text(encoding="utf-8").replace(_TOKEN, settings.APP_VERSION)
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


def _browser_captcha_html(challenge_id: str) -> HTMLResponse:
    try:
        challenge = captcha_manager.get_browser_challenge(challenge_id)
    except BrowserChallengeError as err:
        raise HTTPException(404, str(err)) from err
    if challenge.status != "pending":
        raise HTTPException(410, "该验证链接已完成、过期或被消费，请重新生成")
    path = settings.STATIC_DIR / "admin" / "browser-captcha.html"
    if not path.exists():
        raise HTTPException(404, "验证页面不存在")
    bootstrap = json.dumps(
        {
            "challengeId": challenge.id,
            "scene": challenge.scene,
            "region": challenge.region,
            "prefix": challenge.prefix,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    body = path.read_text(encoding="utf-8").replace("{{CAPTCHA_BOOTSTRAP}}", bootstrap)
    return HTMLResponse(
        body,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/", include_in_schema=False)
async def root():
    # 根路径直接渲染登录面板，方便公网 IP 与 Cloudflare Tunnel 域名访问。
    return _html("login.html")


@router.get("/admin", include_in_schema=False)
async def admin_root():
    return _html("login.html")


@router.get("/admin/login", include_in_schema=False)
async def admin_login():
    return _html("login.html")


@router.get("/admin/accounts", include_in_schema=False)
async def admin_accounts():
    return _html("accounts.html")


@router.get("/admin/settings", include_in_schema=False)
async def admin_settings():
    return _html("settings.html")


@router.get("/captcha/{challenge_id}", include_in_schema=False)
async def browser_captcha(challenge_id: str):
    """在用户自己的 Windows 浏览器中渲染官方阿里云人工验证。"""
    return _browser_captcha_html(challenge_id)


@router.get("/captcha/{challenge_id}/status", include_in_schema=False)
async def browser_captcha_status(challenge_id: str):
    try:
        return captcha_manager.get_browser_challenge(challenge_id).public_view()
    except BrowserChallengeError as err:
        raise HTTPException(404, str(err)) from err


@router.post("/captcha/{challenge_id}/complete", include_in_schema=False)
async def complete_browser_captcha(challenge_id: str, payload: dict = Body(...)):
    try:
        return captcha_manager.complete_browser_challenge(
            challenge_id, str(payload.get("verify_param") or "")
        )
    except BrowserChallengeError as err:
        raise HTTPException(400, str(err)) from err


@router.get("/health", include_in_schema=False)
async def health():
    """轻量健康检查：仅暴露存活与构建标识，绝不返回账号/配置/凭据。"""
    return {
        "status": "ok",
        "version": settings.ZCA_VERSION,
        "commit": settings.ZCA_COMMIT,
    }


@router.get("/meta", include_in_schema=False)
async def meta():
    # 保留 version 键供后台 header.js 消费；新增 commit 标识构建来源。
    return {
        "version": settings.ZCA_VERSION,
        "commit": settings.ZCA_COMMIT,
    }
