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
