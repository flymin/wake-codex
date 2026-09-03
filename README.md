# wake-codex

`wake-codex` 执行 task 自带的 trigger，并在 trigger 返回 go 后通过 `codex queue`
向目标线程提交消息。它支持两种运行方式：原有的前台 one-shot runner，以及可同时管理
多个 cron task 的 foreground daemon。

## 安装

在准备运行该工具的 Python 环境中安装项目及依赖，并将项目入口加入 `PATH`：

```bash
python -m pip install .
export PATH="/path/to/wake-codex/bin:${PATH}"
```

也可以使用 `pyproject.toml` 安装生成的 `wake-codex` console script。daemon 与所有 client
命令必须使用包含 `PyYAML` 和 `croniter` 的环境。

## Task 格式

每个 task 是一个独立目录，固定使用 `task.yaml`：

```yaml
version: 1
name: my-task
thread_id: 123e4567-e89b-42d3-a456-426614174000
trigger: trigger.sh
message: message.txt
schedule: "*/5 * * * *"
timezone: Asia/Shanghai
lifecycle: once
mode: queue-only
```

`trigger` 和 `message` 必须是 task 目录内的相对路径。trigger 必须带 shebang 且可执行；
退出码 `0` 表示 go，`1` 表示 block，其他值表示检查错误。消息必须是非空 UTF-8 文本。
`thread_id` 必须是小写 canonical UUID。

daemon submit 要求五字段 `schedule`。`timezone` 是可选 IANA 时区，默认 daemon 所在机器
的本地时区。`lifecycle` 默认为 `once`：queue 成功后结束；`continuous` 使用边沿触发：
初始为 armed，go 只投递一次，此后必须实际观察到 block 才会重新 armed。

task 通过目录引用注册。提交时冻结 YAML 中的名称、线程、schedule、时区、生命周期、
模式以及 trigger/message 路径；修改这些字段后需要 cancel 并重新 submit。trigger 文件
和 message 文件的内容在每次执行时重新读取，因此可以在 task 运行期间更新。消息正文
不会写入 daemon 数据库或事件元数据。

## Daemon

daemon 始终在前台运行，本项目不提供 `start/status/stop` 包装。可先手动运行：

```bash
wake-codex daemon
```

one-shot 和 daemon 的 `--codex` 都默认使用当前 `PATH` 中的 `codex`；需要固定其他入口
时再显式传入 `--codex /path/to/codex`。

另一个 terminal 中可以随时管理 task：

```bash
wake-codex submit tasks/my-task
wake-codex list
wake-codex list --all --json
wake-codex show TASK_ID
wake-codex events TASK_ID --limit 50 --full
wake-codex cancel TASK_ID
```

`submit` 注册后立即返回 task ID 和下次检查时间。ID 可以使用不歧义的 UUID 前缀；名称
也可用于 `show/events/cancel`，但重复名称需要改用 ID。`list` 默认只显示 active task，
`--all` 包含终态。

daemon 默认最多并行执行 4 个不同 task，同一 task 永不重叠。可用
`--max-workers`、`--command-timeout` 和 `--retry-interval` 调整。cron 漏跑不会逐次补跑：
daemon 重启后会立即合并检查一次，再计算下一个 cron 时间。

默认状态目录依次取 `WAKE_CODEX_HOME`、`XDG_STATE_HOME/wake-codex`、
`~/.local/state/wake-codex`，所有 daemon client 命令都可用 `--state-dir` 覆盖。状态目录
权限为 `0700`，Unix socket 为 `0600`。SQLite 使用 WAL。

### systemd user service

复制 `systemd/wake-codex.service.example` 到 `~/.config/systemd/user/wake-codex.service`，
将 `@WAKE_CODEX@` 和 `@CODEX@` 替换为对应入口的绝对路径，然后执行：

```bash
systemctl --user daemon-reload
systemctl --user enable --now wake-codex.service
systemctl --user status wake-codex.service
```

若机器没有可用的 user systemd session bus，可由 tmux、容器 supervisor 或其他进程
管理器直接托管同一个 foreground daemon 命令。若需要退出登录后继续运行，系统管理员
还可能需要为该用户启用 linger。

