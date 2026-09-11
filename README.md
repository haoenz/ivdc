# ivdc

用 yt-dlp 下载视频、用 ffmpeg 批量压制的命令行工具。支持 H.265 / AV1 / VP9，
可选用 CUDA 解码与 NVENC 硬件编码，支持分片可恢复编码。

行为规格见 [SPEC.md](SPEC.md)。

## 安装

需要 `ffmpeg` 与 `ffprobe` 在 PATH 中（`ivdc dl` 还需要联网）。

```bash
uv tool install .        # 或 pipx install .
ivdc --help
```

开发：

```bash
uv sync
uv run pytest
```

## 使用

### 压制

```bash
ivdc opt                                 # 当前目录所有 mp4，x265，2 路并行
ivdc opt -p D:\Videos --codec av1 --throttle 4
ivdc opt --cuda encode                   # CUDA 解码 + NVENC 编码
ivdc opt --segments 10                   # 每 10 分钟一片，中断后可从断点续压
ivdc opt --crf 24                        # 覆盖默认压缩质量
ivdc opt --what-if                       # 只看看会做什么，一个文件都不动
ivdc opt --mask --debug                  # 文件名匿名 + 打印底层命令
```

压制过程不会就地覆盖源文件：源文件先移入 `.ivdc/backups/`，压到 `.ivdc/tmp/`，
成功后才替换回原位；失败则把备份移回来。中断（Ctrl+C、断电）之后重新跑同一条
命令即可从断点继续。

### 下载

`tbd.txt` 每行一条，标题与链接顺序可互换：

```
我的视频 https://example.com/v
https://example.com/another
```

```bash
ivdc dl                          # 逐条下载，完成项移入 d.txt
ivdc dl --max 5                  # 本次只处理 5 条
ivdc dl --on-error skip          # 失败不阻塞，坏链挪到清单末尾下次重试
```

下载成功一条就从清单里原子地删掉一条，中途崩溃不会把清单截断。

### 清理

```bash
ivdc clean --all --what-if       # 先看能释放多少
ivdc clean --logs --tmp          # 只清日志与临时文件
```

清理范围严格限于 `.ivdc/`。源文件已不在工作目录的备份会被跳过——那是唯一一份数据。

## 状态目录

```
<视频目录>/.ivdc/
├─ backups/     源文件备份
├─ segs/        分片（拼接清单同目录）
├─ tmp/         临时输出
├─ logs/        按天分文件的日志
└─ manifest.json  编码 / 体积 / 耗时 / 状态
```

工作目录里始终只见成片，`*.mp4` 的匹配永远不会命中中间产物。

## 配置

`%APPDATA%\ivdc\config.json`（首次运行 `ivdc dl` 时自动生成，可用 `IVDC_CONFIG`
环境变量覆盖位置）：

```json
{
  "impersonateDomains": [],
  "impersonateTarget": "chrome-136"
}
```

`impersonateDomains` 里列出的域名需要 TLS 指纹伪装。库模式的 impersonate 依赖
`curl_cffi`（`pip install curl_cffi`）；没装时会打印提示后照常下载，而不是失败。
