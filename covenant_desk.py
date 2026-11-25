"""跨村管护约定的经办服务。

把资料台分批到达的事实（表决、修订、撤权、巡查回执、合作方签收回执）
重放为管护约定的版次状态，提供：

* 发起约定：写清管护段、工作种类与承诺成本；
* 签认：各村按自己的表决门槛计票，共用设施条款须双边同意；
* 代签：代表只能在授权条款范围内、授权有效期内代签，撤权不追溯既往；
* 版次：修订产生新版，旧版即告失效；迟到文件只进对应版次，不复活旧版；
* 派责：仅生效条款可派交责任，同段同时段不得派两个互相替代的责任人；
* 接续：退出时已交付工作保留、未来工作可转接、无人接替者挂起；
* 查询：经办人查有效条款/未签齐共维事项/已派责任，住户查本人承诺，
  且任何村组都读不到另一村仅供内部核验的身份材料。

纯标准库实现，时间均为整数刻度，便于与资料台消息对齐。
"""

import argparse
import copy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

DATA = Path(__file__).resolve().parent / "data"
JOINT = "joint"


class CovenantError(Exception):
    """领域错误，携带与资料台风格一致的状态码字符串。"""

    def __init__(self, status, detail=""):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class ReceiptUnavailable(CovenantError):
    def __init__(self):
        super().__init__("temporarily_unavailable", "合作方资料暂不可用")


# ---------------------------------------------------------------------------
# 合作方回执来源
# ---------------------------------------------------------------------------


class LocalReceiptBook:
    """从 partner-queries.json 取回执，并复刻资料台“首次不可用”行为。"""

    def __init__(self, records):
        self._records = {}
        for record in records:
            key = (record["action"], record["desk"])
            if key in self._records:
                raise ValueError("合作方查询样例重复")
            self._records[key] = record
        self._failed = set()

    def __call__(self, action, desk):
        record = self._records.get((action, desk))
        if record is None:
            raise CovenantError("unknown_receipt", f"{action}@{desk}")
        key = (action, desk)
        if record.get("unavailable_first") and key not in self._failed:
            self._failed.add(key)
            raise ReceiptUnavailable()
        return copy.deepcopy(record["fact"])


class HttpReceiptBook:
    """经 HTTP 向运行中的资料台索取合作方回执。"""

    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def __call__(self, action, desk):
        url = f"{self.base_url}/partner?action={action}&desk={desk}"
        try:
            with urlopen(url, timeout=3) as response:  # noqa: S310 本地资料台
                payload = json.loads(response.read().decode("utf-8"))
        except OSError as exc:
            raise ReceiptUnavailable() from exc
        if getattr(response, "status", 200) == 503:
            raise ReceiptUnavailable()
        return payload["fact"]


# ---------------------------------------------------------------------------
# 经办服务引擎
# ---------------------------------------------------------------------------


