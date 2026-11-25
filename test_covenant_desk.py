import copy
from http.client import HTTPConnection
import json
from threading import Thread
import unittest

from covenant_desk import (
    CovenantDesk,
    CovenantError,
    CovenantHTTPServer,
    LocalReceiptBook,
)
import covenant_desk as module


BASE = {
    "parties": {
        "north": {"vote": 2, "cap_cents": 35000, "members": ["n1", "n2", "n3"]},
        "south": {"vote": 2, "cap_cents": 30000, "members": ["s1", "s2", "member-s3"]},
    },
    "sections": {"S-a": "north", "S-b": "south", "pond": "joint"},
    "terms": [
        {"id": "T1", "subject": "S-a", "work": "inspect", "version": 1},
        {
            "id": "T2",
            "subject": "pond",
            "work": "maintain",
            "version": 1,
            "shares": {"north": 18000, "south": 15000},
        },
    ],
    "proxy": {
        "rep": "agent-d",
        "for": "member-s3",
        "enabled_terms": ["T2"],
        "valid_to": 125,
    },
    "committed": {"T1": {"owner": "shift-a", "slot": [140, 160]}},
}

MESSAGES = [
    {"sequence": 1, "received_at": 90, "source": "north",
     "facts": {"kind": "vote", "term": "T1", "version": 1, "members": ["n1", "n2"]}},
    {"sequence": 2, "received_at": 100, "source": "south",
     "facts": {"kind": "vote", "term": "T2", "version": 1, "members": ["s1", "agent-d"]}},
    {"sequence": 3, "received_at": 115, "source": "north",
     "facts": {"kind": "amendment", "term": "T2", "version": 2,
               "shares": {"north": 16000, "south": 32000}}},
    {"sequence": 4, "received_at": 123, "source": "south",
     "facts": {"kind": "revoke_proxy", "rep": "agent-d"}},
    {"sequence": 5, "received_at": 148, "source": "patrol",
     "facts": {"kind": "ack", "term": "T1", "shift": "shift-a", "performed": True}},
]


def make_desk(register=None, messages=None, now=None, receipts=None):
    register = copy.deepcopy(BASE if register is None else register)
    messages = copy.deepcopy(MESSAGES if messages is None else messages)
    book = LocalReceiptBook(receipts or [])
    return CovenantDesk(register, messages, book, now=now)


def vote(seq, at, source, term, version, members):
    return {
        "sequence": seq,
        "received_at": at,
        "source": source,
        "facts": {"kind": "vote", "term": term, "version": version, "members": members},
    }


