# 智能写作技能

## 何时使用

用户要围绕专利做技术方案分析与交底书写作时使用本技能：

- 识别术语本体：功能/对象（fpc/fop）→ `detect_word_ontology`
- 从专利集合抽取技术方案 → `extract_solutions`
- 方案筛选与聚类（收敛到值得写的方案）→ `filter_solutions`
- 模板化 LLM 生成（按 prompt_id 生成交底书草稿等）→ `generate_with_prompt`
- 用户结果库（保存/列出/查看中间结果）→ `user_results`

## 何时不使用

- 用户只是要找专利/论文 → 属于「专利检索」技能
- 普通闲聊/通用问答 → 不用本技能工具

## 工具清单（MCP：smart-writing）

| 工具 | 用途 |
| ---- | ---- |
| `detect_word_ontology` | 术语本体识别：返回 fpc（功能）/ fop（对象）本体树供用户勾选确认 |
| `extract_solutions` | 从勾选的专利批量抽取技术方案（上百并发 LLM 调用，异步批次） |
| `filter_solutions` | 抽取结果筛选 + 聚类：solution2result + result_cluster 合并 |
| `generate_with_prompt` | 通用 LLM 模板生成底座：一个工具 + prompt_id 目录 |
| `user_results` | 用户结果库三件套：保存/列出名字/取详情（action 参数区分） |

## 典型流程（交底书写作主线）

1. **本体确认**：`detect_word_ontology`（用户的技术描述）→ 本体树（fpc/fop）→ **必须等用户勾选确认**，不能自动全选。
2. **方案抽取**：用户确认本体 + 专利集合后，`extract_solutions`。此步是重调用（上百并发 LLM），参数务必来自用户已确认的输入；失败可整批重试。
3. **方案筛选**：`filter_solutions` → 聚类后的方案列表给用户挑选。**人审节点**：写作主线上的每一步有人工确认点，工具边界就卡在确认点上，不要黑箱跑全程。
4. **草稿生成**：`generate_with_prompt`（prompt_id=交底书模板，方案=用户勾选的方案集合）。
5. **结果归档**：`user_results`（action=save）保存中间/最终结果，供后续会话引用。

## 注意事项

- `extract_solutions` 很贵：调用前与用户确认专利集合与本体选择，避免整批返工。
- 用户确认点（本体勾选、专利勾选、方案挑选）必须显式停下来等用户，不要代替用户选择。
- `user_results` 的 ID 引用：中间结果以 ID 传递，先 list 再 get 详情，防上下文爆炸。
- 模板生成超时容忍要高（LLM 长文本），等待时向用户说明正在生成。
