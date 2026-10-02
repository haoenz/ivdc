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
ivdc opt --quality high                    # 高画质；默认 medium，也可选择 low
ivdc opt --dry-run                         # 包括恢复、清理在内的只读计划
ivdc opt --mask                            # 匿名显示名称与错误；日志保留原始信息
ivdc opt --debug                           # 标准 logging 诊断，包括底层命令
```

`--quality high|medium|low` 表示高、中、低画质，高画质通常文件更大；它不控制分辨率或编码速度。
默认 `medium`。各实际编码器的档位可在配置文件的 `quality_profiles` 中调整，见下文。
程序先决定实际编码器，再选择对应数值；NVENC 不可用时使用软件编码器的同名档位。
终端显示实际编码器、档位和 CRF/CQ 数值；实际处理时也写入日志，dry-run 不写日志。
已是目标编码的文件仍跳过，改变画质档位不会强制重压这些成片。

成片保留输入的文件名和目录，递归过滤也保留相对路径。源文件在成片提交之前始终可用：
首次处理先复制备份，编码到 `.ivdc/tmp/`，成功后原子替换源文件；失败清理临时输出，
源文件和已有备份不被覆盖。此策略需要额外磁盘空间。`--no-keep-backup` 只删除本次新建的备份，
此前已有的备份始终保留。

只重新编码视频，全部音轨和字幕从原文件直接复制，保留原编码、流顺序、语言与默认/强制标志。
分片只编码画面，拼接时一次性合入原文件的全部音轨和字幕，避免跨片字幕被切断或重复。
音轨、字幕比画面更长时也不截断尾部。提交前核对流数量、顺序、编码、语言、标志及音频声道数、
采样率；即使 ffmpeg 返回成功，校验不符也不会覆盖原文件。

当前支持单视频流及任意数量的音轨、字幕；额外视频、封面、数据或附件流会明确报告单项失败。
输出容器仍是 MP4（文件名不变）；原音轨或字幕编码不受 MP4 支持时，例如 SubRip 字幕，
任务会报错并保留源文件与备份，不自动转换或丢弃这些流。

上次运行遗留的孤立备份会复制恢复到原位置。切换整片模式不会自动删除可恢复分片。
分片先写入唯一的 `*.partial.mp4`，编码成功并通过媒体校验后，协调线程才发布为
`seg_NNN.mp4`，并原子保存对应的 `seg_NNN.complete.json`。完成记录绑定源文件属性、
编码参数、切片方案和分片内容的 SHA-256；新版分片缺少记录或内容变化时不能直接复用。
发布后、记录写入前中断的分片会保留归档并重压，避免误把未确认的文件当作完成结果。

整片、分片和拼接成片均检查视频编码、尺寸、帧数和视频流时长，并完整解码校验。
整片与拼接成片还核对全部原始流的信息，解码视频与音频；输入解码错误也会中止编码，
不能仅凭 ffmpeg 返回成功提交。无法确认视频完整性时保留原件和备份。
视频与音频起点不同时，分片按视频实际起点定位，拼接恢复音画相对偏移；
整片保留输入时间基准，避免将非整帧偏移取整。整体起点可以归零，各流相对时间保持。
恒定帧率核对帧率及切片区间应有帧数，时长容差按半帧和时间基准计算，不再统一放宽半秒；
短于一帧的尾片并入前一片。拼接成片也必须通过校验后才能替换原视频、清理新建备份。
缺少可靠时序信息或无法确认完整性时报告失败并保留源文件与备份。
变帧率视频不以平均帧率推算帧数，仅容许时间戳精度误差，边界无法确认时会保守报错。
额外解码和内容校验会增加 CPU 与磁盘读取开销。

只有旧版元数据明确匹配源文件属性、编码参数和切片方案，且视频无相对起点偏移时，
没有完成记录的旧分片才可在完整校验后补记凭据。缺少 resume.json、身份不符或混入新版
凭据的旧片先归档再重压，不能因尺寸、时长相同就归入当前源；历史数据不会直接删除。
旧片中的音轨与字幕不参与拼接，最终仍从原文件复制。已有当前凭据且内容未变的分片复用
时不重复解码，最终成片仍完整解码校验。时间轴策略升级会归档旧版版本 2 的分片目录。
编码参数变化时归档整个旧分片目录，
不能复用的分片和上次留下的临时分片也先归档；可用 `clean --segs --dry-run` 检查归档。
此前版本 2 的断点记录缺少流处理策略，升级后会归档原分片目录并重新编码，不删除旧分片。

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
├── segs/<相对文件名>/           分片、完成记录、concat_list.txt、resume.json
├── tmp/<相对文件名>             待提交输出
├── logs/YYYY-MM-DD.log          处理记录与诊断
└── manifest.json                编码、大小、耗时、状态和失败原因
```

源文件、备份和分片目录由一轮任务协调管理，同一视频目录应只运行一个写入任务。
manifest 缺失可以从空记录开始；格式损坏、未知版本或读取失败会中止并保留原数据，
修复或另存损坏的 manifest 后再重试。分片恢复并不依赖历史 manifest 的成功标记。

终端用 Rich 展示，诊断使用标准 logging。`--log` 可指定压制日志路径；日志/manifest
持久化失败会使整轮失败，已提交的视频与备份保持不变。

`opt --mask` 和 `dl --mask` 使正常进度使用随机标签，运行期错误与终端诊断只显示固定摘要，
避免外部错误带出文件名、路径、标题或 URL。覆盖 dry-run 探测失败、恢复失败、下载器警告、
文件读写失败和未预期异常；空计划也不回显可能含文件名的筛选规则。
同时指定 `--debug` 时屏幕仍匿名，调试详情只进入已配置的 ivdc 日志 handler。
不使用 mask 时保留完整错误提示和调试输出；需要查看终端错误详情时关闭 mask。

