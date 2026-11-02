"""ZCode 验证码管理。

默认通过 Node + Playwright 在真实 Chromium 中运行阿里云官方 SDK，
求得 verifyParam（X-Aliyun-Captcha-Verify-Param）。旧 jsdom 引擎仅保留为回退选项。

- 独立消费：每个上游请求求得一个新的 verifyParam，绝不重复使用
- 并发：同一时刻只跑一个求解进程，避免多个 Chromium 争用同一 profile
- 重试：单次求解偶发失败时自动重试
- 受控子进程：可配置超时；超时后 kill 并回收，避免僵尸进程
- region：随配置接口返回，与求解结果一并交回网关，写入校验请求头
"""

from __future__ import annotations

import asyncio
import os
import secrets
import threading
import time
from dataclasses import dataclass

import httpx

from . import logs, settings

# verifyParam 合法性下限：真实阿里云无痕校验串远长于此，
# 用于把"空/截断/异常短"输出判为失败，不打印其内容。
MIN_VERIFY_PARAM_LEN = 32
# stderr/stdout 诊断在异常信息中的最大长度（脱敏后再截断）。
DIAG_MAX_LEN = 200
_DEFAULT_REGION = "sgp"
_DEFAULT_PREFIX = "no8xfe"
_DEFAULT_SCENE = "11xygtvd"


class SolverError(RuntimeError):
    """求解失败基类；message 已脱敏限长，可安全记录。"""


class SolverTimeout(SolverError):
    """子进程超时被终止。"""


class SolverExitError(SolverError):
    """子进程非零退出。"""


class SolverOutputError(SolverError):
    """输出缺少标记 / 格式错误 / verifyParam 过短。"""


class InteractiveCaptchaRequired(SolverError):
    """真实浏览器要求用户完成交互式验证。"""


class BrowserChallengeError(RuntimeError):
    """本地浏览器验证会话不存在、过期或状态不允许。"""


