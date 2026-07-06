# ZCode + GLM-5.2 `extra_body` 修复指南（Windows）

## 用途

本指南用于修复以下组合中的桌面端请求错误：

- Windows 上的 ZCode Desktop
- 自建 OpenAI-compatible provider（例如 UniAPI）
- 上游模型为 NVIDIA Build 的 GLM-5.2
- 模型连接测试成功，但在本地文件夹中创建任务时返回 HTTP 400
- 上游错误包含：`Validation: Unsupported parameter(s): extra_body`

本文件不包含 API Key、服务器地址或其他设备私密信息，可复制到其他电脑使用。

## 已确认的根因

ZCode 3.2.5 的本地模型运行时会把 GLM-5.2 的思考等级转换为：

```json
{
  "openaiCompatible": {
    "extra_body": {
      "chat_template_kwargs": {
        "reasoning_effort": "max"
      }
    }
  }
}
```

ZCode 使用的 JavaScript OpenAI-compatible 适配器不会像 Python OpenAI SDK 那样自动展开 `extra_body`，因此 `extra_body` 被原样发送给 UniAPI/NVIDIA。NVIDIA Build 不接受这个顶层字段并返回 400。

正确形式是直接发送顶层 `chat_template_kwargs`：

```json
{
  "openaiCompatible": {
    "chat_template_kwargs": {
      "reasoning_effort": "max"
    }
  }
}
```

## 重要结论

- 只在 `~/.zcode/v2/config.json` 中设置 `supportsReasoning: false`，可能仅对独立 CLI 生效。
- ZCode 3.2.5 桌面端生成 provider registry 时会丢弃该字段。
- `bots-model-cache.v2.json` 是自动生成缓存，直接修改会在 ZCode 启动时被覆盖。
- 当前可靠修复点是 ZCode 实际加载的运行时 bundle：

```text
C:\Program Files\ZCode\resources\glm\zcode.cjs
```

## 给新 Codex 对话的执行要求

将本文件交给新的 Codex 对话后，可直接要求：

> 阅读项目根目录的 `ZCODE-GLM52-EXTRA-BODY-FIX.md`，在本机诊断当前 ZCode 版本是否仍存在该问题。先用最新桌面端 model-io 日志确认请求确实包含 `extra_body.chat_template_kwargs`，再备份并创建版本匹配的补丁。不要打印 API Key，不要修改项目文件。若需要覆盖 Program Files，请先请求我的管理员授权；触发 UAC 后由我手动确认。修复后必须从 ZCode 桌面端的新本地任务真实调用并核对日志，不能只用 CLI 测试。

## 一、修复前诊断

### 1. 确认 ZCode 安装位置和版本

默认安装位置：

```powershell
$exe = 'C:\Program Files\ZCode\ZCode.exe'
(Get-Item $exe).VersionInfo | Select-Object ProductVersion, FileVersion
```

如果安装目录不同，应从正在运行的进程确认：

```powershell
Get-CimInstance Win32_Process |
  Where-Object Name -eq 'ZCode.exe' |
  Select-Object ProcessId, ExecutablePath, CommandLine
```

### 2. 用失败请求 ID 定位 model-io

```powershell
$requestId = '<界面错误详情中的 request ID>'
rg -a -n --fixed-strings $requestId "$env:USERPROFILE\.zcode\cli"
```

在命中的 `model-io-sess_*.jsonl` 中确认存在类似内容：

```json
"openaiCompatible":{"extra_body":{"chat_template_kwargs":{"reasoning_effort":"max"}}}
```

注意：日志可能包含对话内容。只输出请求 ID 附近的必要片段，不要复制完整日志到公共位置。

### 3. 检查已安装 bundle

```powershell
$target = 'C:\Program Files\ZCode\resources\glm\zcode.cjs'
rg -a -o -m 5 '.{0,180}extra_body:\{chat_template_kwargs.{0,260}' $target
```

只有同时满足以下条件才继续：

1. 桌面端真实失败请求仍包含 `extra_body`。
2. bundle 中存在 GLM-5.2 对应的 `extra_body -> chat_template_kwargs` 映射。
3. NVIDIA/上游 400 明确指出不支持 `extra_body`。

