# ADR-007：短视频流水线、媒体与发布包契约

- 状态：accepted
- 日期：2026-08-11
- 决策者：项目维护者
- 替代：无
- 被替代：无

## 背景

M1 的 `GenerationRequest` 与 `GenerationTerminalRepository` 只服务单一 FACT_CARD：每个 Job 锁定项目 revision，成功即把项目从 `generating` 转为 `review_required`。短视频包含并行且可局部恢复的多个步骤；复制这套终态会让首个步骤过早结束项目，其余步骤发生 revision 冲突，也无法表达多镜头素材、钉住审核集合或安全导出渠道包。

## 决策

### 版本化 DAG 与逻辑产物

首期定义 `short_video_v1`，拓扑与必需输入固定为：

```text
SOURCE → FACT_CARD → TOPIC_BRIEF → SCRIPT → STORYBOARD → ASSET_MANIFEST
                                      │                       │
                                      └→ VOICEOVER → SUBTITLES│
                              STORYBOARD + ASSET_MANIFEST → COVER
                ASSET_MANIFEST + VOICEOVER + SUBTITLES → VIDEO_MASTER
 FACT_CARD + ASSET_MANIFEST + SUBTITLES + COVER + VIDEO_MASTER → QC_REPORT
             QC_REPORT(pass) + VIDEO_MASTER + COVER + evidence → REVIEW_BUNDLE
                   REVIEW_BUNDLE(approved) → CHANNEL_PACKAGE(channel)
```

`PipelineDefinition` 保存稳定 `pipeline_kind`、版本、节点、必需输入 slot、输出 slot 和依赖；运行时不得用隐式代码顺序改变 DAG。一个项目可有多个 `PipelineRun`，同一 run 钉住 definition/profile 版本和起始输入。`project_current_artifacts` 的 kind 级指针不足以表达镜头集合；M2 使用稳定 `logical_key`（如 `storyboard`、`asset/shot-003`、`channel/douyin/package`）标识产物流，版本只在同一 logical key 内单调递增。

`ArtifactRef` 至少包含 `artifact_id`、`version`、`kind`、`logical_key` 和 `sha256`。Artifact 不可变，公开 metadata 可包含 MIME、字节数、时长、宽高和 codec，但不得暴露服务器路径。

### StepRun、Job 与幂等恢复

`StepRun` 表示逻辑步骤，`Job` 表示该步骤的租约执行尝试。StepRun 钉住 pipeline/run、step kind/version、排序后的精确输入 ArtifactRef、规范输入 fingerprint、参数、profile、输出 logical key，以及预留的 output Artifact ID/version。其语义状态为 `pending | succeeded | failed | invalidated`；`pending` 的 queued/running/retry 状态由关联 Job 投影，避免两套执行真相。

创建 StepRun 的 canonical request hash 覆盖 `project_id`、`pipeline_run_id`、step kind/version、完整且排序的输入 refs、规范 JSON 参数、profile version、输出 kind/logical key。相同作用域 Idempotency-Key 与相同摘要复用同一 StepRun/Job；异摘要冲突。每个活动 run 的同一 node/input fingerprint 由数据库唯一约束去重。

成功处理器必须在持有 live lease 的单一事务内验证 reservation、精确输入、输出 kind/logical key/摘要，写入全部 Artifact、血缘、证据和 current pointer，标记 StepRun 与 Job succeeded，并使下游 ready 状态可被唯一约束的 reconciler 发现。不得保留 Worker 自动 succeed 或复制 FACT_CARD 专用终态。永久失败在同一事务标记 StepRun/Job failed，且不移动 pointer；retryable 只释放/重排同一 Job，保持同一 reservation 和最终路径。租约前崩溃可重领同一执行；终态提交后不可重领。

上游 pointer 改变不覆盖历史，而是把依赖旧 ref 的下游 StepRun、ReviewBundle 和 approval 标记 invalidated。显式 rerun 使用新 key，按 logical key 预留下一个版本，只重做受影响子图。项目只在 PipelineRun 启动时进入 `generating`，并在全部必需节点成功、QC blocker 为零且 ReviewBundle 原子建立后进入 `review_required`；单个步骤成功不得改变项目为 `review_required`。任一 `generating` 项目必须存在可恢复的活动 PipelineRun/Job。

### 文件与 FFmpeg 边界

数据库只保存 artifact root 下规范化的 POSIX 相对路径；拒绝绝对路径、`..`、NUL、符号链接逃逸和非白名单文件名。步骤在与最终文件同一文件系统的 attempt 临时目录写入，关闭并计算 SHA-256 后以原子 rename 放入 reservation 决定的不可变最终路径，再提交数据库终态。提交前崩溃可留下孤儿文件；重领时同路径同摘要复用，异摘要失败关闭，数据库绝不能引用临时、缺失或半写文件。孤儿清理由后续独立任务按 reservation 与保留期执行。