@dataclass
class BrowserChallenge:
    """一次性本地浏览器验证会话；verify_param 永不对外返回。"""

    id: str
    scene: str
    region: str
    prefix: str
    created_at: float
    expires_at: float
    status: str = "pending"
    ready_at: float | None = None
    result_expires_at: float | None = None
    ended_at: float | None = None
    verify_param: str | None = None

    def public_view(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        return {
            "id": self.id,
            "status": self.status,
            "expires_in": max(0, int(self.expires_at - now)),
            "result_expires_in": (
                max(0, int(self.result_expires_at - now))
                if self.result_expires_at is not None
                else None
            ),
        }


def _tail(raw: bytes | str | None, limit: int = DIAG_MAX_LEN) -> str:
    """把诊断输出压成单行并限长；绝不用于承载 verifyParam。"""
    if not raw:
        return ""
    text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
    text = " ".join(text.split())
    return text[-limit:]


class CaptchaManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._config_cache: dict | None = None
        self._config_cache_at: float = 0.0
        self._browser_lock = threading.RLock()
        self._browser_challenges: dict[str, BrowserChallenge] = {}

    # ── 配置 ─────────────────────────────────────────────────────────────────
    async def fetch_config(self) -> dict:
        now = time.time() * 1000
        if self._config_cache and now - self._config_cache_at < settings.CAPTCHA_CONFIG_CACHE_TTL:
            return self._config_cache
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.get(
                    "https://zcode.z.ai/api/v1/client/configs",
                    params={
                        "app_version": settings.ZCODE_CLIENT_VERSION,
                        "platform": settings.ZCODE_PLATFORM_ID,
                    },
                )
            res.raise_for_status()
            captcha = ((res.json().get("data") or {}).get("configs") or {}).get("captcha")
            if captcha:
                self._config_cache = captcha
                self._config_cache_at = now
                return captcha
        except (httpx.HTTPError, ValueError) as err:
            logs.warn("captcha", f"获取当前 ZCode 验证配置失败: {err}")
            raise SolverError("无法获取当前 ZCode 验证配置") from err
        raise SolverError("当前 ZCode 验证配置缺少 captcha 字段")

    # ── 求解 ─────────────────────────────────────────────────────────────────
    async def get_verify_param(self, port: int | None = None) -> tuple[str, str]:
        """返回本次请求专用的 (verifyParam, region)。

        阿里云 V3 验证参数只能消费一次；复用会触发 F018/HTTP 400。锁仅用于
        串行化求解器，因为所有 Chromium 实例共享同一个持久化 profile。
        """
        async with self._lock:
            browser_result = self._consume_browser_result()
            if browser_result is not None:
                logs.ok("captcha", "已消费 Windows 本地浏览器的一次性验证结果")
                return browser_result
            config = await self.fetch_config()
            region = config.get("region") or _DEFAULT_REGION
            if config.get("enabled") is False:
                return "", region
            param = await self._solve(config)
            return param, region

    # ── Windows 本地浏览器人工验证 ──────────────────────────────────────────
    async def create_browser_challenge(self) -> BrowserChallenge:
        """创建随机、短期、可公开打开的一次性验证链接会话。"""
        config = await self.fetch_config()
        if config.get("enabled") is False:
            raise BrowserChallengeError("当前 ZCode 配置未启用人机验证")
        now = time.time()
        challenge = BrowserChallenge(
            id=secrets.token_urlsafe(24),
            scene=str(config.get("sceneId") or _DEFAULT_SCENE),
            region=str(config.get("region") or _DEFAULT_REGION),
            prefix=str(config.get("prefix") or _DEFAULT_PREFIX),
            created_at=now,
            expires_at=now + settings.CAPTCHA_BROWSER_LINK_TTL,
        )
        with self._browser_lock:
            self._prune_browser_challenges(now)
            self._browser_challenges[challenge.id] = challenge
        return challenge

    def get_browser_challenge(self, challenge_id: str) -> BrowserChallenge:
        now = time.time()
        with self._browser_lock:
            self._prune_browser_challenges(now)
            challenge = self._browser_challenges.get(challenge_id)
            if challenge is None:
                raise BrowserChallengeError("验证链接不存在或已经失效")
            self._expire_browser_challenge(challenge, now)
            return challenge

    def complete_browser_challenge(self, challenge_id: str, verify_param: str) -> dict:
        """接收浏览器结果；只保存在内存，且不在响应或日志中回显。"""
        verify_param = (verify_param or "").strip()
        if not MIN_VERIFY_PARAM_LEN <= len(verify_param) <= 20_000:
            raise BrowserChallengeError("浏览器返回的验证结果格式无效")
        now = time.time()
        with self._browser_lock:
            challenge = self._browser_challenges.get(challenge_id)
            if challenge is None:
                raise BrowserChallengeError("验证链接不存在或已经失效")
            self._expire_browser_challenge(challenge, now)
            if challenge.status == "expired":
                raise BrowserChallengeError("验证链接已经过期，请重新生成")
            if challenge.status != "pending":
                raise BrowserChallengeError("该验证链接已经完成或被消费")
            challenge.status = "ready"
            challenge.ready_at = now
            challenge.result_expires_at = min(
                challenge.expires_at,
                now + settings.CAPTCHA_BROWSER_RESULT_TTL,
            )
            challenge.verify_param = verify_param
            return challenge.public_view(now)

    def cancel_browser_challenge(self, challenge_id: str) -> bool:
        with self._browser_lock:
            challenge = self._browser_challenges.get(challenge_id)
            if challenge is None:
                return False
            challenge.verify_param = None
            challenge.status = "cancelled"
            challenge.ended_at = time.time()
            return True

    def _consume_browser_result(self) -> tuple[str, str] | None:
        now = time.time()
        with self._browser_lock:
            self._prune_browser_challenges(now)
            ready = sorted(
                (
                    item
                    for item in self._browser_challenges.values()
                    if item.status == "ready" and item.verify_param
                ),
                key=lambda item: item.ready_at or item.created_at,
            )
            if not ready:
                return None
            challenge = ready[0]
            verify_param = challenge.verify_param
            challenge.verify_param = None
            challenge.status = "consumed"
            challenge.ended_at = now
            return verify_param, challenge.region

    def _expire_browser_challenge(self, challenge: BrowserChallenge, now: float) -> None:
        result_expired = (
            challenge.status == "ready"
            and challenge.result_expires_at is not None
            and now >= challenge.result_expires_at
        )
        if now >= challenge.expires_at or result_expired:
            challenge.verify_param = None
            if challenge.status != "expired":
                challenge.status = "expired"
                challenge.ended_at = now

    def _prune_browser_challenges(self, now: float) -> None:
        for challenge in self._browser_challenges.values():
            self._expire_browser_challenge(challenge, now)
        # 已结束的会话保留一分钟供前端看到最终状态，其后清除；同时限制数量。
        stale = [
            key
            for key, item in self._browser_challenges.items()
            if item.status in {"expired", "cancelled", "consumed"}
            and item.ended_at is not None
            and now - item.ended_at > 60
        ]
        for key in stale:
            self._browser_challenges.pop(key, None)
        if len(self._browser_challenges) > 32:
            ordered = sorted(
                self._browser_challenges.values(), key=lambda item: item.created_at
            )
            for item in ordered[: len(self._browser_challenges) - 32]:
                item.verify_param = None
                self._browser_challenges.pop(item.id, None)

    async def _solve(self, config: dict) -> str:
        scene = config.get("sceneId") or _DEFAULT_SCENE
        region = config.get("region") or _DEFAULT_REGION
        prefix = config.get("prefix") or _DEFAULT_PREFIX

        last_err: SolverError | None = None
        for attempt in range(1, settings.CAPTCHA_SOLVE_RETRIES + 1):
            try:
                param = await self._run_solver(scene, region, prefix)
            except InteractiveCaptchaRequired:
                # 重试不会把交互式挑战变成无痕通过，保留具体异常给网关返回 409。
                raise
            except SolverError as err:
                last_err = err
                logs.warn(
                    "captcha",
                    f"第 {attempt}/{settings.CAPTCHA_SOLVE_RETRIES} 次求解未果，重试…",
                )
                continue
            if attempt > 1:
                logs.ok("captcha", f"求解成功（第 {attempt} 次尝试）")
            return param

        raise SolverError(f"验证码求解失败: {last_err or '多次重试无结果'}")

    async def _run_solver(self, scene: str, region: str, prefix: str) -> str:
        if not settings.CAPTCHA_SOLVER_JS.exists():
            raise SolverError(
                f"未找到求解器 {settings.CAPTCHA_SOLVER_JS}，请先在 captcha_node 下执行 npm install"
            )
        argv = [
            settings.NODE_PATH,
            str(settings.CAPTCHA_SOLVER_JS),
            scene,
            region,
            prefix,
        ]
        try:
            proc = await self._create_subprocess(argv)
        except FileNotFoundError as err:
            raise SolverError(f"无法启动 Node（{settings.NODE_PATH}）") from err

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=settings.CAPTCHA_SOLVE_TIMEOUT
            )
        except asyncio.TimeoutError:
            await self._terminate(proc)
            raise SolverTimeout(
                f"求解超时（>{settings.CAPTCHA_SOLVE_TIMEOUT}s），已终止子进程"
            )
        except asyncio.CancelledError:
            # 调用方取消（如客户端断开 / 任务被 cancel）：先回收子进程，
            # 再原样重新抛出 CancelledError，绝不吞掉取消。
            await self._terminate(proc)
            raise

        returncode = proc.returncode
        if returncode != 0:
            if returncode == 6:
                raise InteractiveCaptchaRequired("需要在同一 VPS 浏览器环境中完成人机验证")
            raise SolverExitError(
                f"求解器非零退出（code={returncode}）{_tail(stderr)}".strip()
            )

        param = self._extract_param(stdout)
        if param is None:
            raise SolverOutputError("求解器输出缺少 VERIFY_PARAM 标记")
        if len(param) < MIN_VERIFY_PARAM_LEN:
            # 只透出长度，绝不回显参数值
            raise SolverOutputError(
                f"verifyParam 过短（len={len(param)}，需≥{MIN_VERIFY_PARAM_LEN}）"
            )
        return param

    async def _create_subprocess(self, argv: list[str]):
        """创建求解子进程。独立成方法，便于测试 patch / 计数 / 捕获参数。"""
        env = os.environ.copy()
        env.setdefault("ZCODE_CHROMIUM_PATH", settings.CHROMIUM_PATH)
        env.setdefault("ZCODE_CHROMIUM_PROFILE_DIR", str(settings.CHROMIUM_PROFILE_DIR))
        env.setdefault(
            "ZCODE_CAPTCHA_BROWSER_HEADLESS",
            "1" if settings.CAPTCHA_BROWSER_HEADLESS else "0",
        )
        env.setdefault(
            "ZCODE_CAPTCHA_BROWSER_TIMEOUT", str(settings.CAPTCHA_BROWSER_TIMEOUT)
        )
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(settings.CAPTCHA_SOLVER_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

    @staticmethod
    async def _terminate(proc) -> None:
        """终止并回收子进程，避免留下僵尸。

        reap 用 shield 保护：即便清理过程中又收到取消，被 shield 的
        ``proc.wait()`` 仍会继续推进至子进程被回收，不会因取消而跳过。
        """
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.shield(proc.wait())
        except asyncio.CancelledError:
            # 清理本身被取消：wait() 已被 shield，子进程仍会被回收，
            # 这里吞掉清理期的取消；调用点会原样重抛真正的 CancelledError。
            pass
        except (ProcessLookupError, ChildProcessError):
            pass

    @staticmethod
    def _extract_param(raw: bytes | None) -> str | None:
        if not raw:
            return None
        for line in raw.decode("utf-8", "ignore").splitlines():
            if line.startswith("VERIFY_PARAM="):
                return line[len("VERIFY_PARAM="):].strip()
        return None

    def invalidate(self) -> None:
        """兼容调用点；verifyParam 不再缓存，因此无需额外失效。"""

    async def close(self) -> None:
        with self._browser_lock:
            for challenge in self._browser_challenges.values():
                challenge.verify_param = None
            self._browser_challenges.clear()


captcha_manager = CaptchaManager()
