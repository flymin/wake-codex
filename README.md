# wake-codex

`wake-codex` 周期性执行 task 自带的 trigger。当 trigger 返回 go 后，工具读取
task 当前的消息文件，并通过 `codex queue` 把消息排到目标线程的下一轮。默认的
`queue-only` 模式兼容普通 Codex TUI；需要强制确认共享 app-server loaded 状态时可用
`strict` 模式。

工具是前台、单 task、一次性进程。需要后台运行时使用 tmux、nohup 或 systemd；
它本身不管理 daemon 或 PID 文件。

## 环境

激活已安装项目依赖的 Python 环境，并把 Codex 工具目录加入 `PATH`：

```bash
export PATH="/path/to/wake-codex/bin:${PATH}"
```

## Task 格式

每个 task 是一个独立目录，必须包含固定名称 `task.yaml`：

```yaml
version: 1
name: my-task
thread_id: 123e4567-e89b-42d3-a456-426614174000
trigger: trigger.sh
message: message.txt
```

- `trigger` 和 `message` 必须是 task 目录内的相对路径，不能逃逸到目录外。
- `thread_id` 必须是小写、带连字符的规范 UUID。启动时会把目标分成 active、
  archived 和 missing：`sessions/` 中的 active session（或仍在 index 中的 session）
  才能继续；`archived_sessions/` 中的 session 和 missing session 都以退出码 2
  拒绝，且不执行 trigger。archived transcript 不再是可投递目标。
- trigger 必须带有 shebang 且可执行。runner 直接执行它，不使用 shell 解释 YAML。
- trigger 退出码 `0` 表示 go，`1` 表示 block，其他退出码表示可重试错误。
- `message.txt` 使用 UTF-8。runner 不修改该文件，并在每次即将调用 queue 时重新读取。
- 空消息、临时不可读消息、trigger 错误和临时 queue 错误会按轮询间隔重试。
  queue 明确返回 session archived、not loaded 或 not found 时是不可重试的配置错误。
- `task.yaml` 在启动时读取一次；运行中只保证消息文件可以安全更新。

## 活跃检查模式

`--mode queue-only` 是默认模式：不调用 `thread/loaded/list`，也不使用远程
app-server 参数。trigger 返回 go 后直接执行：

```bash
codex queue --thread <thread_id> --message <message>
```

它兼容普通 TUI，但 queue 成功只代表消息已排队，不能证明当前存在消费该消息的
Codex 进程。成功后 runner 默认输出提示：消息只有在目标会话存在活跃 Codex 进程时
才会执行；否则请运行 `codex resume <thread_id>`。使用 `--silent 1` 可只隐藏这条提示。

`--mode strict` 保留严格检查。目标 CLI/TUI 必须连接同一个 app-server endpoint，
且目标线程必须出现在受支持的 `thread/loaded/list` 结果中。runner 在每次执行 trigger
前和真正 queue 前各检查一次；连接失败、RPC 超时、响应不可解析或目标未 loaded 时
均 fail closed，退出码为 2。strict queue 会通过 `--remote` 使用同一个 endpoint。
工具不通过 `ps`、锁文件或 transcript 时间戳猜测活跃性。

strict 默认使用 `unix://` control socket。自定义 socket 可使用
`--app-server-endpoint unix:///absolute/path.sock`，并让目标 Codex 客户端连接同一
endpoint。需要时可通过 `codex app-server daemon start` 启动受管 app-server。

## 使用示例

`tasks/` 用于本机真实任务并被 Git 完整忽略。复制脱敏模板后填写真实 session ID、
Slurm job ID 和消息：

```bash
cp -a tasks.example/slurm-jobs tasks/my-slurm-jobs
${EDITOR:-vi} tasks/my-slurm-jobs/task.yaml tasks/my-slurm-jobs/trigger.sh tasks/my-slurm-jobs/message.txt
```

前台启动：

```bash
wake-codex \
  --codex "$(command -v codex)" \
  --poll-interval 60 \
  --timeout -1 \
  tasks/my-slurm-jobs
```

示例 trigger 使用 `sacct` 监控三个占位 job。所有 job 都进入 Slurm 终态后返回 go；
至少一个 job 仍活跃时返回 block。可以用 `SACCT_BIN=/other/path/sacct` 覆盖入口。

