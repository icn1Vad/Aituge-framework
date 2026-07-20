# Translation Service

独立翻译业务 Service，复用 Aituge-framework 的纯基模调用、Task、Event、
StageRun 和 Artifact 能力，但不进入 Proof、制度审校、ReAct Agent 或工具链。

## 边界

```text
ContiNew Java -> Translation Service -> Base Model
                                    -> BabelDOC Sidecar (PDF only)
```

- 只有 Java 可以调用本服务；所有业务接口要求 `X-Internal-Token`。
- Java 传递 `X-User-Id`、`X-Tenant-Id`、`X-Dept-Id` 和 `X-Request-Id`。
- 不接收 `X-Roles`，不判断角色权限。
- Task/Event 只保存哈希、字符数、语言、进度和安全化错误，不保存完整正文。
- 文本结果只在当前 HTTP 响应返回；历史任务明确返回 `content_retained=false`。
- 文件结果以 Framework Artifact 保存，供 Java 流式下载、校验并归档。
- Python 的文件完成态是 `OUTPUT_READY`；Java 归档成功后才能形成业务 `SUCCEEDED`。

## API

```http
POST /v1/translations/text
POST /v1/translations/files
GET  /v1/translations/tasks/{taskId}
GET  /v1/translations/tasks/{taskId}/artifacts
GET  /v1/translations/artifacts/{artifactId}/content
```

创建接口要求 `Idempotency-Key`。查询和下载按 Java 传入的用户及租户上下文检查归属。

统一错误响应：

```json
{
  "success": false,
  "error": {"code": "...", "message": "...", "retryable": false},
  "request_id": "..."
}
```

## 语言

业务码固定为：

```text
AUTO, ZH_CN, ZH_TW, EN, JA, KO, FR, DE, ES, RU, AR, PT
```

目标语言不能是 `AUTO`。源语言为 `AUTO` 时，本服务从有限样本进行结构化模型检测；
置信度不足、文本太短或 PDF 无足够可提取文本时返回
`SOURCE_LANGUAGE_UNDETERMINED`，不会把不确定的 `auto` 继续传给 BabelDOC。

## DOCX 样式策略

DOCX 以完整段落或完整表格单元格为逻辑翻译单元。一次模型调用可包含多个逻辑单元，
但绝不逐个 `w:t` 翻译。每个逻辑单元携带 Run、段落边界、制表符、换行、对象和域代码标记。

标记完整且顺序一致时，译文按 RunSpan 写回并保留原 Run 属性。标记映射失败时，针对整个
逻辑单元重新取得纯文本译文，放入第一个可翻译 Run，清空其余可翻译 Run；段落/单元格、
编号、图片和非文本节点保持不动，并记录 `DOCX_STYLE_DEGRADED` Event。

DOC 先由任务独占的 LibreOffice profile 转为 DOCX，完成 OpenXML 翻译后可转回 DOC。

## 重启与文件限制

- SQLite 和 Artifact 根目录位于 `/data` 持久卷。
- 启动时把遗留 `created/running` 翻译任务标记为 `interrupted`；不做断点恢复。
- 启动时清理 `/data/tmp`，已完成 Artifact 不删除。
- 默认上传上限 50 MiB、PDF 300 页、DOCX 解压 200 MiB/5000 条目/压缩比 100。
- 校验扩展名、声明 MIME 和 PDF/OLE/ZIP/OpenXML 真实内容。
- LibreOffice、单次模型、BabelDOC 和总任务均有独立超时。
- 默认文本并发 4、文件执行并发 2、文件待处理任务 16。

## 构建和验证

开发机不安装依赖、不执行测试。服务器独立 Demo 环境先构建项目已有
`ai-framework-core:base`，再以仓库根目录为 context 构建本目录 Dockerfile。

```bash
docker build -f services/translation/Dockerfile -t aituge-translation:demo .
```

公共 Compose、宿主机端口和 `ai-feature-demo-net` 由报告+翻译 integration 配置统一决定，
本模块不单独提交 Compose 端口映射。
