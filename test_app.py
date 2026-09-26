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

    def test_hold_freezes_whole_derivative_chain_and_restores(self):
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-003", "disk.img",
            base64.b64encode(b"disk image").decode(), self.retention,
        )
        # 冻结原件后，开箱、移交、派生、释放全部暂停
        self.store.set_hold("custodian1", item["id"], True, "法院诉讼保全裁定")
        for action in (
            lambda: self.store.open_evidence("custodian1", item["id"], "A 区证物室"),
            lambda: self.store.transfer("custodian1", item["id"], "custodian2", "法院证物库"),
            lambda: self.store.derive("analyst1", item["id"], "镜像提取", "E-003-D0", "x.json", base64.b64encode(b"{}").decode()),
            lambda: self.store.release("custodian1", item["id"], "检察机关"),
        ):
            with self.assertRaises(BusinessError) as ctx:
                action()
            self.assertEqual(ctx.exception.code, "legal_hold_active")
        # 解除后按原状态恢复：可开箱、可派生
        self.store.set_hold("auditor1", item["id"], False, "保全裁定解除")
        self.store.open_evidence("custodian1", item["id"], "A 区证物室")
        child = self.store.derive(
            "analyst1", item["id"], "镜像文件提取", "E-003-D1", "files.json",
            base64.b64encode(b'["a.txt"]').decode(),
        )
        # 再次冻结原件：派生暂停，已有衍生证据也不能移交或释放
        self.store.set_hold("auditor1", item["id"], True, "二审补充保全")
        with self.assertRaises(BusinessError) as ctx:
            self.store.derive("analyst1", item["id"], "二次提取", "E-003-D2", "y.json", base64.b64encode(b"{}").decode())
        self.assertEqual(ctx.exception.code, "legal_hold_active")
        for action in (
            lambda: self.store.transfer("custodian1", child["id"], "custodian2", "分析实验室"),
            lambda: self.store.release("custodian1", child["id"], "检察机关"),
        ):
            with self.assertRaises(BusinessError) as ctx:
                action()
            self.assertEqual(ctx.exception.code, "legal_hold_active")
            self.assertIn(f"#{item['id']}", ctx.exception.message)
        # 报告标注冻结来源与可办理操作
        report = self.store.report("auditor1", self.case["id"])
        original = next(x for x in report["evidence"] if x["id"] == item["id"])
        derivative = next(x for x in report["evidence"] if x["id"] == child["id"])
        self.assertTrue(original["legal_hold"])
        self.assertEqual(original["allowed_operations"], [])
        self.assertFalse(derivative["legal_hold"])
        self.assertTrue(derivative["effective_hold"])
        self.assertEqual(derivative["hold_sources"], [{"evidence_id": item["id"], "label": "E-003"}])
        self.assertEqual(derivative["allowed_operations"], [])
        # 解除后各自按原状态恢复：衍生证据可移交、可释放，原件可移交
        self.store.set_hold("custodian1", item["id"], False, "二审保全期满解除")
        self.store.transfer("custodian1", child["id"], "custodian2", "分析实验室")
        self.store.release("custodian2", child["id"], "检察机关")
        report = self.store.report("auditor1", self.case["id"])
        original = next(x for x in report["evidence"] if x["id"] == item["id"])
        derivative = next(x for x in report["evidence"] if x["id"] == child["id"])
        self.assertEqual(original["allowed_operations"], ["derive", "transfer", "release"])
        self.assertEqual(derivative["status"], "released")
        self.assertEqual(derivative["allowed_operations"], [])


if __name__ == "__main__":
    unittest.main()
