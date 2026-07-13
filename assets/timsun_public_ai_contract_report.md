# timsun.net 公开 AI URL 合同提取报告

## 结果

基于 2026-07-11 已保存的 timsun.net 公开 HTML 快照，逐条从实际 `href` 提取并去重后，共确认 **330 条** AI 公开路径：

| 页面族 | 已证实路径数 | 主要证据页 |
|---|---:|---|
| 产业链节点详情 | 45 | `/ai-industry/` 与关系图谱 |
| 公司档案 | 219 | `/ai-industry/market-map/` |
| 模型详情 | 12 | `/ai-industry/chain/model-evolution/` |
| Coding Agent 详情 | 11 | `/ai-industry/chain/model-evolution/` |
| AI 术语详情 | 32 | `/ai-industry/chain/glossary/` |
| AI 集合 / 导航页 | 11 | AI Hub 与各列表页 |

机器可读清单见 `assets/timsun_public_ai_contract.json`。每条记录只包含已经在公开 HTML 内实际出现的路径，并携带 `path`、`family`、`evidence_url`、`evidence_date`。

## 证据强度与边界

- 关系图谱快照页面自身显示“45 个节点 × 219 家公司”；从公开 HTML 链接独立去重得到的数量也正好是 45 和 219。
- 模型榜公开 HTML 中实际出现 12 个模型详情链接和 11 个 Coding Agent 详情链接。
- 术语列表公开 HTML 中实际出现 32 个术语详情链接。
- 同一轮低频页面审计曾确认 AI Hub、资本地图、关系图谱、产业链、模型榜、应用雷达、价值量拆解、术语库及 Nvidia、HBM、GPT-5.5 代表详情页返回 HTTP 200。
- 原站公开 sitemap 没有完整收录这些动态 AI 详情路由，因此本清单以公开页面的内部链接为合同证据，不把 sitemap 缺失解释为路由不存在。
- 原站 robots.txt 明确禁止 AI / 训练爬虫，并对一般爬虫设置 10 秒延迟。生成本清单时没有再次联网访问 timsun.net，也没有逐条请求 330 个详情页；只复用 2026-07-11 的既有公开页面快照。因此，`evidence_date` 表示观察到该链接的快照日期，不等同于每个详情页的最后更新时间或本次在线复验时间。
- 任务提示中的“39 个节点”与现有证据不一致：快照中的图谱统计和链接集合都证明是 **45 个节点**。本报告保留可证实的 45 条，不删减、不猜测。
