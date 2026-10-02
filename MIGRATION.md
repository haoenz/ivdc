# Python 3.14 重构迁移

这是破坏性接口更新，无旧参数别名或双配置键。升级前保留 `.ivdc/`，不需要搬动视频、备份和分片。

| 原接口 | 新接口 |
| --- | --- |
| Python 3.11+ | Python 3.14+ |
| `opt --what-if` / `clean --what-if` | `opt --dry-run` / `clean --dry-run` |
| `opt --throttle N` | `opt --workers N` |
| `opt --crf N` | `opt --quality high\|medium\|low`，数值在配置的 `quality_profiles` 中维护 |
| `OptOptions.what_if / throttle` | `OptimizeOptions.dry_run / workers` |
| `OptimizeOptions.crf` | `OptimizeOptions.quality`，类型为 `ivdc.quality.Quality` |
| `DlOptions` | `DownloadOptions` |
| `EncodeConfig` | `EncodingConfig` |
| `impersonateDomains` | `impersonate_domains` |
| `impersonateTarget` | `impersonate_target` |

配置需要手工重命名键，域名必须写成字符串数组。未知键和损坏内容现在报错，不再默默使用默认值。
`IVDC_CONFIG` 继续有效；Windows 继续使用 APPDATA，macOS 改用
`~/Library/Application Support/ivdc/config.json`，Unix 遵循 XDG_CONFIG_HOME。
使用旧自定义位置时设置 `IVDC_CONFIG`，或将配置复制到新平台路径。首次下载不再自动写配置，
示例见 [config.example.json](config.example.json)。

画质入口改为 `--quality`，默认 `medium`，不保留 `--crf` 别名。已有下载配置无需修改，
可添加 `quality_profiles`，按 `libx265`、`libsvtav1`、`libvpx-vp9`、`hevc_nvenc`、`av1_nvenc`
分别覆盖 high/medium/low；只写需要改变的档位即可。完整默认表、范围与依据见 README。
例如原来使用 `--crf 20` 的 x265 任务，可配置 `{"quality_profiles":{"libx265":{"high":20}}}`，
以后使用 `--quality high`。硬件与软件数值独立，回退时不再把硬件值直接交给软件编码器。
配置错误（包括未使用的编码器条目）会在启动媒体工具、恢复、清理及备份前返回退出码 2。

SVT-AV1 中档从此前 CRF 30 调整为官方默认 35；需要保持旧数值时配置
`{"quality_profiles":{"libsvtav1":{"medium":30}}}`。其它编码器中档数值保持不变，但 NVENC
现在显式采用 VBR/CQ 并取消平均码率目标，不能据此承诺输出与旧版本相同。
新版分片按实际 encoder_args 判断复用；实际参数变化时先归档原目录再重压，不删除旧分片。

Python 调用方从 `ivdc.optimization_plan` 导入 `OptimizeOptions`，从 `ivdc.download` 导入
`DownloadOptions`。`run_optimize`、`run_download`、`run_clean` 现在返回有类型的结果，
不接收 Console 或返回进程退出码。前两者接受可选事件回调；终端、错误提示和退出码由 CLI 处理。
底层子进程使用 `with ProcessRunner() as runner:` 与 `runner.capture / runner.stream`，
不再提供全局进程集合或全局终止函数。启动失败抛带上下文的异常。
`resolve_encode_config` 接收 `quality` 和 `quality_profiles`，不再接收 `crf`；
`EncodingConfig` 用 `encoder`、`quality`、`quality_parameter`、`quality_value` 表示解析结果，
不再用同一个 `crf` 字段兼指 CRF 和 CQ。

行为变化：

- dry-run 不再触发恢复或临时文件清理，也不创建配置、日志或 manifest；孤立备份进入只读计划。
- 源文件保持在原位直到新成片原子提交。首次备份采用复制，需要额外磁盘空间；已有备份不覆盖。
  `--no-keep-backup` 仅清理本次新建备份。
