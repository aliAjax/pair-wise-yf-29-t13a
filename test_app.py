import base64
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, CustodyStore


class CustodyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CustodyStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.case = self.store.create_case("custodian1", "CASE-2026-001", "跨境资金调查")
        self.store.add_member("custodian1", self.case["id"], "custodian2", "custodian")
        self.store.add_member("custodian1", self.case["id"], "analyst1", "analyst")
        self.store.add_member("custodian1", self.case["id"], "auditor1", "auditor")
        self.retention = (date.today() + timedelta(days=3650)).isoformat()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_custody_analysis_release_and_integrity_report(self):
        raw = b"bank statement original bytes"
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-001", "statement.csv",
            base64.b64encode(raw).decode(), self.retention, "custodian1",
        )
        opened = self.store.open_evidence("custodian1", item["id"], "A 区证物室", "两名人员在场开箱")
        self.assertEqual(opened["status"], "opened")
        child = self.store.derive(
            "analyst1", item["id"], "CSV 提取交易记录", "E-001-D1", "transactions.json",
            base64.b64encode(b'[{"amount": 100}]').decode(),
        )
        self.store.transfer("custodian2", item["id"], "custodian2", "法院证物库", "封存后移交")
        self.store.release("custodian2", item["id"], "检察机关", "按调取令释放原件")
        report = self.store.report("auditor1", self.case["id"])
        self.assertTrue(report["overall_integrity_valid"])
        self.assertEqual(report["evidence_count"], 2)
        original = next(x for x in report["evidence"] if x["id"] == item["id"])
        self.assertEqual(original["status"], "released")
        self.assertTrue(original["chain_valid"])
        self.assertEqual(child["parent_id"], item["id"])

    def test_permissions_and_legal_hold_block_release(self):
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-002", "raw.bin",
            base64.b64encode(b"evidence").decode(), self.retention,
        )
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_evidence("outsider", item["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("analyst1", item["id"], "外部机构")
        self.assertEqual(ctx.exception.status, 403)
        self.store.set_hold("auditor1", item["id"], True, "诉讼保全要求")
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("custodian1", item["id"], "外部机构")
        self.assertEqual(ctx.exception.code, "legal_hold_active")

    def test_hold_freezes_original_operations_until_cleared(self):
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-100", "disk.img",
            base64.b64encode(b"disk-bytes").decode(), self.retention,
        )
        self.store.set_hold("custodian1", item["id"], True, "法院诉讼保全裁定")
        for action in (
            lambda: self.store.open_evidence("custodian1", item["id"], "证物室"),
            lambda: self.store.transfer("custodian1", item["id"], "custodian2", "证物库"),
            lambda: self.store.derive(
                "analyst1", item["id"], "镜像哈希校验", "E-100-D1", "hash.txt",
                base64.b64encode(b"x").decode(),
            ),
        ):
            with self.assertRaises(BusinessError) as ctx:
                action()
            self.assertEqual(ctx.exception.code, "legal_hold_frozen")
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("custodian1", item["id"], "检察机关")
        self.assertEqual(ctx.exception.code, "legal_hold_active")
        view = self.store.get_evidence("auditor1", item["id"])
        self.assertTrue(view["frozen"])
        self.assertEqual(view["hold_source"], {"type": "direct", "evidence_id": item["id"]})
        self.assertEqual(view["allowed_operations"], [])
        self.store.set_hold("custodian1", item["id"], False, "裁定解除，恢复办理")
        opened = self.store.open_evidence("custodian1", item["id"], "证物室")
        self.assertEqual(opened["status"], "opened")
        view = self.store.get_evidence("custodian1", item["id"])
        self.assertFalse(view["frozen"])
        self.assertIsNone(view["hold_source"])
        self.assertEqual(view["allowed_operations"], ["transfer", "derive", "release"])

    def test_hold_on_original_freezes_derivative_chain(self):
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-200", "mail.pst",
            base64.b64encode(b"mailbox").decode(), self.retention,
        )
        self.store.open_evidence("custodian1", item["id"], "证物室")
        child = self.store.derive(
            "analyst1", item["id"], "CSV 提取交易记录", "E-200-D1", "tx.json",
            base64.b64encode(b"[]").decode(),
        )
        self.store.transfer("custodian1", child["id"], "custodian2", "分析实验室")
        self.store.set_hold("auditor1", item["id"], True, "检察监督冻结要求")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transfer("custodian1", item["id"], "custodian2", "法院证物库")
        self.assertEqual(ctx.exception.code, "legal_hold_frozen")
        with self.assertRaises(BusinessError) as ctx:
            self.store.derive(
                "analyst1", item["id"], "再次提取", "E-200-D2", "x.json",
                base64.b64encode(b"{}").decode(),
            )
        self.assertEqual(ctx.exception.code, "legal_hold_frozen")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transfer("custodian1", child["id"], "custodian1", "证物室")
        self.assertEqual(ctx.exception.code, "legal_hold_frozen")
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("custodian1", child["id"], "外部机构")
        self.assertEqual(ctx.exception.code, "legal_hold_active")
        view = self.store.get_evidence("auditor1", child["id"])
        self.assertTrue(view["frozen"])
        self.assertEqual(view["hold_source"], {"type": "inherited", "ancestors": [item["id"]]})
        self.assertEqual(view["allowed_operations"], [])
        report = self.store.report("auditor1", self.case["id"])
        original = next(x for x in report["evidence"] if x["id"] == item["id"])
        derived = next(x for x in report["evidence"] if x["id"] == child["id"])
        self.assertEqual(original["hold_source"], {"type": "direct", "evidence_id": item["id"]})
        self.assertEqual(derived["hold_source"], {"type": "inherited", "ancestors": [item["id"]]})
        self.assertEqual(original["allowed_operations"], [])
        self.assertEqual(derived["allowed_operations"], [])
        self.store.set_hold("auditor1", item["id"], False, "冻结期满解除")
        restored = self.store.get_evidence("auditor1", item["id"])
        self.assertFalse(restored["frozen"])
        self.assertEqual(restored["allowed_operations"], ["transfer", "derive", "release"])
        moved = self.store.transfer("custodian1", child["id"], "custodian1", "证物室")
        self.assertEqual(moved["current_custodian"], "custodian1")
        done = self.store.release("custodian1", child["id"], "检察机关")
        self.assertEqual(done["status"], "released")

    def test_hold_on_derivative_does_not_freeze_parent(self):
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-300", "log.zip",
            base64.b64encode(b"logs").decode(), self.retention,
        )
        self.store.open_evidence("custodian1", item["id"], "证物室")
        child = self.store.derive(
            "analyst1", item["id"], "日志关键字命中", "E-300-D1", "hits.txt",
            base64.b64encode(b"hit").decode(),
        )
        self.store.set_hold("auditor1", child["id"], True, "衍生报告涉诉保全")
        parent_view = self.store.get_evidence("auditor1", item["id"])
        self.assertFalse(parent_view["frozen"])
        self.store.transfer("custodian1", item["id"], "custodian2", "证物库")
        child_view = self.store.get_evidence("auditor1", child["id"])
        self.assertTrue(child_view["frozen"])
        self.assertEqual(child_view["hold_source"], {"type": "direct", "evidence_id": child["id"]})


if __name__ == "__main__":
    unittest.main()
