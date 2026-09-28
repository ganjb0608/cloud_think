你负责判断**分配给你的那一条提交记录**属于哪一类。

类别只能是以下四个之一：
- `breaking` — 不兼容变更，使用者必须改代码
- `feature` — 新增能力
- `fix` — 修复缺陷
- `internal` — 重构、测试、CI、文档等，不需要写进面向用户的说明

输出 JSON：
{"kind": "feature", "summary": "一句面向用户的描述", "user_facing": true}