class CovenantDesk:
    def __init__(self, register, messages, receipt_book=None, now=None):
        self.register = copy.deepcopy(register)
        self.parties = self.register["parties"]
        self.sections = self.register["sections"]
        self.receipt_book = receipt_book
        self.proxy_revoked_at = None
        self._terms = {}
        self._duties = {}
        self._ack_ledger = []
        self._notices = []
        self._imported = set()
        self._counter = 0

        for raw in self.register.get("terms", []):
            self._terms[raw["id"]] = self._new_term(raw)
        for message in sorted(messages, key=lambda item: item["sequence"]):
            self._ingest_message(message)

        times = [message["received_at"] for message in messages]
        self.now = now if now is not None else (max(times) if times else 0)

        for term_id, committed in self.register.get("committed", {}).items():
            duty_id = f"{term_id}:{committed['owner']}"
            self._duties[duty_id] = {
                "id": duty_id,
                "term": term_id,
                "owner": committed["owner"],
                "owners_history": [committed["owner"]],
                "slot": list(committed["slot"]),
                "status": "assigned",
                "partner_accepted": None,
                "history": [{"at": self.now, "event": "registered"}],
            }
        for ack in self._ack_ledger:
            self._apply_ack(ack)

    # -- 构造辅助 ----------------------------------------------------------

    @classmethod
    def from_folder(cls, folder, now=None, receipt_book=None, desk_url=None):
        folder = Path(folder)
        register = json.loads((folder / "register.json").read_text(encoding="utf-8"))
        messages = json.loads((folder / "messages.json").read_text(encoding="utf-8"))
        if receipt_book is None:
            if desk_url is not None:
                receipt_book = HttpReceiptBook(desk_url)
            else:
                records = json.loads(
                    (folder / "partner-queries.json").read_text(encoding="utf-8")
                )
                receipt_book = LocalReceiptBook(records)
        return cls(register, messages, receipt_book, now)

    def _new_term(self, raw):
        return {
            "id": raw["id"],
            "subject": raw["subject"],
            "work": raw["work"],
            "versions": [self._new_version(raw.get("version", 1), raw.get("shares"))],
        }

    def _new_version(self, number, shares):
        return {
            "version": number,
            "shares": copy.deepcopy(shares),
            "ballots": {
                party_id: {"principals": [], "signers": [], "invalid": []}
                for party_id in self.parties
            },
            "late_signatures": [],
            "superseded_at": None,
        }

    # -- 事实重放 ----------------------------------------------------------

    def _ingest_message(self, message):
        at = message["received_at"]
        source = message["source"]
        facts = message["facts"]
        kind = facts["kind"]
        if kind == "vote":
            self._ingest_vote(facts, at, source)
        elif kind == "amendment":
            self._ingest_amendment(facts, at)
        elif kind == "revoke_proxy":
            proxy = self.register.get("proxy")
            if proxy and proxy.get("rep") == facts.get("rep"):
                self.proxy_revoked_at = at
        elif kind == "ack":
            self._ack_ledger.append({"at": at, **facts})
        else:
            raise CovenantError("unknown_fact", kind)

    def _ingest_vote(self, facts, at, source, signed_at=None, via="message"):
        term = self._require_term(facts["term"])
        version = self._find_version(term, facts["version"])
        if source not in self.parties:
            raise CovenantError("unknown_party", source)
        bucket = version["ballots"][source]
        signed_at = at if signed_at is None else signed_at
        for signer in facts.get("members", []):
            principal, ok = self._resolve_signer(signer, source, term["id"], signed_at)
            if ok:
                if signer not in bucket["signers"]:
                    bucket["signers"].append(signer)
                if principal not in bucket["principals"]:
                    bucket["principals"].append(principal)
            elif signer not in bucket["invalid"]:
                bucket["invalid"].append(signer)
        return version

    def _ingest_amendment(self, facts, at):
        term = self._require_term(facts["term"])
        current = term["versions"][-1]
        expected = current["version"] + 1
        if facts["version"] != expected:
            raise CovenantError("version_skips", f"{term['id']} 应为版次 {expected}")
        current["superseded_at"] = at
        shares = facts.get("shares", current["shares"])
        term["versions"].append(self._new_version(expected, shares))

    def _resolve_signer(self, signer, party_id, term_id, at):
        """返回（被代表的本组成员, 是否有效）。"""
        members = self.parties[party_id]["members"]
        if signer in members:
            return signer, True
        proxy = self.register.get("proxy")
        if not proxy or signer != proxy.get("rep"):
            return None, False
        if term_id not in proxy.get("enabled_terms", []):
            return None, False
        if at > proxy["valid_to"]:
            return None, False
        if self.proxy_revoked_at is not None and at >= self.proxy_revoked_at:
            return None, False
        principal = proxy["for"]
        if principal not in members:
            return None, False
        return principal, True

    # -- 合作方回执：迟到签名进对应版次，但不复活失效版次 ------------------

    def pull_receipt(self, action, desk):
        if self.receipt_book is None:
            raise CovenantError("receipt_book_missing", action)
        fact = self.receipt_book(action, desk)  # 可能抛 ReceiptUnavailable
        key = (action, desk, fact.get("term"), fact.get("version"))
        if key in self._imported:
            return fact
        self._imported.add(key)
        if action == "read-signature":
            term = self._terms.get(fact["term"])
            if term is None:
                raise CovenantError("unknown_term", fact["term"])
            version = self._find_version(term, fact["version"])
            version["late_signatures"].append(
                {
                    "desk": desk,
                    "signed_at": fact["signed_at"],
                    "received_at": fact["received_at"],
                }
            )
        elif action == "read-duty":
            for duty in self._duties.values():
                if duty["term"] == fact["term"] and duty["owner"] == fact["shift"]:
                    duty["partner_accepted"] = fact["accepted"]
        return fact

    # -- 生效判断 ----------------------------------------------------------

    def _required_parties(self, subject):
        owner = self.sections[subject]
        return list(self.parties) if owner == JOINT else [owner]

    def _party_approved(self, version, party_id):
        threshold = self.parties[party_id]["vote"]
        return len(version["ballots"][party_id]["principals"]) >= threshold

    def _bucket_approved(self, party_id, bucket):
        return len(bucket["principals"]) >= self.parties[party_id]["vote"]

    def _version_reasons(self, term, version):
        """返回该版次不能生效的原因列表；空列表即生效。"""
        reasons = []
        if version["superseded_at"] is not None:
            reasons.append("superseded")
        for party_id in self._required_parties(term["subject"]):
            if not self._party_approved(version, party_id):
                reasons.append(f"missing_approval:{party_id}")
        for party_id, amount in (version["shares"] or {}).items():
            if amount > self.parties[party_id]["cap_cents"]:
                reasons.append(f"cap_exceeded:{party_id}")
        return reasons

    def _current_version(self, term):
        return term["versions"][-1]

    def _require_term(self, term_id):
        try:
            return self._terms[term_id]
        except KeyError:
            raise CovenantError("unknown_term", term_id) from None

    def _find_version(self, term, number):
        for version in term["versions"]:
            if version["version"] == number:
                return version
        raise CovenantError("bad_version", f"{term['id']} v{number}")

    # -- 发起约定 ----------------------------------------------------------

    def propose_term(self, term_id, subject, work, shares=None):
        if term_id in self._terms:
            raise CovenantError("term_exists", term_id)
        if subject not in self.sections:
            raise CovenantError("unknown_subject", subject)
        if self.sections[subject] == JOINT:
            if not shares or set(shares) != set(self.parties):
                raise CovenantError("bad_shares", "共用条款须写清两边分担成本")
        elif shares:
            for party_id in shares:
                if party_id not in self.parties:
                    raise CovenantError("bad_shares", party_id)
        term = self._new_term(
            {"id": term_id, "subject": subject, "work": work, "shares": shares}
        )
        self._terms[term_id] = term
        return self.term_view(term_id)

    # -- 责任派交 ----------------------------------------------------------

    @staticmethod
    def _slots_overlap(one, other):
        return one[0] < other[1] and other[0] < one[1]

    def assign_duty(self, term_id, owner, slot):
        term = self._require_term(term_id)
        version = self._current_version(term)
        reasons = self._version_reasons(term, version)
        if reasons:
            raise CovenantError("term_not_effective", f"{term_id}: {','.join(reasons)}")
        if len(slot) != 2 or not all(isinstance(v, int) for v in slot) or slot[0] >= slot[1]:
            raise CovenantError("bad_slot", str(slot))
        subject = term["subject"]
        for duty in self._duties.values():
            if duty["status"] == "vacant":
                continue
            other_subject = self._terms[duty["term"]]["subject"]
            if (
                other_subject == subject
                and duty["owner"] != owner
                and self._slots_overlap(duty["slot"], slot)
            ):
                raise CovenantError(
                    "duty_conflict",
                    f"{subject} {slot} 已由 {duty['owner']} 承担 {duty['slot']}",
                )
        self._counter += 1
        duty = {
            "id": f"D{self._counter}",
            "term": term_id,
            "owner": owner,
            "owners_history": [owner],
            "slot": list(slot),
            "status": "assigned",
            "partner_accepted": None,
            "history": [{"at": self.now, "event": "assigned", "owner": owner}],
        }
        self._duties[duty["id"]] = duty
        return self._duty_view(duty)

    def _apply_ack(self, ack):
        for duty in self._duties.values():
            if duty["term"] == ack["term"] and duty["owner"] == ack.get("shift"):
                if ack.get("performed"):
                    duty["status"] = "delivered"
                    duty["history"].append({"at": ack["at"], "event": "performed"})

    def withdraw(self, owner, term_id=None):
        """当事人退出：已交付保留、未到时段可转接、过期未交付锁定。"""
        summary = {"delivered": [], "awaiting_takeover": [], "locked": []}
        for duty in self._duties.values():
            if owner not in duty["owners_history"] or duty["owner"] != owner:
                continue
            if term_id is not None and duty["term"] != term_id:
                continue
            if duty["status"] == "delivered":
                summary["delivered"].append(duty["id"])
                continue
            if duty["slot"][1] <= self.now:
                summary["locked"].append(duty["id"])
                continue
            duty["status"] = "vacant"
            duty["history"].append({"at": self.now, "event": "withdrawn", "owner": owner})
            summary["awaiting_takeover"].append(duty["id"])
        return summary

    def reassign(self, duty_id, new_owner):
        duty = self._duties.get(duty_id)
        if duty is None:
            raise CovenantError("unknown_duty", duty_id)
        if duty["status"] != "vacant":
            raise CovenantError("duty_not_vacant", duty_id)
        slot = duty["slot"]
        subject = self._terms[duty["term"]]["subject"]
        for other in self._duties.values():
            if other["id"] == duty_id or other["status"] == "vacant":
                continue
            if (
                self._terms[other["term"]]["subject"] == subject
                and other["owner"] != new_owner
                and self._slots_overlap(other["slot"], slot)
            ):
                raise CovenantError("duty_conflict", new_owner)
        duty["owner"] = new_owner
        duty["owners_history"].append(new_owner)
        duty["status"] = "assigned"
        duty["history"].append(
            {"at": self.now, "event": "reassigned", "owner": new_owner}
        )
        return self._duty_view(duty)

    # -- 通知代发 ----------------------------------------------------------

    def send_notice(self, recipient, text, term_id=None, by="operator"):
        if recipient != "all" and recipient not in self.parties:
            raise CovenantError("unknown_recipient", recipient)
        if term_id is not None:
            self._require_term(term_id)
        self._counter += 1
        notice = {
            "id": f"N{self._counter}",
            "at": self.now,
            "by": by,
            "recipient": recipient,
            "term": term_id,
            "text": text,
        }
        self._notices.append(notice)
        return copy.deepcopy(notice)

    # -- 查询视图（带身份屏蔽）--------------------------------------------

    def _ballot_view(self, party_id, bucket, viewer):
        view = {
            "voter_count": len(bucket["principals"]),
            "threshold": self.parties[party_id]["vote"],
            "approved": self._bucket_approved(party_id, bucket),
            "invalid_count": len(bucket["invalid"]),
        }
        if viewer == party_id:
            view.update(
                signers=list(bucket["signers"]),
                principals=list(bucket["principals"]),
                invalid=list(bucket["invalid"]),
            )
        return view

    def term_view(self, term_id, viewer=None):
        term = self._require_term(term_id)
        versions = []
        for version in term["versions"]:
            reasons = self._version_reasons(term, version)
            versions.append(
                {
                    "version": version["version"],
                    "shares": copy.deepcopy(version["shares"]),
                    "superseded_at": version["superseded_at"],
                    "effective": not reasons,
                    "reasons": reasons,
                    "approvals": {
                        party_id: self._ballot_view(party_id, bucket, viewer)
                        for party_id, bucket in version["ballots"].items()
                    },
                    "late_signatures": copy.deepcopy(version["late_signatures"]),
                }
            )
        current = self._current_version(term)
        return {
            "id": term["id"],
            "subject": term["subject"],
            "scope": self.sections[term["subject"]],
            "work": term["work"],
            "required_parties": self._required_parties(term["subject"]),
            "current_version": current["version"],
            "effective": not self._version_reasons(term, current),
            "versions": versions,
        }

    def terms_view(self, viewer=None):
        return [self.term_view(term_id, viewer) for term_id in self._terms]

    def effective_terms(self, viewer=None):
        return [view for view in self.terms_view(viewer) if view["effective"]]

    def pending_joint(self, viewer=None):
        """共维事项中未签齐（或被上限挡住）的当前版次。"""
        pending = []
        for term_id, term in self._terms.items():
            if self.sections[term["subject"]] != JOINT:
                continue
            view = self.term_view(term_id, viewer)
            current = next(
                version
                for version in view["versions"]
                if version["version"] == view["current_version"]
            )
            if not current["effective"]:
                pending.append(
                    {
                        "term": term_id,
                        "version": current["version"],
                        "reasons": current["reasons"],
                        "missing_parties": [
                            party_id
                            for party_id in view["required_parties"]
                            if not current["approvals"][party_id]["approved"]
                        ],
                    }
                )
        return pending

    def _duty_view(self, duty):
        return {
            "id": duty["id"],
            "term": duty["term"],
            "owner": duty["owner"],
            "slot": list(duty["slot"]),
            "status": duty["status"],
            "partner_accepted": duty["partner_accepted"],
            "history": copy.deepcopy(duty["history"]),
        }

    def duties(self, viewer=None):
        return [self._duty_view(duty) for duty in self._duties.values()]

    def household_view(self, household):
        """住户只看与自己有关的承诺：已作、可变更、等待接手。"""
        view = {"performed": [], "changeable": [], "awaiting_takeover": [], "signatures": []}
        for duty in self._duties.values():
            involved = household in duty["owners_history"]
            if not involved:
                continue
            item = self._duty_view(duty)
            if duty["status"] == "delivered":
                view["performed"].append(item)
            elif duty["status"] == "vacant":
                view["awaiting_takeover"].append(item)
            elif duty["owner"] == household and duty["slot"][0] > self.now:
                view["changeable"].append(item)
        for term in self._terms.values():
            for version in term["versions"]:
                for party_id, bucket in version["ballots"].items():
                    if household in bucket["signers"] or household in bucket["principals"]:
                        view["signatures"].append(
                            {
                                "term": term["id"],
                                "version": version["version"],
                                "superseded": version["superseded_at"] is not None,
                            }
                        )
        return view

    def parties_view(self, viewer=None):
        """村组名册与代理身份仅本村可见，对外只暴露规则与计数。"""
        result = {}
        for party_id, party in self.parties.items():
            entry = {
                "vote": party["vote"],
                "cap_cents": party["cap_cents"],
            }
            if viewer == party_id:
                entry["members"] = list(party["members"])
            else:
                entry["members"] = f"redacted:{party_id}"
            result[party_id] = entry
        proxy = self.register.get("proxy")
        if proxy:
            proxy_view = {
                "rep": proxy["rep"],
                "enabled_terms": list(proxy["enabled_terms"]),
                "valid_to": proxy["valid_to"],
                "revoked_at": self.proxy_revoked_at,
            }
            owner_party = next(
                (
                    party_id
                    for party_id, party in self.parties.items()
                    if proxy["for"] in party["members"]
                ),
                None,
            )
            if viewer == owner_party:
                proxy_view["for"] = proxy["for"]
            else:
                proxy_view["for"] = f"redacted:{owner_party}" if owner_party else "redacted"
            result["proxy"] = proxy_view
        return result

    def notices(self):
        return copy.deepcopy(self._notices)