class ReplayFixtureTest(unittest.TestCase):
    def setUp(self):
        self.desk = make_desk()

    def _version(self, term_id, number):
        term = self.desk._require_term(term_id)
        return self.desk._find_version(term, number)

    def test_exclusive_term_approved_by_own_vote_rule_only(self):
        view = self.desk.term_view("T1", viewer="north")
        self.assertTrue(view["effective"])
        self.assertEqual(view["scope"], "north")
        self.assertEqual(view["required_parties"], ["north"])
        self.assertEqual(view["versions"][0]["approvals"]["north"]["voter_count"], 2)
        self.assertEqual(view["versions"][0]["approvals"]["north"]["threshold"], 2)
        # 另一村的内部名册对北村经办人不应出现
        self.assertTrue(
            str(self.desk.parties_view(viewer="north")["south"]["members"]).startswith("redacted")
        )

    def test_joint_term_needs_both_sides_and_cap(self):
        view = self.desk.term_view("T2")
        self.assertFalse(view["effective"])
        self.assertEqual(set(view["required_parties"]), {"north", "south"})
        v2 = next(v for v in view["versions"] if v["version"] == 2)
        self.assertIn("missing_approval:north", v2["reasons"])
        self.assertIn("missing_approval:south", v2["reasons"])
        self.assertIn("cap_exceeded:south", v2["reasons"])
        pending = self.desk.pending_joint()
        self.assertEqual([item["term"] for item in pending], ["T2"])
        self.assertEqual(set(pending[0]["missing_parties"]), {"north", "south"})

    def test_amendment_supersedes_and_late_vote_cannot_revive(self):
        v1 = self._version("T2", 1)
        self.assertIsNotNone(v1["superseded_at"])
        # v1 曾有南村有效代签：s1 + 被代表的 member-s3
        self.assertEqual(set(v1["ballots"]["south"]["principals"]), {"s1", "member-s3"})
        # 迟到的北村表决只进对应版次，不复活已经失效的 v1
        desk = make_desk(messages=MESSAGES + [
            vote(6, 150, "north", "T2", 1, ["n1", "n2"]),
        ])
        term = desk.term_view("T2")
        v1_view = next(v for v in term["versions"] if v["version"] == 1)
        self.assertIn("superseded", v1_view["reasons"])
        self.assertEqual(v1_view["approvals"]["north"]["voter_count"], 2)
        self.assertFalse(term["effective"])
        self.assertEqual(desk.pending_joint()[0]["version"], 2)

    def test_proxy_revocation_is_not_retroactive(self):
        v1 = self._version("T2", 1)
        # 撤权在 123，代签发生在 100：既往签名仍然有效
        self.assertNotIn("agent-d", v1["ballots"]["south"]["invalid"])
        self.assertEqual(self.desk.proxy_revoked_at, 123)

    def test_proxy_must_stay_within_scope_and_validity(self):
        # 撤权之后代签 → 无效
        desk = make_desk(messages=MESSAGES + [
            vote(6, 130, "south", "T2", 2, ["agent-d"]),
        ])
        v2 = desk._find_version(desk._require_term("T2"), 2)
        self.assertIn("agent-d", v2["ballots"]["south"]["invalid"])
        self.assertEqual(len(v2["ballots"]["south"]["principals"]), 0)

        # 授权范围之外的条款 → 无效
        desk = make_desk(messages=MESSAGES + [
            vote(6, 95, "south", "T1", 1, ["agent-d"]),
        ])
        t1 = desk._find_version(desk._require_term("T1"), 1)
        self.assertIn("agent-d", t1["ballots"]["south"]["invalid"])

        # 超过授权有效期 → 无效
        desk = make_desk(messages=MESSAGES + [
            vote(6, 126, "south", "T2", 2, ["agent-d"]),
        ])
        v2 = desk._find_version(desk._require_term("T2"), 2)
        self.assertIn("agent-d", v2["ballots"]["south"]["invalid"])

    def test_revocation_keeps_done_work_and_issued_duties(self):
        desk = make_desk(messages=MESSAGES + [
            {"sequence": 6, "received_at": 124, "source": "south",
             "facts": {"kind": "revoke_proxy", "rep": "agent-d"}},
        ])
        v1 = desk._find_version(desk._require_term("T2"), 1)
        self.assertEqual(set(v1["ballots"]["south"]["principals"]), {"s1", "member-s3"})
        duty = next(d for d in desk.duties() if d["term"] == "T1")
        self.assertEqual(duty["status"], "delivered")

    def test_ack_marks_registered_duty_delivered(self):
        duty = next(d for d in self.desk.duties() if d["id"] == "T1:shift-a")
        self.assertEqual(duty["status"], "delivered")
        self.assertEqual(duty["slot"], [140, 160])

    def test_effective_terms_only(self):
        self.assertEqual([t["id"] for t in self.desk.effective_terms()], ["T1"])


