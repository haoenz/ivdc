# Python 3.14 重构迁移

这是破坏性接口更新，无旧参数别名或双配置键。升级前保留 `.ivdc/`，不需要搬动视频、备份和分片。

| 原接口 | 新接口 |
| --- | --- |
| Python 3.11+ | Python 3.14+ |
| `opt --what-if` / `clean --what-if` | `opt --dry-run` / `clean --dry-run` |
| `opt --throttle N` | `opt --workers N` |
| `OptOptions.what_if / throttle` | `OptimizeOptions.dry_run / workers` |
| `DlOptions` | `DownloadOptions` |
| `EncodeConfig` | `EncodingConfig` |
| `impersonateDomains` | `impersonate_domains` |
| `impersonateTarget` | `impersonate_target` |

配置需要手工重命名键，域名必须写成字符串数组。未知键和损坏内容现在报错，不再默默使用默认值。
`IVDC_CONFIG` 继续有效；Windows 继续使用 APPDATA，macOS 改用
`~/Library/Application Support/ivdc/config.json`，Unix 遵循 XDG_CONFIG_HOME。
使用旧自定义位置时设置 `IVDC_CONFIG`，或将配置复制到新平台路径。首次下载不再自动写配置，
示例见 [config.example.json](config.example.json)。

Python 调用方从 `ivdc.optimization_plan` 导入 `OptimizeOptions`，从 `ivdc.download` 导入
`DownloadOptions`。`run_optimize`、`run_download`、`run_clean` 现在返回有类型的结果，
不接收 Console 或返回进程退出码。前两者接受可选事件回调；终端、错误提示和退出码由 CLI 处理。
底层子进程使用 `with ProcessRunner() as runner:` 与 `runner.capture / runner.stream`，
不再提供全局进程集合或全局终止函数。启动失败抛带上下文的异常。

行为变化：

- dry-run 不再触发恢复或临时文件清理，也不创建配置、日志或 manifest；孤立备份进入只读计划。
- 源文件保持在原位直到新成片原子提交。首次备份采用复制，需要额外磁盘空间；已有备份不覆盖。
  `--no-keep-backup` 仅清理本次新建备份。
- 旧版本留下的缺席源文件会从备份复制恢复，原备份仍保留。整片模式也保留可恢复分片。
- `.ivdc/` 目录布局、版本 1 manifest、`seg_NNN.mp4` 与 ffmpeg 拼接格式继续可读。
  没有 resume.json 或使用旧版元数据的分片通过严格媒体校验后可复用，升级时补记完成凭据。
  新任务使用版本 2 resume.json 和逐片 `seg_NNN.complete.json`，后者绑定任务身份及内容
  SHA-256。新版分片没有完成凭据时不视为完成，先归档再重压；`*.partial.mp4` 不参与拼接。
  升级旧分片时如果在补记凭据前中断，下次同样保守归档并重压，原数据不会直接删除。
  参数变化会归档原分片目录，需用户明确执行 `clean --segs` 才清理归档。
- 分片与拼接成片增加视频流时长、帧数、流信息和完整解码校验，不再使用固定半秒容差。
  新增校验会增加处理时间；时序信息不足或校验不通过时停止该文件并保留原视频与备份，
  即使指定 `--no-keep-backup` 也不会在校验失败后删除备份。
- 单项探测、准备或编码失败都会反映在最终退出码，损坏/不可读 manifest 不再被空对象覆盖。
- 清单读写错误会中止下载；写入失败的任务保持可重试。完成记录与任务清单间中断仍可能产生
  重复记录，这是两个文件不具备事务提交的边界。
- 显式下载标题现在真正影响文件名；重名时保留原下载文件，不覆盖已有文件。
- GPU 可用性必须通过 `nvidia-smi -L` 探测，不再将 Windows 上仅存在 nvcuda.dll 当作有卡。
  Windows 也会检查 System32 下的 nvidia-smi.exe；工具不可用时降级 CPU。
- 正常终端摘要仍保留体积、耗时和完成状态；诊断采用 logging，调试输出格式可能变化。

BOM 输入、Windows 文件名处理、Windows 终端功能、CUDA/NVENC 和外部工具协议继续保留。