## 活跃检查

`queue-only` 是默认模式，不调用 `thread/loaded/list`，兼容普通 Codex TUI。queue 成功
只代表命令接受了消息；消息只有在目标会话存在活跃 Codex 进程时才会执行，否则需要
运行 `codex resume <thread_id>`。one-shot runner 会打印这条 note，`--silent 1` 可隐藏。

`strict` 在 trigger 前和 queue 前分别通过受支持的 `thread/loaded/list` 检查 loaded
状态，并让 queue 使用同一个 `--app-server-endpoint`。未 loaded、响应不可确认或检查
失败都会 fail closed。检查和 queue 指向同一 endpoint，但 Codex 当前没有把两者合成
原子操作的接口，二次检查后仍存在很短的关闭竞态；queue 自身的明确拒绝会覆盖该竞态。

submit 始终先检查 session：`archived_sessions/` 中的 archived session 和不存在的
session 都直接拒绝。它们不会执行 trigger，也不会成为离线投递目标。

## 状态与故障语义

daemon 在 task 目录持续持有 `.wake-codex.lock`，因此 active daemon task 不能同时由
one-shot runner 执行。queue 前后仍写兼容的 `.wake-codex-state.json`；其中只有状态、
次数和消息 SHA256，不含消息正文。

- trigger 返回 `0` 时 event status 为 `ok` 并进入投递；返回 `1` 时 status 为 `block`，
  task 正常保持 `scheduled` 并等待下一个 cron；其他返回码为 `error`，超时为 `timeout`。
- 临时 queue 错误或消息暂不可读：进入内部 retry，不重新执行 trigger。
- archived、not loaded、not found：终态 `rejected`，不重试。
- queue 超时、发送中 daemon 崩溃、发送中取消：终态 `ambiguous`，不自动重试。
- scheduled/checking 取消：`cancelled`；发送中的取消：`ambiguous`。
- 重启恢复时 checking 重新调度，retrying 立即到期，sending/cancelling 变为 ambiguous。

每次 trigger、strict check 和 queue 的完整 stdout/stderr 都永久保存在 gzip artifact；
数据库记录路径、大小和 SHA256。`show/events` 默认只显示摘要，`--full` 读取完整输出。
上述 trigger status 语义只影响修复后新产生的 event，不迁移已有历史记录。
没有自动 retention，可显式清理：

```bash
wake-codex purge outputs --older-than 30
wake-codex purge outputs --task TASK_ID --type trigger --confirm
wake-codex purge tasks --status delivered --before 2026-01-01T00:00:00+00:00
wake-codex purge tasks --status delivered --before 2026-01-01T00:00:00+00:00 --confirm
```

purge 默认 dry run，必须加 `--confirm` 才删除。output purge 保留事件元数据；task purge
只处理 terminal task，并级联删除其事件和 artifacts。可按 task/type/status/时间或 age
筛选。

## One-shot runner

不带子命令的旧用法保持兼容，`schedule` 字段不是必需的：

```bash
wake-codex --poll-interval 60 --timeout -1 tasks/my-task
```

`--codex` 默认使用当前 `PATH` 中解析到的 `codex`（等价于 `command -v codex`）；
未找到时会在执行前报错，也可以显式传入其他入口。主要参数还包括
`--mode {queue-only,strict}`、`--codex-home`、
`--app-server-endpoint`、`--command-timeout` 和 `--force`。`--silent` 接受 0 到 3：
0 输出全部，1 隐藏 queue-only note，2 另隐藏 poll 输出，3 只保留最终结果。退出码 0
表示成功，2 表示配置/session/明确拒绝错误，3 表示总超时，4 表示已投递或结果不确定，
5 表示 task 被锁定，130 表示中断。

`tasks/` 用于真实任务并被 Git 完整忽略。`tasks.example/` 是脱敏模板，包含立即触发和
Slurm job 终态检查示例。

## 测试

测试只使用 fake trigger、fake Slurm 和 fake Codex，不会向真实线程发送消息：

```bash
pytest
bash -n tasks.example/immediate/trigger.sh tasks.example/slurm-jobs/trigger.sh
```
