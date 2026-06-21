# R1 独立审查：Start Plan 求解器技术 Gate 与定向返修

- 角色：乙方 Codex
- 审查对象：丙方 R1 工作区 diff 与交付报告
- 证据等级：E1 + E2
- Gate 结论：**暂缓纳入，存在 1 项 P1；定向返修后复验**

## 1. 白名单与交付核对

实际代码变化仅包含：

- `captcha_node/solver.js`
- `app/captcha.py`
- `app/routes/gateway.py`
- `tests/test_gateway.py`
- `tests/test_captcha.py`

均在 R1 白名单内。未发现 Git 写操作、真实账号读取、真实 Messages 请求或 VPS 改动。

`captcha_node/solver.js` 与 PR #5 `aac09c7` 的实际差异仅为 stderr 诊断脱敏/限长，交付描述与 diff 一致。

## 2. 乙方独立复跑（E2）

在 `C:\Czode\zca` 执行：

| 命令 | 退出码 | 结果 |
|---|---:|---|
| `python -m compileall -q app tests main.py` | 0 | 通过 |
| `python -m unittest discover -s tests -v` | 0 | 24/24 通过 |
| `node --check captcha_node/solver.js` | 0 | 通过 |
| `npm --prefix captcha_node test --if-present` | 0 | 无 npm test 脚本，安全跳过 |

本轮仍未执行真实 captcha/API/VPS 测试，不能升级为 E3/E4。

## 3. P1：任务取消路径可能遗留 Node 子进程

### 事实

`app/captcha.py::_run_solver()` 只捕获 `asyncio.TimeoutError`。当承载首个 single-flight 求解的请求在 `proc.communicate()` 期间断开或任务被取消时，会抛出 `asyncio.CancelledError`；当前路径不会调用 `_terminate(proc)`，Node 进程可能继续运行至自身超时。

这违反“受控子进程在异常/取消路径完整回收”的验收意图，并可能在客户端频繁断开时积累暂存进程，因此定为 P1。

### 丙方定向返修白名单

仅允许修改：

- `app/captcha.py`
- `tests/test_captcha.py`
- 原交付报告（追加返修小节）

禁止顺手修改其他文件。

### 修复要求

1. `proc.communicate()` 被取消时必须 kill 并 `await wait()` 回收，然后原样重新抛出 `CancelledError`，不得吞掉取消。
2. 清理过程应防止清理 await 自身被取消而跳过回收；采用最小、清晰的 asyncio 取消处理。
3. 新增回归测试，真实创建求解任务、让其进入挂起的 `communicate()`、调用 `task.cancel()`，断言：
   - 调用者收到 `asyncio.CancelledError`；
   - 假子进程 `killed is True`；
   - 假子进程 `waited is True`；
   - 不产生缓存值。
4. 现有 24 项测试必须继续通过。

### 可选 P2

将 timeout/nonzero/output 测试从只断言 `SolverError` 加强为断言对应具体子类；不阻断 Gate，不得因此扩大改动。

## 4. 返修交付

丙方在原报告 `2606211957-R1-汇报-StartPlan求解器开发交付.md` 末尾追加“R1 Gate 定向返修”小节，写明实际 diff、测试命令、通过数和退出码。不得执行 Git 写操作。完成后释放文件锁并通知乙方复验。

## 5. 返修复验与最终 Gate（2026-06-21 20:26）

丙方实际返修严格限定为 `app/captcha.py` 与 `tests/test_captcha.py`：

- 取消路径先终止子进程并通过 `asyncio.shield(proc.wait())` 推进回收，随后重新抛出原 `CancelledError`。
- 新增真实 task cancel 回归，断言 killed、waited、无缓存。
- timeout、非零退出、缺标记分别断言具体异常子类。

乙方独立复跑：

| 命令 | 退出码 | 结果 |
|---|---:|---|
| `python -m compileall -q app tests main.py` | 0 | 通过 |
| `python -m unittest discover -s tests -v` | 0 | 28/28 通过 |
| `node --check captcha_node/solver.js` | 0 | 通过 |
| `npm --prefix captcha_node test --if-present` | 0 | 安全跳过 |
| `git diff --check` | 0 | 通过 |

最终 Gate：**R1 E2 通过，允许纳入下一次 Git 提交。**

边界：未做 E3/E4，不宣称真实 captcha 或 3012 已解决。