如果升级后的 ZCode 已不再发送 `extra_body`，不要应用旧补丁。

## 二、创建带保护的补丁

ZCode bundle 是单行压缩文件，普通行级补丁工具可能无法修改。应使用“精确字符串 + 唯一匹配计数”方式，匹配数量不是 1 时立即停止。

以下脚本适用于已验证的 ZCode 3.2.5 bundle。未来版本的压缩变量名可能改变，不能在匹配数量为 0 时强行套用。

```powershell
$ErrorActionPreference = 'Stop'

$target = 'C:\Program Files\ZCode\resources\glm\zcode.cjs'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$workRoot = Join-Path $env:USERPROFILE '.zcode\v2\bundle-patch-extra-body'
$backup = Join-Path $workRoot "zcode.cjs.original-$stamp"
$patched = Join-Path $workRoot "zcode.cjs.patched-$stamp"

New-Item -ItemType Directory -Path $workRoot -Force | Out-Null
Copy-Item -LiteralPath $target -Destination $backup
Copy-Item -LiteralPath $target -Destination $patched

$old = 'function zyn(){return{[Vb]:{openaiCompatible:{extra_body:{chat_template_kwargs:{reasoning_effort:"max"}}}},[TC]:{openaiCompatible:{extra_body:{chat_template_kwargs:{reasoning_effort:"high"}}}},[L$e]:{openaiCompatible:{extra_body:{chat_template_kwargs:{enable_thinking:!1}}}}}}'
$new = 'function zyn(){return{[Vb]:{openaiCompatible:{chat_template_kwargs:{reasoning_effort:"max"}}},[TC]:{openaiCompatible:{chat_template_kwargs:{reasoning_effort:"high"}}},[L$e]:{openaiCompatible:{chat_template_kwargs:{enable_thinking:!1}}}}}'

$text = [System.IO.File]::ReadAllText($patched, [System.Text.Encoding]::UTF8)
$count = ([regex]::Matches($text, [regex]::Escape($old))).Count
if ($count -ne 1) {
    throw "补丁目标应唯一匹配 1 次，实际为 $count。停止操作并重新检查当前 ZCode 版本。"
}

$text = $text.Replace($old, $new)
[System.IO.File]::WriteAllText(
    $patched,
    $text,
    [System.Text.UTF8Encoding]::new($false)
)

$verify = [System.IO.File]::ReadAllText($patched, [System.Text.Encoding]::UTF8)
$oldAfter = ([regex]::Matches($verify, [regex]::Escape($old))).Count
$newAfter = ([regex]::Matches($verify, [regex]::Escape($new))).Count
if ($oldAfter -ne 0 -or $newAfter -ne 1) {
    throw "补丁候选文件校验失败：old=$oldAfter, new=$newAfter"
}

Get-FileHash $target, $backup, $patched -Algorithm SHA256
Write-Host "原版备份：$backup"
Write-Host "补丁候选：$patched"
```

### 未来版本变量名改变时

不要做宽泛的全局替换。先找到创建 GLM-5.2 reasoning provider options 的函数，只修改以下三个语义等价项：

```text
max     : openaiCompatible.extra_body.chat_template_kwargs.reasoning_effort = "max"
high    : openaiCompatible.extra_body.chat_template_kwargs.reasoning_effort = "high"
nothink : openaiCompatible.extra_body.chat_template_kwargs.enable_thinking = false
```

目标是仅移除中间的 `extra_body` 包装，保留 `chat_template_kwargs` 及其内部值。

## 三、管理员覆盖

`Program Files` 默认禁止普通权限写入。覆盖前必须：

1. 确认原版备份已存在且哈希与安装文件一致。
2. 完全退出 ZCode，包括后台进程。
3. 向用户请求管理员授权。
4. 触发 UAC 后由用户手动点击“是”；不要自动操作 UAC。

Codex 可在获得授权后执行：

```powershell
Get-Process ZCode -ErrorAction SilentlyContinue | Stop-Process -Force

$src = '<上一节生成的补丁候选绝对路径>'
$dst = 'C:\Program Files\ZCode\resources\glm\zcode.cjs'
$payload = "`$ErrorActionPreference='Stop'; Copy-Item -LiteralPath '$src' -Destination '$dst' -Force"
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($payload))

