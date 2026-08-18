"""运行期配置：环境变量 + 默认值。

所有可调参数集中在此。账号与凭证不在此处，而是持久化到 data/ 目录（见 store.py）。
"""

from __future__ import annotations

import os
import platform as runtime_platform
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# 项目根目录
ROOT_DIR = Path(__file__).resolve().parents[1]


def _resolve_path(env_name: str, default: str) -> Path:
    raw = (os.getenv(env_name, default) or default).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path


def _int(env_name: str, default: int) -> int:
    try:
        return int(os.getenv(env_name, str(default)))
    except (TypeError, ValueError):
        return default


# ── 目录 ─────────────────────────────────────────────────────────────────────
DATA_DIR = _resolve_path("ZCODE_DATA_DIR", "data")
# 账号与设置持久化到本地 SQLite（与 grok2api 的 local 后端一致）
DB_PATH = DATA_DIR / "accounts.db"
STATIC_DIR = Path(__file__).resolve().parent / "statics"

# ── 服务 ─────────────────────────────────────────────────────────────────────
PORT = _int("ZCODE_PORT", 3000)
HOST = os.getenv("ZCODE_HOST", "0.0.0.0")

# ── 鉴权 ─────────────────────────────────────────────────────────────────────
# 后台管理密码默认值，首次启动写入 data/accounts.db，之后以数据库（meta 表）为准。
DEFAULT_ADMIN_KEY = os.getenv("ZCODE_ADMIN_KEY", "zcode")

# 网关 API Key（调用 /v1/messages 等的鉴权）。默认空（仅回环安全使用）。
# 仅用于「新库初始化」与「既有库当前为空时补齐」；绝不覆盖用户已在后台设置的非空值。
# ⚠️ 公网监听（ZCA_BIND_IP=0.0.0.0）前必须设为强随机非空值。
GATEWAY_KEY = (os.getenv("ZCODE_GATEWAY_KEY", "") or "").strip()

# ── 验证码配置缓存 ───────────────────────────────────────────────────────────
CAPTCHA_CONFIG_CACHE_TTL = _int("CAPTCHA_CONFIG_CACHE_TTL", 600_000)  # ms

# 验证码求解（默认 Node + 真实 Chromium；可显式切回旧 jsdom 引擎）
NODE_PATH = os.getenv("ZCODE_NODE_PATH", "node")
CAPTCHA_SOLVER_DIR = ROOT_DIR / "captcha_node"
CAPTCHA_ENGINE = (os.getenv("ZCODE_CAPTCHA_ENGINE", "browser") or "browser").strip().lower()
CAPTCHA_SOLVER_JS = CAPTCHA_SOLVER_DIR / (
    "browser_solver.js" if CAPTCHA_ENGINE == "browser" else "solver.js"
)
CAPTCHA_SOLVE_RETRIES = _int("ZCODE_CAPTCHA_RETRIES", 4)
CAPTCHA_SOLVE_TIMEOUT = _int("ZCODE_CAPTCHA_TIMEOUT", 150)  # 含人工验证等待时间（秒）
CHROMIUM_PATH = os.getenv("ZCODE_CHROMIUM_PATH", "/usr/bin/chromium")
CHROMIUM_PROFILE_DIR = _resolve_path(
    "ZCODE_CHROMIUM_PROFILE_DIR", str(DATA_DIR / "chromium-profile")
)
CAPTCHA_BROWSER_HEADLESS = os.getenv("ZCODE_CAPTCHA_BROWSER_HEADLESS", "0") == "1"
CAPTCHA_BROWSER_TIMEOUT = _int("ZCODE_CAPTCHA_BROWSER_TIMEOUT", 120_000)

# Z.ai OAuth 3.7.7：官方网页授权在独立 Xvfb/noVNC 桌面运行，避免与验证码窗口争用。
OAUTH_BROWSER_JS = CAPTCHA_SOLVER_DIR / "oauth_login.js"
OAUTH_DISPLAY = os.getenv("ZCODE_OAUTH_DISPLAY", ":100")
OAUTH_BROWSER_TIMEOUT = _int("ZCODE_OAUTH_BROWSER_TIMEOUT", 600_000)  # ms
OAUTH_FLOW_TIMEOUT = _int("ZCODE_OAUTH_TIMEOUT", 630)  # seconds
OAUTH_AUTHORIZE_URL = os.getenv(
    "ZCODE_OAUTH_AUTHORIZE_URL", "https://chat.z.ai/api/oauth/authorize"
)
OAUTH_TOKEN_URL = os.getenv(
    "ZCODE_OAUTH_TOKEN_URL", "https://zcode.z.ai/api/v1/oauth/token"
)
OAUTH_USERINFO_URL = os.getenv(
    "ZCODE_OAUTH_USERINFO_URL", "https://chat.z.ai/api/oauth/userinfo"
)
OAUTH_BRIDGE_URL = os.getenv(
    "ZCODE_OAUTH_BRIDGE_URL", "https://zcode.z.ai/app/oauth/login"
)
OAUTH_CLIENT_ID = os.getenv(
    "ZCODE_OAUTH_CLIENT_ID", "client_P8X5CMWmlaRO9gyO-KSqtg"
)
_novnc_host = (os.getenv("ZCA_NOVNC_BIND_IP", "127.0.0.1") or "127.0.0.1").strip()
if _novnc_host in ("", "0.0.0.0", "::"):
    _novnc_host = "127.0.0.1"
