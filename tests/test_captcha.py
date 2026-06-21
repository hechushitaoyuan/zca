from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app import captcha
from app.captcha import (
    CaptchaManager,
    SolverError,
    SolverExitError,
    SolverOutputError,
    SolverTimeout,
)

VALID_PARAM = "a" * 40  # 长度 >= MIN_VERIFY_PARAM_LEN


def good_stdout(param: str = VALID_PARAM) -> bytes:
    return ("noise\nVERIFY_PARAM=" + param + "\n").encode("utf-8")


class FakeProc:
    """模拟 asyncio 子进程：communicate / kill / wait。"""

    def __init__(self, stdout=b"", stderr=b"", returncode=0, hang=False) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self.killed = False
        self.waited = False

    async def communicate(self):
        await asyncio.sleep(0.01)  # 让出控制权，放大并发竞争窗口
        if self._hang:
            await asyncio.sleep(3600)  # 由 wait_for 取消，触发超时
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True
        return self.returncode


class SpawnRecorder:
    """记录每次子进程创建的 argv 并返回预置 FakeProc。"""

    def __init__(self, proc_factory) -> None:
        self.calls: list[list[str]] = []
        self.procs: list[FakeProc] = []
        self._proc_factory = proc_factory

    async def __call__(self, argv):
        self.calls.append(list(argv))
        proc = self._proc_factory()
        self.procs.append(proc)
        return proc


def make_manager(recorder, *, scene="11xygtvd", region="sgp", prefix="no8xfe") -> CaptchaManager:
    mgr = CaptchaManager()

    async def fake_fetch():
        return {"sceneId": scene, "region": region, "prefix": prefix}

    mgr.fetch_config = fake_fetch  # type: ignore[assignment]
    mgr._create_subprocess = recorder  # type: ignore[assignment]
    return mgr


class CaptchaSolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_ttl_cache_starts_subprocess_once(self) -> None:
        recorder = SpawnRecorder(lambda: FakeProc(good_stdout(), returncode=0))
        mgr = make_manager(recorder)
        first = await mgr.get_verify_param()
        second = await mgr.get_verify_param()
        self.assertEqual(first, (VALID_PARAM, "sgp"))
        self.assertEqual(second, (VALID_PARAM, "sgp"))
        self.assertEqual(len(recorder.calls), 1)

    async def test_single_flight_concurrent_calls_start_once(self) -> None:
        recorder = SpawnRecorder(lambda: FakeProc(good_stdout(), returncode=0))
        mgr = make_manager(recorder)
        results = await asyncio.gather(*(mgr.get_verify_param() for _ in range(5)))
        self.assertEqual(len(recorder.calls), 1)
        for res in results:
            self.assertEqual(res, (VALID_PARAM, "sgp"))

    async def test_invalidate_forces_resolve(self) -> None:
        recorder = SpawnRecorder(lambda: FakeProc(good_stdout(), returncode=0))
        mgr = make_manager(recorder)
        await mgr.get_verify_param()
        self.assertEqual(len(recorder.calls), 1)
        mgr.invalidate()
        await mgr.get_verify_param()
        self.assertEqual(len(recorder.calls), 2)

    async def test_dynamic_params_passed_to_node(self) -> None:
        recorder = SpawnRecorder(lambda: FakeProc(good_stdout(), returncode=0))
        mgr = make_manager(recorder, scene="scene-x", region="hzn", prefix="pfx9")
        param, region = await mgr.get_verify_param()
        self.assertEqual(region, "hzn")
        argv = recorder.calls[0]
        # argv = [NODE_PATH, solver.js, scene, region, prefix]
        self.assertEqual(argv[2], "scene-x")
        self.assertEqual(argv[3], "hzn")
        self.assertEqual(argv[4], "pfx9")

    async def test_timeout_kills_and_reaps_subprocess(self) -> None:
        recorder = SpawnRecorder(lambda: FakeProc(hang=True))
        mgr = make_manager(recorder)
        with (
            patch.object(captcha.settings, "CAPTCHA_SOLVE_TIMEOUT", 0.05),
            patch.object(captcha.settings, "CAPTCHA_SOLVE_RETRIES", 1),
        ):
            with self.assertRaises(SolverError):
                await mgr.get_verify_param()
        self.assertEqual(len(recorder.procs), 1)
        proc = recorder.procs[0]
        self.assertTrue(proc.killed, "超时后必须 kill 子进程")
        self.assertTrue(proc.waited, "kill 后必须 wait 回收，避免僵尸")

    async def test_nonzero_exit_fails(self) -> None:
        recorder = SpawnRecorder(
            lambda: FakeProc(stdout=b"", stderr=b"fail=boom", returncode=5)
        )
        mgr = make_manager(recorder)
        with patch.object(captcha.settings, "CAPTCHA_SOLVE_RETRIES", 1):
            with self.assertRaises(SolverError) as ctx:
                await mgr.get_verify_param()
        # 退出码可见，但不应回显完整 stderr 之外的敏感参数
        self.assertIn("code=5", str(ctx.exception))

    async def test_missing_marker_fails(self) -> None:
        recorder = SpawnRecorder(
            lambda: FakeProc(stdout=b"some output without marker", returncode=0)
        )
        mgr = make_manager(recorder)
        with patch.object(captcha.settings, "CAPTCHA_SOLVE_RETRIES", 1):
            with self.assertRaises(SolverError):
                await mgr.get_verify_param()

    async def test_short_param_fails_without_leaking_value(self) -> None:
        secret_short = "TOPSECRET"  # len 9 < 32
        recorder = SpawnRecorder(
            lambda: FakeProc(stdout=good_stdout(secret_short), returncode=0)
        )
        mgr = make_manager(recorder)
        with patch.object(captcha.settings, "CAPTCHA_SOLVE_RETRIES", 1):
            with self.assertRaises(SolverError) as ctx:
                await mgr.get_verify_param()
        message = str(ctx.exception)
        self.assertNotIn(secret_short, message)  # 不得回显过短参数原值
        self.assertIn("len=9", message)

    async def test_diag_tail_is_bounded_and_single_line(self) -> None:
        long_noisy = "x" * 5000 + "\n\n   多行   噪声"
        trimmed = captcha._tail(long_noisy)
        self.assertLessEqual(len(trimmed), captcha.DIAG_MAX_LEN)
        self.assertNotIn("\n", trimmed)

    async def test_cancellation_kills_reaps_and_reraises(self) -> None:
        recorder = SpawnRecorder(lambda: FakeProc(hang=True))
        mgr = make_manager(recorder)

        # 真实创建求解任务，让它进入挂起的 communicate()，再取消。
        task = asyncio.ensure_future(mgr.get_verify_param())
        # 让任务推进到 communicate()（FakeProc.communicate 先 sleep(0.01) 再 hang）
        await asyncio.sleep(0.05)
        self.assertEqual(len(recorder.procs), 1, "取消前子进程应已创建")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        proc = recorder.procs[0]
        self.assertTrue(proc.killed, "取消后必须 kill 子进程")
        self.assertTrue(proc.waited, "kill 后必须 wait 回收，避免遗留进程")
        self.assertIsNone(mgr._cached, "取消不得产生缓存值")

    # ── P2：直接测 _run_solver 的具体异常子类 ─────────────────────────────────
    async def test_run_solver_timeout_subclass(self) -> None:
        recorder = SpawnRecorder(lambda: FakeProc(hang=True))
        mgr = make_manager(recorder)
        with patch.object(captcha.settings, "CAPTCHA_SOLVE_TIMEOUT", 0.05):
            with self.assertRaises(SolverTimeout):
                await mgr._run_solver("11xygtvd", "sgp", "no8xfe")
        self.assertTrue(recorder.procs[0].killed)
        self.assertTrue(recorder.procs[0].waited)

    async def test_run_solver_nonzero_exit_subclass(self) -> None:
        recorder = SpawnRecorder(lambda: FakeProc(stderr=b"fail", returncode=5))
        mgr = make_manager(recorder)
        with self.assertRaises(SolverExitError):
            await mgr._run_solver("11xygtvd", "sgp", "no8xfe")

    async def test_run_solver_missing_marker_subclass(self) -> None:
        recorder = SpawnRecorder(lambda: FakeProc(stdout=b"no marker here", returncode=0))
        mgr = make_manager(recorder)
        with self.assertRaises(SolverOutputError):
            await mgr._run_solver("11xygtvd", "sgp", "no8xfe")


if __name__ == "__main__":
    unittest.main()
