# 站点配置与采集检查

## 目录

- 配置字段
- 首次适配
- 每页检查
- 终止条件
- xpcle.com 历史提示

## 配置字段

为每个站点建立任务级配置，不要把未经验证的选择器写回通用脚本：

```json
{
  "approvedOrigin": "https://example.com",
  "allowedPathPrefix": "/question-bank/",
  "questionContainer": ".question-list",
  "questionItem": ".question-item",
  "stem": ".stem",
  "options": ".option",
  "answer": ".answer",
  "knowledge": ".knowledge",
  "analysis": ".analysis",
  "submitButton": "button.test_btn",
  "submitLabels": ["提交", "查看答案"],
  "paginationContainer": ".pagination",
  "nextLink": "a.next",
  "pageParam": "page"
}
```

`approvedOrigin` 和 `allowedPathPrefix` 必须来自用户批准的起始 URL，其余选择器必须通过只读 DOM 检查获得。

## 首次适配

1. 只读列出候选题目容器、题目数、按钮文字和下一页目标。
2. 确认选择器只覆盖题库，不包含账户、订单、通知或导航区域。
3. 展示一个脱敏后的题目样例和将要点击的控件数量。
4. 获得用户确认后才允许提交答案和翻页。
5. 保存本次任务配置；不要假设它适用于网站的其他版本。

## 每页检查

- 来源和路径仍在允许范围。
- 题目容器唯一且可见。
- 题目数处于配置允许范围，不使用全站固定常量。
- 待点击控件都位于题目项内且文字、类型符合配置。
- 抓取结果只包含结构化题目字段。
- 下一页 URL 经解析后同源、路径合法且页码递增。

## 终止条件

以下任一情况立即停止并交付部分结果：来源或路径变化、容器缺失或多义、页码不递增、URL/页码/内容哈希重复、连续失败达到 3 次、达到任一用户批准预算、页面要求执行额外登录或敏感操作。

## xpcle.com 历史提示

旧页面曾使用 `.test_btn` 和含“下页”的链接，每页曾常见 12 题。这些仅是历史线索，不是可信配置。每次运行都必须重新验证容器、控件语义、下一页目标和实际题数。