_oauth_novnc_port = _int("ZCA_OAUTH_NOVNC_PORT", 6081)
OAUTH_NOVNC_URL = (
    os.getenv("ZCODE_OAUTH_NOVNC_URL", "").strip()
    or f"http://{_novnc_host}:{_oauth_novnc_port}/vnc.html?autoconnect=1&resize=remote"
)

# ── ZCode client identity ────────────────────────────────────────────────────
def _runtime_arch() -> str:
    raw = runtime_platform.machine().lower()
    return {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(raw, raw)


def _runtime_platform() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "win32"
    return "linux"


ZCODE_CLIENT_VERSION = os.getenv("ZCODE_CLIENT_VERSION", "3.7.7")
ZCODE_SOURCE_TITLE = os.getenv("ZCODE_SOURCE_TITLE", "electron")
ZCODE_REFERER = os.getenv("ZCODE_REFERER", "https://zcode.z.ai")
ZCODE_IDENTITY_PLATFORM = os.getenv("ZCODE_IDENTITY_PLATFORM", _runtime_platform())
ZCODE_IDENTITY_ARCH = os.getenv("ZCODE_IDENTITY_ARCH", _runtime_arch())
ZCODE_IDENTITY_RELEASE = os.getenv("ZCODE_IDENTITY_RELEASE", runtime_platform.release())
ZCODE_RELEASE_CHANNEL = (os.getenv("ZCODE_IDENTITY_RELEASE_CHANNEL", "") or "").strip()
ZCODE_CLIENT_LANGUAGE = os.getenv("ZCODE_IDENTITY_CLIENT_LANGUAGE", "zh-CN")
ZCODE_CLIENT_TIMEZONE = os.getenv("ZCODE_IDENTITY_CLIENT_TIMEZONE", "Asia/Shanghai")
ZCODE_DEVICE_MID = (os.getenv("ZCODE_IDENTITY_DEVICE_MID", "") or "").strip()
ZCODE_PLATFORM_ID = f"{ZCODE_IDENTITY_PLATFORM}-{ZCODE_IDENTITY_ARCH}"

# ── 用量监控 ─────────────────────────────────────────────────────────────────
# 后台自动刷新账号额度的间隔（秒）。0 表示关闭后台轮询，仅按需刷新。
QUOTA_REFRESH_INTERVAL = _int("ZCODE_QUOTA_REFRESH_INTERVAL", 60)
# 限流（cooling）冷却时长（秒）
COOLING_SECONDS = _int("ZCODE_COOLING_SECONDS", 300)
RISK_3012_COOLDOWN_BASE = _int("ZCODE_3012_COOLDOWN_BASE", 300)
RISK_3012_COOLDOWN_MAX = _int("ZCODE_3012_COOLDOWN_MAX", 7200)
ACCOUNT_CONCURRENCY_LIMIT = max(1, _int("ZCODE_ACCOUNT_CONCURRENCY_LIMIT", 1))

# ── 上游端点 ─────────────────────────────────────────────────────────────────
UPSTREAM = {
    "zai": os.getenv(
        "ZAI_UPSTREAM_URL",
        "https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages",
    ),
    "zai_fallback": os.getenv(
        "ZAI_FALLBACK_URL",
        "https://api.z.ai/api/anthropic/v1/messages",
    ),
    "bigmodel": os.getenv(
        "BIGMODEL_UPSTREAM_URL",
        "https://open.bigmodel.cn/api/anthropic/v1/messages",
    ),
}

# ZCode 计费 / 额度查询端点
ZCODE_BILLING_BASE = "https://zcode.z.ai/api/v1/zcode-plan"

USER_AGENT = os.getenv("UPSTREAM_USER_AGENT", f"ZCode/{ZCODE_CLIENT_VERSION}")
APP_VERSION = "2.0.0"

# ── 构建标识（由镜像 build args 注入，运行时只读）──────────────────────────────
# 镜像构建时通过 --build-arg ZCA_VERSION=... ZCA_COMMIT=... 注入并写入环境变量；
# 本地直接运行时回退到默认值，便于 /health 与 /meta 区分构建来源。
ZCA_VERSION = (os.getenv("ZCA_VERSION", "") or APP_VERSION).strip()
ZCA_COMMIT = (os.getenv("ZCA_COMMIT", "") or "unknown").strip()