$process = Start-Process powershell.exe `
    -Verb RunAs `
    -WindowStyle Hidden `
    -ArgumentList @('-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded) `
    -Wait `
    -PassThru

if ($process.ExitCode -ne 0) {
    throw "管理员覆盖失败，退出码：$($process.ExitCode)"
}

Get-FileHash $src, $dst -Algorithm SHA256
```

安装文件与补丁候选的 SHA256 必须完全一致。

## 四、必须使用桌面端验证

不能只用独立 CLI 验证，因为该问题曾表现为 CLI 成功、ZCode Desktop 失败。

### 1. 重启 ZCode

```powershell
Start-Process 'C:\Program Files\ZCode\ZCode.exe'
```

### 2. 在本地文件夹中新建任务

选择目标自建 provider 和 `glm-5.2`，发送：

```text
这是桌面端补丁验证。不要调用工具，不要修改文件，只回复 OK。
```

期望界面返回 `OK`，而不是 `Provider rejected the model request`。

### 3. 核对最新 model-io

```powershell
$latest = Get-ChildItem "$env:USERPROFILE\.zcode\cli\rollout\model-io-*.jsonl" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

$lines = Get-Content -LiteralPath $latest.FullName |
    Where-Object { $_ -match '"request"' }

$lines | ForEach-Object {
    [pscustomobject]@{
        HasExtraBody = $_ -match 'extra_body'
        HasTopLevelChatTemplate = $_ -match 'openaiCompatible":\{"chat_template_kwargs"'
    }
}
```

期望每条新请求均为：

```text
HasExtraBody              = False
HasTopLevelChatTemplate   = True
```

期望的 provider options 形态：

```json
"openaiCompatible": {
  "chat_template_kwargs": {
    "reasoning_effort": "max"
  }
}
```

还应在模型使用数据库或日志中确认 `main_turn` 和 `session_title` 均为 `completed`。

## 五、回滚

如果补丁导致 ZCode 无法启动或出现其他异常，使用对应版本的原版备份回滚：

```powershell
Get-Process ZCode -ErrorAction SilentlyContinue | Stop-Process -Force

$backup = '<原版备份绝对路径>'
$target = 'C:\Program Files\ZCode\resources\glm\zcode.cjs'
$payload = "`$ErrorActionPreference='Stop'; Copy-Item -LiteralPath '$backup' -Destination '$target' -Force"
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($payload))

$process = Start-Process powershell.exe `
    -Verb RunAs `
    -WindowStyle Hidden `
    -ArgumentList @('-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded) `
    -Wait `
    -PassThru

if ($process.ExitCode -ne 0) {
    throw "回滚失败，退出码：$($process.ExitCode)"
}

Start-Process 'C:\Program Files\ZCode\ZCode.exe'
```

不要用旧版本备份覆盖新版本 ZCode。每次升级后必须重新备份当前 bundle，再为当前版本创建补丁。

## 六、升级后的检查清单

每次升级 ZCode 或在新电脑安装后：

1. 查看 ZCode 版本及实际安装路径。
2. 新建本地文件夹任务并记录失败 request ID。
3. 检查最新 model-io 是否仍含 `extra_body`。
4. 如果上游已接受请求或 ZCode 已发送顶层 `chat_template_kwargs`，不要打补丁。
5. 如果问题仍存在，为当前版本单独创建原版备份和补丁候选。
6. 唯一匹配断言通过后，再申请管理员覆盖。
7. 从 ZCode Desktop 新任务真实验证。
8. 核对 `extra_body=False`、顶层 `chat_template_kwargs=True`、请求状态 `completed`。
9. 确认项目 Git 工作区没有无关改动。

## 当前已验证环境记录

- ZCode Desktop：3.2.5
- ZCode CLI runtime：0.15.0
- 操作系统：Windows
- 修复方式：展开 GLM-5.2 reasoning provider options 中的 `extra_body`
- 桌面端验证结果：`main_turn` 与 `session_title` 均成功
- 注意：ZCode 更新程序可能覆盖 `resources\glm\zcode.cjs`，升级后需要重新检查

