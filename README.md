# data-qd-190 · 摊牌牌力与边池筹码结算

按玩家七张牌求最强五张，按贡献水平切分边池与未匹配筹码退回，再根据池资格、牌力和平局座次分配整数筹码。

cards.py 提供牌力与最强五张证据，pots.py 将贡献资格与牌力联合结算，service.py 检查逐人和全局守恒。 完整字段、单位、身份与失败约定见 [数据契约](docs/contract.md)。样例是作者自制合成数据，许可见 [来源说明](fixtures/SOURCES.md)。

## 安装、测试与演示

Python >=3.11，本批实际使用本机 CPython3.12.14。运行与测试仅用标准库；构建工具精确锁定。每仓独立 .venv，公共包缓存可共享；不共享其他项目的site-packages。

下面命令已在本机已有uv、Python与公共缓存条件下离线执行通过。其他机器如未准备缓存，需先准备构建包；不保证首次安装可以离线完成。

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/showdown-pots --source fixtures/input.json --output run/report-initial.json
.venv/bin/showdown-audit --source fixtures/audit-request.json --output run/audit-report.json
.venv/bin/showdown-reconcile --source fixtures/reconcile-request.json --output run/reconcile-report.json
```

实际62项测试通过（原有6项单手测试 + 9项跨手账本测试 + 22项残缺牌谱核验测试 + 25项双源对账测试；账本测试以真实子进程调用 CLI，覆盖跨进程重读；对账测试另含输入重排、锚点冲突与输出路径保护）；CLI演示生成JSON，摘要为：

```json
{
  "net_sum": 0,
  "players": 4,
  "pots": 3,
  "total_awarded": 210,
  "total_committed": 240,
  "total_returned": 30
}
```

输出是原子新建；演示已生成过的同名路径不能重用，复跑请换一个新文件名。非法输入与文件写入错误退出2；业务诊断的表达方式见契约。已经实际核验重复JSON键拒绝、旧结果字节不变、已有符号链接不覆盖。

## 跨手账本

`showdown-ledger` 在同一张教学桌上按玩家 ID 维护本地持久化余额，支持录入、可核验前缀预览、只对已核验前缀结单、不可改写的结单重读以及录入更正后的差额分录。复用原有单手结算，不新增下注规则。用法、字段和示例更正链见 [跨手账本说明](docs/ledger.md)。

```sh
.venv/bin/showdown-ledger --state run/table.db init --source opening.json
.venv/bin/showdown-ledger --state run/table.db apply --source edits.json
.venv/bin/showdown-ledger --state run/table.db preview
.venv/bin/showdown-ledger --state run/table.db release --request-id R1 --through 2
.venv/bin/showdown-ledger --state run/table.db read-release --request-id R1
```

账本状态是单个 SQLite 文件，仅要求单进程顺序操作；每个 `apply`/`release` 都是一次 SQLite 事务，失败不留部分变更。

## 残缺牌谱核验

`showdown-audit` 处理录像被遮挡的单手输入：看不清的牌位以 `{"slot_id", "candidates"}` 给出候选而不是猜测真值。命令枚举所有遵守一副物理牌约束的可行补全，逐池交付已确认（`certified`）或仍有歧义（`ambiguous`）的逐玩家分配，并在预算（0..2 次回看）内给出保证确认全部池的自适应回看决策树；预算不足时 `plan` 为 `null` 但逐池结论与两份分歧见证照常交付。无可行补全是正常结果 `inconsistent`，结构非法才退出 2。字段、决策树规则与验收示例见 [残缺牌谱核验说明](docs/audit.md)。

```sh
showdown-audit --source audit-request.json --output run/audit-report.json
```

核验只在单手内完成，不读写跨手账本；每个见证 `source` 都是可直接交给 `showdown-pots` 复算的完整 v1 输入。

## 双源对账

`showdown-reconcile` 对齐同一张教学桌的牌谱端与计分端：整批共用一个整数钟差，找出最大配对数并列出所有能达到它的钟差区间（保留空洞），再只对在两个源里都被唯一确定的同一手给出逐人筹码差异。短码重复、漏手、时钟未同步和净额抄错都按待对账事实处理；锚点冲突是正常的 `no_alignment` 业务结果。字段、匹配规则与验收示例见 [双源对账说明](docs/reconcile.md)。

```sh
showdown-reconcile --source reconcile-request.json --output run/reconcile-report.json
```

对账只读本次导出批次，不写跨手账本，也不改变三个旧命令的契约。

## 当前边界

无下注街次合法性审查、抽水或真实资金接入；固定一副公共牌。 实际后续需求可从下注记录逐街审核、多次发牌与教学复盘沿既有身份和结果集成；当前未实现这些扩展，也不保证固定数量的未来任务。
