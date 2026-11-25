# 跨村共管林带的议案签认与接续

各村表决门槛及支出上限各自记录。独享段和共用点使用不同条款编号。代理授权按条款列适用范围，签名时间与采集时间可能不同。

本地资料台提供合成台账、分批报到的事实以及合作方的独立回执。台账为可供查询的登记记录，回执注明的是合作方所知的实际情况。

## 联调入口

使用 Python 3.11.2 标准库。资料台监听本地地址。

```sh
python3 -m unittest -v
python3 evidence_desk.py --port 8765
```

`GET /register` 读取 `data/register.json`。 `GET /messages?after=0&size=30` 从 `data/messages.json` 返回一页到达记录。消息含 `sequence`、`received_at`、`source` 和 `facts`。用返回的 `cursor` 读取下一页，游标顺序是接收顺序。

`GET /partner?action=操作&desk=合作方` 查询 `data/partner-queries.json` 中对应的 `fact`。配置 `unavailable_first` 的查询第一次返回 503，此次查询未能取得回执，同一查询后续可取得已记录的事实。未知查询返回 404。可以用 `--data` 指向另一组同名文件。

## 经办服务（covenant_desk）

在资料台之上重放事实，维护管护约定的版次与签认状态：

```sh
python3 covenant_desk.py --port 8766                 # 回执取本地 data
python3 covenant_desk.py --port 8766 --desk http://127.0.0.1:8765
```

### 规则要点

* 独享段只按管护村自己的门槛计票；共用蓄水点条款须双边均达各自门槛，且任一村分担额超过其 `cap_cents` 即不能生效。
* 修订产生新版，旧版立即标记失效；迟到的表决/签名只登记到对应版次，不复活失效版次，旧版同意不带入新版（改共同项目或超上限须重新判断）。
* 代签只在授权条款范围内、`valid_to` 之内有效；撤权不追溯，撤权前的有效签名、已交付工作和已派责任均保留。
* 仅生效条款可派责任；同一管护段重叠时段派给不同责任人返回 `duty_conflict`（同段同时段不得有两位互相替代的责任人）。
* 退出按已交付（保留）、未来时段（转 vacant 等待接手，可 `reassign`）、过期未交付（锁定不可转接）分类。
* 查询按 `viewer` 屏蔽：另一村的成员名册、代理委托人、表决签名明细只给计数，住户经 `/households/{id}` 只能看到本人的承诺。

### 接口

GET（可选 `?viewer=north|south`）：

* `/terms`、`/terms/effective`：各版约定与当前生效条款
* `/joint/pending`：未签齐或被上限挡住的共维事项
* `/duties`：已派交责任及状态（assigned / delivered / vacant）
* `/households/{住户}`：本人已作、仍可变更、等待接手的承诺
* `/parties`：各村门槛与上限（身份材料对他村脱敏）
* `/notices`：经办人代发通知的登记

POST（JSON）：

* `/terms`：发起约定（`term_id, subject, work, shares`，共用条款必须写清两边成本）
* `/duties`：派交责任（`term, owner, slot`）；`/duties/{id}/reassign` 接手空位
* `/withdrawals`：当事人退出（`owner, term?`），返回三类工作清单
* `/notices`：代发通知（`recipient, text, term?, by?`）
* `/receipts`：向合作方资料台拉取回执（`action, desk`），迟到签名进对应版次，首次 503 不写账