`tasks.example/immediate` 是立即返回 go 的最小模板。两个 example 都使用 nil UUID 和
极简消息，不包含真实 session、job 或业务信息。

## CLI

```text
usage: wake-codex [-h] --codex CODEX [--codex-home CODEX_HOME]
                  [--app-server-endpoint ENDPOINT]
                  [--mode {queue-only,strict}] [--silent {0,1,2,3}]
                  [--poll-interval SECONDS] [--timeout SECONDS]
                  [--command-timeout SECONDS] [--force] task_folder
```

- `--codex-home` 指定用于验证会话的 Codex 状态目录。默认依次使用环境变量
  `CODEX_HOME` 和 `~/.codex`；它必须与 `--codex` 入口实际使用的状态目录一致。
- `--mode` 默认 `queue-only`；`strict` 启用两次 app-server loaded 检查。
- `--silent` 接受 `0` 到 `3` 的整数，默认 `0`。级别是累加的：

| level | 输出行为 |
| ---: | --- |
| 0 | 输出全部日志 |
| 1 | 隐藏 queue-only 成功后的 note |
| 2 | 在 level 1 基础上隐藏 trigger poll、消息等待和重试轮询输出 |
| 3 | 只保留最终 queue 成功、timeout、明确错误或其他终止结果 |

  level 3 的 queue 成功行仍包含 Codex queue 的 stdout/stderr 摘要；配置错误和投递结果
  不确定等重要终止信息不会被隐藏。
- `--app-server-endpoint` 只用于 strict，默认 `unix://`，也接受
  `unix:///absolute/path.sock`。活跃检查和 queue 固定使用同一个 endpoint。
- `--poll-interval` 默认 60 秒，也用于 trigger/queue 错误后的重试。
- `--timeout` 是整个进程的总超时；默认 `-1`，表示不超时。
- `--command-timeout` 默认 30 秒，限制单次 trigger 或 queue 子进程。
- `--force` 忽略已经投递或结果不确定的状态，允许再次发送。使用前应先检查
  目标线程，避免重复消息。

退出码：

| code | 含义 |
| ---: | --- |
| 0 | queue 成功 |
| 2 | 参数、配置、session archived/missing/not loaded、路径或 Codex 预检/明确拒绝错误 |
| 3 | 总超时 |
| 4 | 已发送，或上次发送结果不确定 |
| 5 | 同一 task 已有 runner 持锁 |
| 130 | 用户中断 |

## 状态与重复投递

runner 在 task 目录创建 `.wake-codex.lock` 和 `.wake-codex-state.json`。状态文件
使用原子替换写入，只记录线程、尝试次数、时间和消息 SHA256，不保存消息正文。

调用 queue 前先写入 `sending`，成功后再写入 `delivered`。如果进程在 queue
执行期间崩溃或 queue 超时，下一次启动会拒绝自动重发，因为无法判断消息是否已经
入队。确认目标线程后，可显式使用 `--force` 恢复。

queue 的临时非零错误会写成 `retrying`；重启 runner 会直接继续发送阶段，不再执行
trigger。每次重试仍会重新读取消息文件；strict 模式还会重新检查 loaded 状态。
无论使用哪种模式，queue 明确返回 archived、not loaded 或 not found 时都会写成终态
`rejected` 并以退出码 2 结束，不会进入无限重试。修复目标会话后需要显式 `--force`
才能再次投递。

strict 的 loaded 检查与 queue 都指向同一个 endpoint，但 Codex CLI 当前没有提供把
`thread/loaded/list` 与 queue 合并为原子操作的接口。第二次检查完成后、queue 到达前
仍有极短的关闭竞态；若此时 session 关闭，queue 自身的 not-loaded/not-found 拒绝会
被当作不可重试配置错误。queue 超时依旧属于投递结果不确定，状态保留为 `sending`。

## 测试

测试全部使用 fake trigger、fake Slurm 和 fake Codex，不会向真实线程发送消息：

```bash
pytest
bash -n tasks.example/immediate/trigger.sh tasks.example/slurm-jobs/trigger.sh
```
