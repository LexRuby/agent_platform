# 智能写作能力 MCP 注册交付包

- 日期：2026-09-03

- 用途：直接提供给服务器端，将智能写作能力注册为 MCP 工具。代码已在服务器上，本包只做注册信息，不含实现。

- 组成：

  - `2026-09-03_MCP注册规格-智能写作.json` —— 机读注册包（主交付物）

  - 本文档 —— 注册说明与注意事项

- 背景资料：`2026-09-03_智能写作接口文档/`（14 页原始接口文档）、`2026-09-03_智能写作工具层设计.md`（聚合设计依据）

## 1. 注册包结构说明

JSON 顶层：

```
package / server_name / display_name / version   # MCP server 基本信息
request_envelope                                 # 后端统一请求信封
tools[]                                          # 9 个工具的 MCP 标准定义
```

每个 tool 含三个字段：

| 字段            | 性质                 | 说明                                                                                          |
| ------------- | ------------------ | ------------------------------------------------------------------------------------------- |
| `name`        | MCP 标准             | 工具名，Agent 调用时使用                                                                             |
| `description` | MCP 标准             | 中文描述，含"何时用/何时不用"，直接决定 Agent 选工具的准确率，注册时原样透传给 LLM                                            |
| `inputSchema` | MCP 标准             | JSON Schema 参数定义                                                                            |
| `x_backend`   | **路由扩展（非 MCP 标准）** | 后端路由表：`when`（路由条件）、`endpoint`（后端地址）、`method`（后端方法名）、`query_fields`（工具入参 → 后端 query 对象的字段映射） |

注册器读 `x_backend` 组装后端调用；若注册方案不支持自定义字段，可忽略它、按本文第 3 节的路由表手工配置。

## 2. 后端统一请求信封

所有后端接口共用同一信封：

```
POST <endpoint>            # 见各工具 x_backend
Content-Type: application/json

{
  "utterance": { "query": { ...由工具入参按 query_fields 组装... } },
  "method": "<后端方法名>",
  "source": "vr"
}
```

响应取 `response.data` 为业务数据；`patent_search` 另有 `response.cnt_dict`（字段统计）。

## 3. 路由总表（13 method → 9 工具）

| MCP 工具                 | 路由条件           | 后端 method                                | 后端地址                  |
| ---------------------- | -------------- | ---------------------------------------- | --------------------- |
| search\_patents        | mode=natural   | patent\_search                           | 140.210.4.206:30010   |
| search\_patents        | mode=boolean   | high\_patent\_search                     | 140.210.4.206:30010   |
| search\_by\_principle  | —              | extend\_patent\_search                   | 140.210.4.206:30010   |
| search\_journals       | —              | high\_journal\_search                    | 140.210.4.206:30010   |
| get\_patent\_details   | —              | get\_patent\_by\_ids                     | 116.204.102.229:30020 |
| detect\_word\_ontology | —              | detect\_word\_onto                       | 116.204.102.229:30020 |
| extract\_solutions     | —              | auto\_ai\_patent\_write\_solution        | 116.204.102.229:30020 |
| filter\_solutions      | action=filter  | auto\_ai\_patent\_write\_solution2result | 116.204.102.229:30020 |
| filter\_solutions      | action=cluster | auto\_ai\_patent\_write\_result\_cluster | 116.204.102.229:30020 |
| generate\_with\_prompt | —              | llm\_requests\_prompt\_temple            | 116.204.102.229:30020 |
| user\_results          | action=save    | user\_result\_save\_info                 | 116.204.102.229:30020 |
| user\_results          | action=list    | get\_user\_info\_names                   | 116.204.102.229:30020 |
| user\_results          | action=fetch   | get\_user\_info\_detail                  | 116.204.102.229:30020 |

地址规则（2026-09-03 用户确认）：**搜索类 4 个接口在 140.210.4.206:30010；其余全部在 116.204.102.229:30020。**

## 4. 注册注意事项

1. **description 必须原样注册**，不要压缩。字段里的"何时用/何时不用"负例（如"查期刊改用 search\_journals"）是防止 Agent 混选工具的关键。
2. **条件必填的校验**：`filter_solutions.query`（filter 时）、`user_results.name/time/info`（按 action）是 JSON Schema 表达不了的运行时校验，注册层或后端需兜底报错。
3. **错误透传**：后端报错原文返回给 Agent，便于其自行修正参数重试（如 user\_results 保存重名、prompt\_id 不存在）。
4. **超时设置**：`extract_solutions`、`filter_solutions` 是批量 LLM 并发任务，MCP 调用超时建议 ≥ 300s；其余常规即可。
5. **上下文体量**：`search_patents` size 默认 500、`get_patent_details` 返回全字段时，单次返回可能很大。若 MCP 层有会话状态能力，建议加结果缓存/引用机制（可选增强，不阻塞注册）。
6. **参数值类型**：`generate_with_prompt` 的模板参数值必须全部为 string（模板传参约束），schema 已用 `additionalProperties: {"type": "string"}` 表达，后端会再校验。
7. **数据衔接链**：`extract_solutions.patent_df` ← `search_patents`/`get_patent_details` 返回；`filter_solutions.method_df` ← `extract_solutions` 返回。字段不可裁剪，注册层如做结果转换务必保留原字段。

## 5. 未决事项（注册前需确认）

1. 意图分类接口（原 140.210.4.206:30011/consult）是否迁到 116.204.102.229:30020——用户待确认；该接口不在本注册包内（意图分类在平台入口层做）
2. prompt\_id 全量目录：当前 description 仅含已知 2 个（key\_words\_extend、tech\_background\_summary），需从 prompt\_factory 导出全量后更新 description 或以资源形式附加
3. 总览页提到的"任务分解接口"（fpc/fop 拆解）无独立 method 文档，未纳入注册包

