# ivdc

基于 yt-dlp 和 FFmpeg 的命令行视频下载与压制工具，支持 H.265、AV1、VP9、CUDA 加速和可恢复分片。

## 安装

需要 Python 3.14+、uv，以及 PATH 中的 `ffmpeg` 和 `ffprobe`。在仓库目录执行：

```bash
uv tool install .
ivdc --help
```

## 压制视频

```bash
ivdc opt                                  # 当前目录的 MP4，默认 x265、中画质、2 个并行任务
ivdc opt -p /path/to/videos --codec av1     # 指定目录与编码
ivdc opt --quality high                    # 画质：high / medium / low
ivdc opt --cuda off                        # 纯 CPU
ivdc opt --cuda encode                     # CUDA 解码 + NVENC 编码
ivdc opt --segments 10                     # 每 10 分钟一片，中断后可继续
ivdc opt --dry-run                         # 只查看计划，不修改文件
```

默认尝试 CUDA 解码，硬件加速不可用时回退到 CPU。已是目标编码的文件会跳过。

输出为 MP4，保留原文件名和位置；音轨、字幕直接复制。编码前备份原文件，输出通过完整性校验后
才替换原文件，失败时保留原件。默认保留备份，`--no-keep-backup` 可在成功后删除本次新建备份。
备份、分片和日志存放在视频目录的 `.ivdc/` 下，请预留磁盘空间。

支持单视频流；额外视频、封面、附件或 MP4 不支持的音轨、字幕会报错。更多选项见 `ivdc opt --help`。

## 下载视频

在当前目录创建 `tbd.txt`，每行一个 URL，可附带标题：

```text
https://example.com/video
我的视频 https://example.com/another
```

```bash
ivdc dl                                   # 读取 tbd.txt，完成记录写入 d.txt
ivdc dl --tasks tasks.txt --done done.txt --max 5
ivdc dl --on-error skip                    # 失败项移到队尾，继续后续任务
```

成功项自动移出任务清单。默认遇到失败停止，失败项保留以便重试。
同一任务清单或视频目录请只运行一个写入任务。

## 清理文件

```bash
ivdc clean --all --dry-run                 # 先查看清理范围
ivdc clean --logs --tmp                    # 删除日志和临时输出
ivdc clean --backups --segs                 # 删除备份和分片
```

清理仅作用于 `.ivdc/`；原位置缺少视频时，对应备份会受到保护。删除分片会同时删除其归档。

## 配置

不提供配置文件时使用默认值。可复制 [config.example.json](config.example.json)，
通过 `IVDC_CONFIG` 指定路径，或放到平台默认位置：

| 平台 | 路径 |
| --- | --- |
| Windows | `%APPDATA%\ivdc\config.json` |
| macOS | `~/Library/Application Support/ivdc/config.json` |
| Linux | `$XDG_CONFIG_HOME/ivdc/config.json`，默认 `~/.config/ivdc/config.json` |

例如，调整 x265 的高画质档位：

```json
{
  "quality_profiles": {
    "libx265": {"high": 20}
  }
}
```

省略的档位使用默认值；数值越小画质越高、文件通常越大，各档须满足 `high < medium < low`。

## 开发

```bash
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

测试包含使用临时视频的 FFmpeg 集成测试。可用 `git config --local core.hooksPath .githooks`
启用提交检查；[CI](.github/workflows/ci.yml) 执行静态检查和完整测试。
详细行为与实现约定见 [SPEC.md](SPEC.md)。