class DutyAndHandoverTest(unittest.TestCase):
    def setUp(self):
        self.desk = make_desk(now=200)

    def _activate_pond(self):
        """补一版在上限内、双边达标的共维条款。"""
        desk = self.desk
        desk.register["terms"].append({
            "id": "T3",
            "subject": "pond",
            "work": "maintain",
            "version": 1,
            "shares": {"north": 10000, "south": 10000},
        })
        desk._terms["T3"] = desk._new_term(desk.register["terms"][-1])
        v1 = desk._find_version(desk._require_term("T3"), 1)
        v1["ballots"]["north"]["principals"] = ["n1", "n2"]
        v1["ballots"]["north"]["signers"] = ["n1", "n2"]
        v1["ballots"]["south"]["principals"] = ["s1", "s2"]
        v1["ballots"]["south"]["signers"] = ["s1", "s2"]
        return desk

    def test_duty_only_for_effective_term(self):
        with self.assertRaises(CovenantError) as caught:
            self.desk.assign_duty("T2", "x", [210, 220])
        self.assertEqual(caught.exception.status, "term_not_effective")

    def test_conflicting_alternates_on_same_slot_rejected(self):
        desk = self._activate_pond()
        first = desk.assign_duty("T3", "shift-b", [220, 240])
        self.assertEqual(first["status"], "assigned")
        with self.assertRaises(CovenantError) as caught:
            desk.assign_duty("T3", "shift-c", [230, 250])
        self.assertEqual(caught.exception.status, "duty_conflict")
        # 不同时段可以派
        desk.assign_duty("T3", "shift-c", [240, 250])
        # 同一责任人同段不构成冲突（与 shift-c 相邻不重叠）
        desk.assign_duty("T3", "shift-b", [250, 260])

    def test_exclusive_section_rotation_change_keeps_joint_confirmation(self):
        desk = self._activate_pond()
        before = desk.term_view("T3")
        self.assertTrue(before["effective"])
        # 只动甲村独享段 T1 的轮值（已交付的另派未来轮值）
        new_shift = desk.assign_duty("T1", "shift-n2", [300, 320])
        self.assertEqual(new_shift["term"], "T1")
        # 共用蓄水点 T3 的确认不受影响；未签齐列表里只有原有 T2，不含 T3
        self.assertTrue(desk.term_view("T3")["effective"])
        self.assertEqual([i["term"] for i in desk.pending_joint()], ["T2"])

    def test_cap_change_forces_re_judgment(self):
        desk = make_desk(messages=MESSAGES + [
            {"sequence": 6, "received_at": 160, "source": "north",
             "facts": {"kind": "amendment", "term": "T2", "version": 3,
                       "shares": {"north": 16000, "south": 16000}}},
        ], now=200)
        term = desk.term_view("T2")
        self.assertEqual(term["current_version"], 3)
        v3 = next(v for v in term["versions"] if v["version"] == 3)
        # 上限内，但旧版同意不带入新版：两边都要重新判断
        self.assertEqual(
            set(v3["reasons"]),
            {"missing_approval:north", "missing_approval:south"},
        )

    def test_withdraw_splits_delivered_transferable_and_unstaffed(self):
        desk = self._activate_pond()
        desk.assign_duty("T3", "h1", [210, 230])          # 未来，可转接
        desk.assign_duty("T3", "h1", [150, 170])          # 已过期但无交付回执
        delivered = desk.assign_duty("T3", "h1", [180, 190])
        # 模拟已交付
        for duty in desk._duties.values():
            if duty["id"] == delivered["id"]:
                duty["status"] = "delivered"
        summary = desk.withdraw("h1")
        self.assertEqual(len(summary["delivered"]), 1)
        self.assertEqual(len(summary["awaiting_takeover"]), 1)
        self.assertEqual(len(summary["locked"]), 1)
        vacant_id = summary["awaiting_takeover"][0]
        # 未交付的真实记录不被删除
        self.assertEqual(desk._duties[vacant_id]["status"], "vacant")
        # 有人接手后恢复派交，且不能与同段同时段责任人冲突
        desk.reassign(vacant_id, "h2")
        self.assertEqual(desk._duties[vacant_id]["owner"], "h2")
        with self.assertRaises(CovenantError) as caught:
            desk.reassign(vacant_id, "h3")
        self.assertEqual(caught.exception.status, "duty_not_vacant")
        # 锁定的工作不能被转接
        with self.assertRaises(CovenantError) as caught:
            desk.reassign(summary["locked"][0], "h2")
        self.assertEqual(caught.exception.status, "duty_not_vacant")

    def test_household_view_scopes_to_own_commitments(self):
        desk = self._activate_pond()
        desk.assign_duty("T3", "h1", [300, 320])
        desk.assign_duty("T3", "h2", [330, 340])
        view = desk.household_view("h1")
        self.assertEqual(len(view["changeable"]), 1)
        self.assertEqual(view["changeable"][0]["owner"], "h1")
        self.assertEqual(desk.household_view("h2")["changeable"][0]["owner"], "h2")
        self.assertEqual(desk.household_view("stranger")["changeable"], [])
        # 签过字的住户能看到自己的签名
        signed = desk.household_view("n1")
        self.assertIn({"term": "T1", "version": 1, "superseded": False}, signed["signatures"])

    def test_cross_village_identity_materials_are_redacted(self):
        north_view = self.desk.parties_view(viewer="north")
        self.assertEqual(north_view["north"]["members"], ["n1", "n2", "n3"])
        self.assertTrue(str(north_view["south"]["members"]).startswith("redacted"))
        self.assertTrue(str(north_view["proxy"]["for"]).startswith("redacted"))
        south_view = self.desk.parties_view(viewer="south")
        self.assertEqual(south_view["proxy"]["for"], "member-s3")
        self.assertTrue(str(south_view["north"]["members"]).startswith("redacted"))
        # 表决明细对另一村只给计数，不给成员身份
        t1 = self.desk.term_view("T1", viewer="south")["versions"][0]
        self.assertNotIn("signers", t1["approvals"]["north"])
        t1_north = self.desk.term_view("T1", viewer="north")["versions"][0]
        self.assertEqual(t1_north["approvals"]["north"]["signers"], ["n1", "n2"])