匿名仅影响终端展示，不改文件名、任务清单、完成记录、manifest、业务结果或处理日志。
压制的单项失败和恢复失败在实际处理日志中保留原始原因。下载默认不创建日志文件，
dry-run 和启动前失败也不额外创建日志，因此这些场景不会提示“详见处理日志”。
CLI 匿名模式暂停 ivdc 日志向根 logger 传播，防止其它根控制台 handler 重复输出原文；
直接挂在 ivdc logger 上的文件 handler 仍收到原始记录，退出时恢复日志设置。

日志路径在启动媒体处理和任何文件修改前校验，包括 `--dry-run`。它不能与当前输入视频
（含将被跳过的文件）、待恢复文件或 manifest 冲突，也不能占用恢复所需的目录。
`.ivdc/` 内只允许将日志写入 `logs/` 子目录，备份、分片和临时输出目录均禁止。
符号链接会按实际目标检查，已有文件还会检查硬链接是否指向同一输入或状态文件。
路径冲突返回参数错误（退出码 2），不创建日志、不恢复、不清理。
普通自定义日志（例如 `--log reports/run.log`）仍支持创建父目录和追加已有记录。

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
  "impersonate_target": "chrome-136",
  "quality_profiles": {
    "libx265": {"high": 24, "medium": 28, "low": 32}
  }
}
```

配置键只接受 snake_case。未知键、错误类型和损坏 JSON 会明确报错；BOM 输入仍支持。
域名按主机名或其子域匹配，不匹配 URL 查询字符串。TLS impersonate 是可选能力，
需要 `curl_cffi` 及支持目标的 yt-dlp；可选能力不可用时告警后使用普通下载。

`quality_profiles` 按实际编码器名配置，名称来自 ffmpeg（包括 `libvpx-vp9` 中的连字符）。
可以省略整个配置项、某个编码器或某一档，省略部分沿用内置值。完整示例见
[config.example.json](config.example.json)。内置档位如下：

| 实际编码器 | 参数 | high 高 | medium 中（默认） | low 低 | 配置整数范围 |
| --- | --- | --- | --- | --- | --- |
| `libx265` | CRF | 24 | 28 | 32 | 0–51 |
| `libsvtav1` | CRF | 28 | 35 | 42 | 1–63 |
| `libvpx-vp9` | CRF | 25 | 31 | 37 | 0–63 |
| `hevc_nvenc` | CQ | 23 | 28 | 33 | 1–51 |
| `av1_nvenc` | CQ | 25 | 30 | 35 | 1–63 |

这些参数数值越小，目标画质越高。合并默认值后，每个编码器必须满足 `high < medium < low`。
不接受未知编码器、未知档位、字符串、布尔值、小数或越界值；所有显式配置都校验，
包括本轮未使用的编码器。压制会在启动媒体工具、备份、恢复、清理和日志写入前读取并验证配置，
错误返回退出码 2，dry-run 同样检查。已有仅包含下载设置的配置无需补齐新字段。

例如，只想把 x265 的高画质改为 20，可将 `quality_profiles` 写为
`{"libx265": {"high": 20}}`；中、低档和其它编码器保持默认。
分片身份包含实际编码参数：修改当前档位数值或切换实际编码器后，旧分片目录先归档再重压；
只修改未使用的档位，不影响已有完成分片的复用。

默认值的依据（2026-10-03 查阅）与边界：

- x265 以官方默认 CRF 28 为中档，高、低档由本项目取上下各 4。
  [x265 官方说明](https://x265.readthedocs.io/en/stable/cli.html#quality-rate-control-and-rate-distortion-options)
- SVT-AV1 以官方默认 35 为中档，高、低档取上下各 7。项目使用 FFmpeg 的整数 CRF 接口，
  固定支持 1–63；FFmpeg 7.1 的 0 表示未指定，不作为固定画质，新版 SVT 的扩展范围不自动开放。
  [SVT-AV1 官方指南](https://github.com/AOMediaCodec/SVT-AV1/blob/main/Docs/svt-av1_encoder_user_guide.md)、
  [FFmpeg 7.1.1 适配源码](https://github.com/FFmpeg/FFmpeg/blob/n7.1.1/libavcodec/libsvtav1.c)
- VP9 以 Google 的 1080p 点播建议值 31 作为起点，高、低档取上下各 6。
  原建议涉及分辨率和受限码率，本项目仅借用该数值作为初始策略，实际使用 `-b:v 0` 的质量模式。
  [Google 推荐值](https://developers.google.com/media/vp9/settings/vod)、
  [Google 码率模式说明](https://developers.google.com/media/vp9/bitrate-modes)
- NVENC 使用显式 `-rc vbr -cq N -b:v 0`。保留项目原有 HEVC 28、AV1 30 作为中档，
  高、低档取上下各 5；这些是项目初始策略，并非 NVIDIA 官方推荐的三档值。
  CQ 0 表示自动，所以配置排除 0；HEVC 与 AV1 的上限分别为 51、63。
  [NVIDIA 目标画质模式](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-video-encoder-api-prog-guide/index.html#rate-control)、
  [FFmpeg HEVC 选项](https://github.com/FFmpeg/FFmpeg/blob/n7.1.1/libavcodec/nvenc_hevc.c)、
  [FFmpeg AV1 选项](https://github.com/FFmpeg/FFmpeg/blob/n7.1.1/libavcodec/nvenc_av1.c)

三档不是跨编码器等画质承诺，也不是无损承诺。软件编码测试使用生成的小样本验证执行流程；
真实素材的主观画质、不同分辨率及 NVENC 实机效果仍需按用途评估，可据此调整配置。
