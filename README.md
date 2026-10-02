# ivdc

Python 3.14+ 命令行视频下载与压制工具。下载使用 yt-dlp；压制支持 H.265、AV1、VP9、
CUDA 解码、NVENC 编码和可恢复分片。业务规格见 [SPEC.md](SPEC.md)，升级前请看
[迁移说明](MIGRATION.md)。

## 安装与开发

需要 Python 3.14+，以及 PATH 中的 `ffmpeg`、`ffprobe`；下载还需要网络。
GPU 模式使用 `nvidia-smi -L` 检查真实设备；探测不可用时提示并退回 CPU。

```bash
uv tool install .
ivdc --help
```

开发与验证：

```bash
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
uv run pytest -m integration -rs
```

集成测试使用临时目录生成小视频，真实调用 ffmpeg，不接触个人视频和下载清单。
缺少工具的跳过不代表验证通过。CUDA/NVENC、Windows 控制台和公网下载须在对应环境另行验证。

仓库提供版本控制下的 Git hooks。每次新克隆后，在仓库根目录启用：

```bash
uv sync --locked
git config --local core.hooksPath .githooks
```

`pre-commit` 检查暂存区空白错误、锁文件一致性、Ruff 格式与 lint、Pyright 类型；
`pre-push` 重跑这些检查，并要求 PATH 中存在 ffmpeg 和 ffprobe，然后运行完整 pytest，
其中已经包含真实 ffmpeg 集成测试，不重复执行。任一命令失败都会阻止提交或推送。
hooks 只检查，不自动格式化、暂存或 stash；可分别用 `sh .githooks/pre-commit` 和
`sh .githooks/pre-push` 手动运行。Git for Windows 使用随附的 shell 执行它们。

除暂存区空白检查外，检查对象是当前工作区，不是暂存区快照或推送的每个历史提交。
部分暂存、推送其它分支时，需要另行验证实际提交内容。pytest 会列出跳过原因；
例如 Windows 上的 POSIX SIGINT 测试会跳过，hooks 通过不代表这些环境能力已验证。
架构审查、文档与行为一致性仍需人工审查；本地 hooks 也不能替代 CI。
启用前若已有自定义 `core.hooksPath`，请合并已有检查；移除本仓库配置可用
`git config --local --unset core.hooksPath`。

## 压制

```bash
ivdc opt                                  # 当前目录 mp4，x265，2 个工作线程
ivdc opt -p /path/to/videos --codec av1 --workers 4
ivdc opt --cuda off                        # 纯 CPU
ivdc opt --cuda encode                     # CUDA 解码 + 可用时的 NVENC 编码
ivdc opt --segments 10                     # 每 10 分钟一片，可断点恢复
ivdc opt --crf 24
ivdc opt --dry-run                         # 包括恢复、清理在内的只读计划
ivdc opt --mask                            # 屏幕用随机标签，日志保留真实名称
ivdc opt --debug                           # 标准 logging 诊断，包括底层命令
```

成片保留输入的文件名和目录，递归过滤也保留相对路径。源文件在成片提交之前始终可用：
首次处理先复制备份，编码到 `.ivdc/tmp/`，成功后原子替换源文件；失败清理临时输出，
源文件和已有备份不被覆盖。此策略需要额外磁盘空间。`--no-keep-backup` 只删除本次新建的备份，
此前已有的备份始终保留。

上次运行遗留的孤立备份会复制恢复到原位置。切换整片模式不会自动删除可恢复分片。
分片工作线程只编码并返回结果，协调线程负责拼接和提交。已有分片经编码与时长校验后复用；
新任务记录源文件属性、编码参数和切片方案，参数变化时将原分片目录归档，再开始新任务。
不能复用的已有分片也先归档，避免重压时覆盖；可用 `clean --segs --dry-run` 检查归档。

`--dry-run` 不恢复、不清理、不生成配置、不写日志文件或 manifest，也不创建状态目录。
它直接探测待恢复备份，将缺席源文件纳入计划。诊断和计划仍可输出到终端。

一次运行中单项失败不妨碍其它文件，最终退出码为 1。Ctrl+C 会取消排队任务、终止并回收
探测/编码/拼接子进程，然后退出进度线程。重新运行可继续；已提交的成片不会回退。

## 下载

`tbd.txt` 每行一个任务，标题和 URL 顺序可互换，支持 UTF-8 BOM 与空白行：

```text
我的视频 https://example.com/video
https://example.com/another
```

```bash
ivdc dl
ivdc dl --tasks tasks.txt --done finished.txt --max 5
ivdc dl --on-error skip
```

显式标题决定落盘名称；没有标题时使用下载器提供的标题。两者都清理 Windows 非法字符与
设备名。播放列表使用序号区分显式标题；发生重名或无法改名时保留下载器原文件并给出警告。

成功项先追加完成记录，再原子移出任务清单。默认首次失败停止，失败项留在原位；
`--on-error skip` 将失败项移到队尾，每轮只尝试一次。`--max` 限制尝试次数。
读取错误、完成记录写入错误、清单写回错误都会报告失败，不会假装清单为空或继续丢弃任务。
完成记录和任务清单不是跨文件事务：两次写入之间中断时，任务可能重试、完成记录可能重复，
但任务不会因记录失败而丢失。运行时请保持同一清单只有一个写入者。

## 清理

```bash
ivdc clean --all --dry-run
ivdc clean --logs --tmp
ivdc clean --backups --segs
```

清理严格限于 `.ivdc/`。对应文件不在工作目录的备份被保护。显式 `--segs` 会删除
所选目录下的分片和归档，请先看 dry-run。部分删除失败返回 1，按类别报告实际删除项数。

## 状态与日志

```text
<视频目录>/.ivdc/
├── backups/<相对文件名>         原视频备份
├── segs/<相对文件名>/           分片、concat_list.txt、resume.json
├── tmp/<相对文件名>             待提交输出
├── logs/YYYY-MM-DD.log          处理记录与诊断
└── manifest.json                编码、大小、耗时、状态和失败原因
```

源文件、备份和分片目录由一轮任务协调管理，同一视频目录应只运行一个写入任务。
manifest 缺失可以从空记录开始；格式损坏、未知版本或读取失败会中止并保留原数据，
修复或另存损坏的 manifest 后再重试。分片恢复并不依赖历史 manifest 的成功标记。

终端用 Rich 展示，诊断使用标准 logging。`--log` 可指定压制日志路径；日志/manifest
持久化失败会使整轮失败，已提交的视频与备份保持不变。

## 配置

配置缺失时使用默认值，不自动创建；可复制 [配置示例](config.example.json)。
`IVDC_CONFIG` 优先于以下平台默认路径：

| 平台 | 默认路径 |
| --- | --- |
| Windows | `%APPDATA%\ivdc\config.json`；APPDATA 缺失时使用用户目录下 `AppData/Roaming` |
| macOS | `~/Library/Application Support/ivdc/config.json` |
| Linux / 其它 Unix | `$XDG_CONFIG_HOME/ivdc/config.json`；变量未设置或非绝对路径时用 `~/.config/ivdc/config.json` |

```json
{
  "impersonate_domains": [],
  "impersonate_target": "chrome-136"
}
```

配置键只接受 snake_case。未知键、错误类型和损坏 JSON 会明确报错；BOM 输入仍支持。
域名按主机名或其子域匹配，不匹配 URL 查询字符串。TLS impersonate 是可选能力，
需要 `curl_cffi` 及支持目标的 yt-dlp；可选能力不可用时告警后使用普通下载。