class ReceiptAndServiceTest(unittest.TestCase):
    def test_late_partner_signature_enters_its_version_without_revival(self):
        receipts = [
            {"action": "read-signature", "desk": "south-desk",
             "fact": {"term": "T2", "version": 1, "received_at": 140, "signed_at": 105},
             "unavailable_first": False},
            {"action": "read-duty", "desk": "north-desk",
             "fact": {"term": "T1", "shift": "shift-a", "accepted": True},
             "unavailable_first": False},
        ]
        desk = make_desk(receipts=receipts)
        fact = desk.pull_receipt("read-signature", "south-desk")
        self.assertEqual(fact["signed_at"], 105)
        t2 = desk.term_view("T2")
        v1 = next(v for v in t2["versions"] if v["version"] == 1)
        self.assertIn("superseded", v1["reasons"])  # 迟到签名不复活 v1
        self.assertEqual(v1["late_signatures"][0]["received_at"], 140)
        # 重复拉取幂等
        desk.pull_receipt("read-signature", "south-desk")
        self.assertEqual(len(v1["late_signatures"]), 1)
        # 责任回执落账
        desk.pull_receipt("read-duty", "north-desk")
        duty = next(d for d in desk.duties() if d["term"] == "T1")
        self.assertTrue(duty["partner_accepted"])

    def test_unavailable_receipt_then_available(self):
        receipts = [
            {"action": "read-duty", "desk": "north-desk",
             "fact": {"term": "T1", "shift": "shift-a", "accepted": True},
             "unavailable_first": True},
        ]
        desk = make_desk(receipts=receipts)
        with self.assertRaises(CovenantError) as caught:
            desk.pull_receipt("read-duty", "north-desk")
        self.assertEqual(caught.exception.status, "temporarily_unavailable")
        fact = desk.pull_receipt("read-duty", "north-desk")
        self.assertTrue(fact["accepted"])

    def test_unknown_receipt(self):
        desk = make_desk()
        with self.assertRaises(CovenantError) as caught:
            desk.pull_receipt("read-signature", "nowhere")
        self.assertEqual(caught.exception.status, "unknown_receipt")

    def test_from_folder_matches_fixture(self):
        desk = CovenantDesk.from_folder(module.DATA)
        self.assertEqual([t["id"] for t in desk.effective_terms()], ["T1"])
        self.assertEqual([i["term"] for i in desk.pending_joint()], ["T2"])
        self.assertEqual(desk.term_view("T2")["current_version"], 2)

    def test_propose_term_requires_joint_shares(self):
        desk = make_desk()
        with self.assertRaises(CovenantError) as caught:
            desk.propose_term("T9", "pond", "maintain")
        self.assertEqual(caught.exception.status, "bad_shares")
        view = desk.propose_term(
            "T9", "S-b", "prune", shares={"south": 5000}
        )
        self.assertEqual(view["scope"], "south")
        self.assertFalse(view["effective"])

    def test_notice_dispatch_is_recorded(self):
        desk = make_desk()
        notice = desk.send_notice("all", "明早巡查集合", term_id="T1", by="operator")
        self.assertEqual(notice["recipient"], "all")
        self.assertEqual(desk.notices()[0]["text"], "明早巡查集合")
        with self.assertRaises(CovenantError):
            desk.send_notice("west", "x")

    def test_bad_slot_rejected(self):
        desk = make_desk(now=10)
        with self.assertRaises(CovenantError):
            desk.assign_duty("T1", "h", [100, 100])