- 旧版本留下的缺席源文件会从备份复制恢复，原备份仍保留。整片模式也保留可恢复分片。
- `.ivdc/` 目录布局、版本 1 manifest、`seg_NNN.mp4` 与 ffmpeg 拼接格式继续可读。
  明确匹配旧版元数据且视频相对起点为零的分片，通过严格媒体校验后可复用并补记完成凭据。
  缺少 resume.json 的片不再自动复用，先归档再重压；不会直接删除旧数据。
  新任务使用版本 2 resume.json 和逐片 `seg_NNN.complete.json`，后者绑定任务身份及内容
  SHA-256。新版分片没有完成凭据时不视为完成，先归档再重压；`*.partial.mp4` 不参与拼接。
  升级旧分片时如果在补记凭据前中断，下次同样保守归档并重压，原数据不会直接删除。
  参数变化会归档原分片目录，需用户明确执行 `clean --segs` 才清理归档。
- 整片、分片与拼接成片均执行视频流时长、帧数、流信息和完整解码校验，不再使用固定半秒容差。
  新增校验会增加处理时间；时序信息不足或校验不通过时停止该文件并保留原视频与备份，
  即使指定 `--no-keep-backup` 也不会在校验失败后删除备份。
- 整片与分片均显式保留全部原音轨和字幕，直接复制而不重新编码；语言与默认/强制标志不变。
  分片只压制画面，拼接时一次性合入原音轨和字幕；整片不再按视频时长截断音轨、字幕尾部。
  输出仍为 MP4，原流编码不受支持时明确失败并保留源文件与备份，需要用户另行选择容器或转换。
  多视频流、封面、数据和附件流现在明确拒绝，避免悄悄丢失内容。
  身份明确匹配且时间轴兼容的旧格式分片仍可验证并复用视频；无身份的旧片归档重压。
  版本 2 的断点记录新增时间轴策略，旧记录缺失时归档原目录并重压，旧片不会删除。
- 非零视频起点按容器与视频的相对时间处理，保留音画偏移；整片使用输入时间精度。
- 单项探测、准备或编码失败都会反映在最终退出码，损坏/不可读 manifest 不再被空对象覆盖。
- 清单读写错误会中止下载；写入失败的任务保持可重试。完成记录与任务清单间中断仍可能产生
  重复记录，这是两个文件不具备事务提交的边界。
- 显式下载标题现在真正影响文件名；重名时保留原下载文件，不覆盖已有文件。
- GPU 可用性必须通过 `nvidia-smi -L` 探测，不再将 Windows 上仅存在 nvcuda.dll 当作有卡。
  Windows 也会检查 System32 下的 nvidia-smi.exe；工具不可用时降级 CPU。
- 正常终端摘要仍保留体积、耗时和完成状态；诊断采用 logging，调试输出格式可能变化。
- `--mask` 现在同时隐藏运行期错误和终端诊断中的文件名、路径、标题及 URL，
  `--mask --debug` 也不会在终端暴露原文。非匿名模式继续显示完整详情。
  压制日志、manifest、下载清单及业务结果仍保存原始内容，不自动匿名化这些文件。
  下载默认无日志文件；dry-run 与启动前失败不为诊断新增写入，需要终端详情时关闭 mask。
  嵌入式调用使用匿名 CLI 时，ivdc 暂停向根 logger 传播日志；如需独立文件记录，应将
  文件 handler 挂在 ivdc logger，运行退出后原日志设置会恢复。
- 自定义 `--log` 现在在处理前检查路径冲突：不能占用当前输入、待恢复文件、manifest，
  或写入 `.ivdc/backups`、`.ivdc/segs`、`.ivdc/tmp`；`.ivdc/` 内仅允许写入 `logs/`。
  符号链接与硬链接同样校验，dry-run 也会对冲突返回退出码 2。
  需要调整冲突路径时，可使用 `.ivdc/logs/custom.log` 或普通的外部日志路径。

BOM 输入、Windows 文件名处理、Windows 终端功能、CUDA/NVENC 和外部工具协议继续保留。
