# 阶段接口

`POST /api/stages/<stage>` 接收 `multipart/form-data`。字段 `sample=dirty`、`sample=clean` 或 `file=<CSV>` 三选一。阶段为 `intake`、`prepare`、`calculate`、`policy`、`balance`。预览不入库。

`POST /api/runs` 接收同样的输入，执行全批并返回运行报告。`GET /api/runs` 返回最近 50 批。`GET /api/runs/<id>` 查询一批和当前复核状态。

`PATCH /api/runs/<id>` 接收 `revision`、`status` 和 `note`。状态使用 `APPROVED` 或 `HELD`。冲突返回 409，客户端读取新版本后重试。

`GET /api/runs/<id>/evidence` 下载内含运行清单、行号隔离、业务证据和复核信息的 JSON。

## 补录与版本

`POST /api/runs/<id>/supplements` 接收补录 CSV（`file`）、JSON `records` 或 `csv` 文本，并应携带同一笔补录稳定不变的 `idempotency_key`（也可用 `Idempotency-Key` 请求头）。原批次报告和来源不会被改写；接口返回 `original` 原签发快照与 `current` 补录后当前版本。

同一 `idempotency_key` 重试返回同一个当前版本和同一条签发/暂缓结果。中断后可调用 `POST /api/supplements/<supplement_id>/resume`（或 `recover`、`retry`）恢复；也可用相同键和相同内容再次 POST。

`GET /api/runs/<id>/versions` 回看原快照、版本链和补录记录；`GET`/`POST /api/runs/<id>/verify` 可从持久化来源重算并复验原快照可见行。等值剔除会减少当前可见行；位置剔除找不到目标文件或偏移时，当前版本为 `HELD`，并在 `report.marked_records`、`report.marks` 与分组 `marks` 中标出原始补录记录。工作台右侧提供“回看原快照 / 补录后当前结果”切换。