class HttpServiceTest(unittest.TestCase):
    def setUp(self):
        self.desk = make_desk(now=200)
        self.server = CovenantHTTPServer(0, self.desk)
        self.addCleanup(self.server.server_close)
        thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            payload = json.dumps(body).encode("utf-8") if body is not None else None
            headers = {"Content-Type": "application/json"} if payload else {}
            connection.request(method, path, payload, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_get_endpoints(self):
        status, data = self.request("GET", "/terms/effective")
        self.assertEqual(status, 200)
        self.assertEqual([t["id"] for t in data["terms"]], ["T1"])
        status, data = self.request("GET", "/joint/pending")
        self.assertEqual(status, 200)
        self.assertEqual(data["items"][0]["term"], "T2")
        status, data = self.request("GET", "/duties")
        self.assertEqual(status, 200)
        self.assertTrue(data["duties"])
        status, data = self.request("GET", "/parties?viewer=north")
        self.assertEqual(status, 200)
        self.assertTrue(str(data["south"]["members"]).startswith("redacted"))
        status, data = self.request("GET", "/households/n1")
        self.assertEqual(status, 200)
        self.assertTrue(data["signatures"])

    def test_post_duty_conflict_and_notice(self):
        self.desk.register["terms"].append({
            "id": "T3", "subject": "pond", "work": "maintain", "version": 1,
            "shares": {"north": 10000, "south": 10000},
        })
        self.desk._terms["T3"] = self.desk._new_term(self.desk.register["terms"][-1])
        v1 = self.desk._find_version(self.desk._require_term("T3"), 1)
        for party in ("north", "south"):
            v1["ballots"][party]["principals"] = ["a", "b"]
            v1["ballots"][party]["signers"] = ["a", "b"]
        status, _ = self.request("POST", "/duties",
                                 {"term": "T3", "owner": "h1", "slot": [220, 240]})
        self.assertEqual(status, 201)
        status, data = self.request("POST", "/duties",
                                    {"term": "T3", "owner": "h2", "slot": [230, 250]})
        self.assertEqual(status, 409)
        self.assertEqual(data["status"], "duty_conflict")
        status, data = self.request("POST", "/notices",
                                    {"recipient": "all", "text": "通知"})
        self.assertEqual(status, 201)
        status, _ = self.request("POST", "/duties",
                                 {"term": "T2", "owner": "h", "slot": [1, 2]})
        self.assertEqual(status, 409)

    def test_unknown_route(self):
        self.assertEqual(self.request("GET", "/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