FFmpeg/ffprobe 只允许固定版本二进制、argv 调用和受控模板；禁止 shell、用户原始 filter/协议/路径，禁用网络输入，设置超时、并发与资源上限，限制 stderr 长度并映射为安全 `error_class`。日志不得包含正文、凭据或内部路径。输入输出均 probe，工具版本、argv 模板版本、字体/素材摘要写入 lineage。固定运行时内要求可复现字节；跨工具版本只承诺 profile 一致并保留实际 SHA。

### `short_video_9x16_v1` 媒体 profile

- 主视频：MP4、1080×1920、SAR 1:1、30 fps progressive、H.264 High、`yuv420p`，产品时长 15～180 秒。
- 音频：AAC-LC、48 kHz、stereo，集成响度目标 `-14 LUFS ±1`，true peak 不高于 `-1 dBTP`。
- 字幕：UTF-8 SRT sidecar 并烧录到主视频；cue 单调、不重叠且不越界。安全区为左右 90 px、顶部 180 px、底部 360 px；具体字体、字号、每行字数属于版本化 profile。
- 封面：1080×1920 sRGB JPEG；标题必须在同一安全区。渠道 validator 可收紧产品时长、文案和文件大小，不能静默改变母版。

### 自动质检与人工审核

QCReport 不可变并记录 profile/tool/ruleset 版本，结果为 `passed` 和稳定排序的 issues：`code`、`severity(blocker|warning|info)`、artifact ref、可选时间段、`message_key`、evidence refs。最低检查包括：文件可 probe/解码、流数量、profile/时长/fps/codec、A/V 漂移、响度/峰值、黑帧/静音、字幕时序/安全区/溢出、封面尺寸、事实 citation 完整性、素材 license 完整性以及重复/敏感风险。任一 blocker 令 `passed=false`；warning 必须展示并由审核者显式确认。

ReviewBundle 是不可变审核快照，钉住 project revision、profile、全部 artifact refs、QC、citation/source 摘要和 license；其 bundle hash 来自规范 manifest。后端仅在 bundle 完整、未 stale/invalidated、QC pass、证据和授权完整时返回 `can_approve=true`。approval/reject 必须携带 bundle hash 与 expected revision；批准记录钉住 ref 集合，后续 current pointer 变化使批准失效，不能漂移到新产物。拒绝保留 bundle/Artifact/pointer，并选择合法 rerun 起点生成新子图；不得产生无 Job 的 `generating`。

### 抖音与视频号发布包

`CHANNEL_PACKAGE` 由 `project_id + channel + approved bundle hash` 唯一标识并幂等导出。渠道为 `douyin | wechat_channels`，各自运行版本化 validator。包只消费已批准且未失效的 bundle，包含 `manifest.json`、`video.mp4`、`cover.jpg`、`captions.srt`、`description.txt`、`citations.json`、`licenses.json` 和 `checksums.sha256`；manifest 记录 schema/profile/channel、固定 refs/SHA/MIME、标题、描述、标签、citation/license，不含存储路径、Job payload、异常正文或 secret。归档拒绝符号链接、路径穿越和重复文件名。

没有已验证的官方发布权限时，M2 终点严格为受权下载的发布包和人工交付记录；项目保持 `approved`，不得伪装成 `published`，不得用浏览器模拟正式发布。以后只有独立安全任务可启用官方 Publisher adapter，并继续使用不可变 package ID 与 idempotency key。

媒体内容通过项目 ownership SQL 校验的受权路由提供，MIME 白名单、ETag=SHA，视频/音频支持 Range；API 生成 URL，客户端不得接触或拼接文件路径。

## 候选方案

- 每个步骤复制 FACT_CARD request/terminal：无法安全并行且产生提前审核状态，否决。
- 把所有镜头塞进 kind 级 current pointer：无法局部失效、恢复和审计，否决。
- 在数据库提交后直接写最终媒体：可能令 DB 指向半文件，否决。
- 无审核直接发布或浏览器模拟：权限、幂等与账号安全不可接受，否决。

## 影响

M2 必须先新增 PipelineRun/StepRun/logical key schema 与通用终态，再实现业务步骤；M1 FACT_CARD 接口可继续服务既有能力，但不得成为 M2 兼容旁路。文件终态不是单一 ACID 事务，因此接受可识别孤儿文件，并以 reservation、原子 rename、摘要复用和后续 GC 保证恢复。14 天若只验证导出包，只能宣称“发布包链路准出”，不能宣称平台自动发布稳定。
