"""验证码求解（无浏览器）。

通过 Node + jsdom 在模拟浏览器环境中运行阿里云无痕 SDK，
求得 verifyParam（X-Aliyun-Captcha-Verify-Param）。不再启动真实浏览器。

- 缓存：求得的 verifyParam 在 TTL 内复用
- 并发：同一时刻只跑一个求解进程（single-flight），其余请求等待后命中缓存
- 重试：单次求解偶发失败时自动重试
- 受控子进程：可配置超时；超时后 kill 并回收，避免僵尸进程
- region：随配置接口返回，与求解结果一并交回网关，写入校验请求头
"""

from __future__ import annotations

import asyncio
import time

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


def _tail(raw: bytes | str | None, limit: int = DIAG_MAX_LEN) -> str:
    """把诊断输出压成单行并限长；绝不用于承载 verifyParam。"""
    if not raw:
        return ""
    text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
    text = " ".join(text.split())
    return text[-limit:]


class CaptchaManager:
    def __init__(self) -> None:
        self._cached: str | None = None
        self._cached_region: str = _DEFAULT_REGION
        self._cached_at: float = 0.0
        self._lock = asyncio.Lock()
        self._config_cache: dict | None = None
        self._config_cache_at: float = 0.0

    # ── 配置 ─────────────────────────────────────────────────────────────────
    async def fetch_config(self) -> dict:
        now = time.time() * 1000
        if self._config_cache and now - self._config_cache_at < settings.CAPTCHA_CONFIG_CACHE_TTL:
            return self._config_cache
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.get(
                    "https://zcode.z.ai/api/v1/client/configs"
                    "?app_version=3.0.0&platform=win32"
                )
            res.raise_for_status()
            captcha = ((res.json().get("data") or {}).get("configs") or {}).get("captcha")
            if captcha:
                self._config_cache = captcha
                self._config_cache_at = now
                return captcha
        except (httpx.HTTPError, ValueError) as err:
            logs.warn("captcha", f"获取配置失败，使用默认: {err}")
        return {
            "enabled": True,
            "prefix": _DEFAULT_PREFIX,
            "region": _DEFAULT_REGION,
            "sceneId": _DEFAULT_SCENE,
        }

    # ── 求解 ─────────────────────────────────────────────────────────────────
    async def get_verify_param(self, port: int | None = None) -> tuple[str, str]:
        """返回 (verifyParam, region)。TTL 内复用缓存；并发 single-flight。"""
        now = time.time() * 1000
        if self._cached and now - self._cached_at < settings.CAPTCHA_CACHE_TTL:
            return self._cached, self._cached_region

        async with self._lock:
            # 二次检查：等锁期间可能已被其他请求填充
            if self._cached and time.time() * 1000 - self._cached_at < settings.CAPTCHA_CACHE_TTL:
                return self._cached, self._cached_region

            config = await self.fetch_config()
            region = config.get("region") or _DEFAULT_REGION
            param = await self._solve(config)
            self._cached = param
            self._cached_region = region
            self._cached_at = time.time() * 1000
            return param, region

    async def _solve(self, config: dict) -> str:
        scene = config.get("sceneId") or _DEFAULT_SCENE
        region = config.get("region") or _DEFAULT_REGION
        prefix = config.get("prefix") or _DEFAULT_PREFIX

        last_err: SolverError | None = None
        for attempt in range(1, settings.CAPTCHA_SOLVE_RETRIES + 1):
            try:
                param = await self._run_solver(scene, region, prefix)
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
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(settings.CAPTCHA_SOLVER_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
        self._cached = None
        self._cached_at = 0.0

    async def close(self) -> None:
        pass


captcha_manager = CaptchaManager()