# ---------------------------------------------------------------------------
# HTTP 入口
# ---------------------------------------------------------------------------

HTTP_STATUS = {
    "unknown_term": 404,
    "unknown_subject": 404,
    "unknown_party": 404,
    "unknown_receipt": 404,
    "unknown_duty": 404,
    "unknown_recipient": 404,
    "term_exists": 409,
    "term_not_effective": 409,
    "duty_conflict": 409,
    "duty_not_vacant": 409,
    "version_skips": 409,
    "temporarily_unavailable": 503,
}


class CovenantHTTPServer(ThreadingHTTPServer):
    def __init__(self, port, desk):
        self.desk = desk
        self.lock = Lock()
        super().__init__(("127.0.0.1", port), CovenantHandler)


class CovenantHandler(BaseHTTPRequestHandler):
    def _respond(self, status, data):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _viewer(self, query):
        viewer = query.get("viewer", [None])[0]
        return viewer or None

    def _error(self, exc):
        self._respond(
            HTTP_STATUS.get(exc.status, 400),
            {"status": exc.status, "detail": exc.detail},
        )

    def do_GET(self):
        route = urlsplit(self.path)
        query = parse_qs(route.query, keep_blank_values=True)
        desk = self.server.desk
        try:
            with self.server.lock:
                if route.path == "/terms":
                    self._respond(200, {"terms": desk.terms_view(self._viewer(query))})
                elif route.path == "/terms/effective":
                    self._respond(200, {"terms": desk.effective_terms(self._viewer(query))})
                elif route.path == "/joint/pending":
                    self._respond(200, {"items": desk.pending_joint(self._viewer(query))})
                elif route.path == "/duties":
                    self._respond(200, {"duties": desk.duties(self._viewer(query))})
                elif route.path == "/notices":
                    self._respond(200, {"notices": desk.notices()})
                elif route.path == "/parties":
                    self._respond(200, desk.parties_view(self._viewer(query)))
                elif route.path.startswith("/households/"):
                    household = route.path.rsplit("/", 1)[1]
                    self._respond(200, desk.household_view(household))
                else:
                    self._respond(404, {"status": "not_found"})
        except CovenantError as exc:
            self._error(exc)

    def do_POST(self):
        route = urlsplit(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._respond(400, {"status": "bad_body"})
            return
        desk = self.server.desk
        try:
            with self.server.lock:
                if route.path == "/terms":
                    self._respond(
                        201,
                        desk.propose_term(
                            body["term_id"],
                            body["subject"],
                            body["work"],
                            body.get("shares"),
                        ),
                    )
                elif route.path == "/duties":
                    self._respond(
                        201,
                        desk.assign_duty(body["term"], body["owner"], body["slot"]),
                    )
                elif route.path.endswith("/reassign"):
                    duty_id = route.path.split("/")[2]
                    self._respond(200, desk.reassign(duty_id, body["owner"]))
                elif route.path == "/withdrawals":
                    self._respond(
                        200,
                        desk.withdraw(body["owner"], body.get("term")),
                    )
                elif route.path == "/notices":
                    self._respond(
                        201,
                        desk.send_notice(
                            body["recipient"],
                            body["text"],
                            term_id=body.get("term"),
                            by=body.get("by", "operator"),
                        ),
                    )
                elif route.path == "/receipts":
                    self._respond(
                        200,
                        {"fact": desk.pull_receipt(body["action"], body["desk"])},
                    )
                else:
                    self._respond(404, {"status": "not_found"})
        except CovenantError as exc:
            self._error(exc)

    def log_message(self, *args):
        return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--desk", default=None, help="资料台地址，如 http://127.0.0.1:8765")
    args = parser.parse_args()
    desk = CovenantDesk.from_folder(args.data, desk_url=args.desk)
    with CovenantHTTPServer(args.port, desk) as server:
        print(f"经办服务 http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
