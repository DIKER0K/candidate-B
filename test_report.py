"""Проверки расчёта: цифры из задания, граничные случаи и логика дат."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path

import report


DATA = Path(__file__).parent / "data"


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.contracts, self.works, self.payments = report.load(DATA)

    def build(self, day: str):
        return {r.contract.contract_id: r
                for r in report.build_report(self.contracts, self.works, self.payments,
                                             report.parse_date(day))}

    # --- основные цифры на отчётную дату ---

    def test_otchet_na_10_11(self):
        res = self.build("2026-11-10")
        # D-1: 500 000 подлежит оплате, 315 000 выплачено (аванс 300 000 + 15 000 к осени)
        self.assertEqual(res["D-1"].payable, Decimal("500000.00"))
        self.assertEqual(res["D-1"].paid_total, Decimal("315000.00"))
        self.assertEqual(res["D-1"].left, Decimal("185000.00"))
        # платёж в очереди не считается выплатой
        self.assertEqual(res["D-1"].queued, Decimal("150000.00"))
        # возврат не уменьшает остаток к оплате
        self.assertEqual(res["D-1"].refunded, Decimal("50000.00"))

        # D-2: работа W-23 принята 11.11 — на 10.11 ещё не выполнена
        self.assertEqual(res["D-2"].payable, Decimal("90000.00"))
        self.assertEqual(res["D-2"].paid_total, Decimal("75000.00"))
        self.assertEqual(res["D-2"].left, Decimal("15000.00"))

    def test_raznesenie_avansa_po_rabotam(self):
        """FIFO: аванс закрывает работы по порядку, суммы по строкам фиксированы."""
        res = self.build("2026-11-10")

        d1 = {w.work_id: w for w in res["D-1"].works}
        self.assertEqual(d1["W-11"].paid, Decimal("100000.00"))
        self.assertEqual(d1["W-12"].paid, Decimal("100000.00"))
        self.assertEqual(d1["W-14"].paid, Decimal("100000.00"))   # 85 000 аванса + 15 000 прямых
        self.assertEqual(d1["W-15"].paid, Decimal("15000.00"))
        self.assertEqual(d1["W-16"].paid, Decimal("0.00"))
        self.assertEqual(d1["W-13"].paid, Decimal("0.00"))

        d2 = {w.work_id: w for w in res["D-2"].works}
        self.assertEqual(d2["W-21"].paid, Decimal("20000.00"))
        self.assertEqual(d2["W-22"].paid, Decimal("30000.00"))
        self.assertEqual(d2["W-23"].paid, Decimal("25000.00"))

    # --- статусы и суммы на дату ---

    def test_status_on_date(self):
        res = self.build("2026-11-10")
        by_id = {w.work_id: w for w in res["D-2"].works}
        self.assertEqual(by_id["W-23"].status_on(date(2026, 11, 10)), "planned")
        self.assertEqual(by_id["W-23"].status_on(date(2026, 11, 11)), "done")

    def test_otmena_uchityvaetsya_na_datu(self):
        """До отмены (12.10) работа W-13 ещё входит в сумму к оплате."""
        res_early = self.build("2026-10-01")
        self.assertEqual(res_early["D-1"].payable, Decimal("600000.00"))
        self.assertEqual(res_early["D-1"].paid_total, Decimal("300000.00"))
        self.assertEqual(res_early["D-1"].left, Decimal("300000.00"))

        by_id = {w.work_id: w for w in res_early["D-1"].works}
        self.assertEqual(by_id["W-13"].status_on(date(2026, 10, 1)), "planned")
        self.assertEqual(by_id["W-11"].status_on(date(2026, 10, 1)), "done")
        # возврат P-2 к 01.10 уже состоялся (10.09), в выплаты не входит
        self.assertEqual(res_early["D-1"].refunded, Decimal("50000.00"))

    def test_vozvrat_uchityvaetsya_po_date(self):
        """05.09: деньги ушли 02.09, вернулись 10.09 — на 05.09 это ещё выплата."""
        res = self.build("2026-09-05")
        self.assertEqual(res["D-1"].paid_total, Decimal("350000.00"))
        self.assertEqual(res["D-1"].refunded, Decimal("0.00"))
        self.assertEqual(res["D-1"].left, Decimal("250000.00"))

        # после даты возврата — уже не выплата
        res_after = self.build("2026-10-01")
        self.assertEqual(res_after["D-1"].paid_total, Decimal("300000.00"))
        self.assertEqual(res_after["D-1"].refunded, Decimal("50000.00"))

    def test_pozdnie_operacii_ne_popadayut(self):
        res10 = self.build("2026-11-10")
        res12 = self.build("2026-11-12")
        self.assertEqual(res10["D-2"].paid_total, Decimal("75000.00"))
        self.assertEqual(res12["D-2"].paid_total, Decimal("95000.00"))
        # на 12.11 выплачено 95 000 при сумме работ 90 000 — переплата 5 000
        self.assertEqual(res12["D-2"].left, Decimal("-5000.00"))

    # --- инварианты ---

    def test_summa_po_rabotam_ravna_vyplate(self):
        """Выплата = разнесено по работам + нераспределённый остаток."""
        for day in ("2026-09-05", "2026-10-01", "2026-11-10", "2026-11-12"):
            for r in self.build(day).values():
                paid_by_works = sum((w.paid for w in r.works), Decimal("0.00"))
                self.assertEqual(paid_by_works + r.unallocated_advance, r.paid_total,
                                 f"{r.contract.contract_id} на {day}")

    def test_vyplata_po_rabote_ne_bolshe_ceny(self):
        for day in ("2026-10-01", "2026-11-10", "2026-11-12"):
            for r in self.build(day).values():
                for w in r.works:
                    self.assertLessEqual(w.paid, w.price, f"{w.work_id} на {day}")

    # --- деньги, даты, CLI ---

    def test_dengi_bez_float(self):
        self.assertEqual(report.money("1234.56"), Decimal("1234.56"))
        self.assertEqual(report.money("1 234,56"), Decimal("1234.56"))
        self.assertEqual(report.money("0.005"), Decimal("0.01"))     # половина вверх
        self.assertEqual(report.money(""), Decimal("0.00"))
        self.assertEqual(report.fmt(Decimal("1234567.89")), "1 234 567,89")
        self.assertEqual(report.fmt(Decimal("-5000")), "-5 000,00")

    def test_slomannaya_data_ne_padaet(self):
        self.assertIsNone(report.parse_date("2026-13-01"))
        self.assertIsNone(report.parse_date("не дата"))
        self.assertIsNone(report.parse_date(""))
        self.assertEqual(report.parse_date("2026-11-10"), date(2026, 11, 10))

    def test_cli_na_date_10_11(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = report.main(["--data", str(DATA), "--date", "2026-11-10"])
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("185 000,00", out)
        self.assertIn("15 000,00", out)
        self.assertNotIn("e-", out.lower())

    def test_cli_slomannaya_data(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = report.main(["--data", str(DATA), "--date", "2026-13-01"])
        self.assertEqual(code, 2)


class BadDataTest(unittest.TestCase):
    """Мусорные данные: внятная ошибка вместо traceback и предупреждения там, где можно посчитать."""

    def make_dir(self, contracts, works, payments, *, drop_column=None):
        tmp = tempfile.TemporaryDirectory()
        base = Path(tmp.name)
        files = {
            "contracts.csv": "contract_id,contractor,amount,signed_at\n" + contracts,
            "works.csv": "work_id,contract_id,title,price,status,completed_at,cancelled_at\n" + works,
            "payments.csv": "payment_id,contract_id,work_id,purpose,amount,status,created_at,paid_at,refunded_at\n" + payments,
        }
        for name, text in files.items():
            if drop_column and name == drop_column[0]:
                lines = [ln for ln in text.splitlines()]
                keep = [i for i, c in enumerate(lines[0].split(",")) if c != drop_column[1]]
                text = "\n".join(",".join(parts.split(",")[i] for i in keep)
                                  for parts in lines) + "\n"
            (base / name).write_text(text, encoding="utf-8")
        self.addCleanup(tmp.cleanup)
        return base

    def test_bad_amount(self):
        base = self.make_dir("D-1,Студия,600000.00,2026-08-25\n",
                             "W-1,D-1,Работа,abc,planned,,\n", "")
        with self.assertRaises(report.DataError):
            report.load(base)

    def test_missing_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(report.DataError):
            report.load(Path(tmp.name))

    def test_missing_column(self):
        base = self.make_dir("D-1,Студия,600000.00,2026-08-25\n",
                             "W-1,D-1,Работа,100000.00,planned,,\n", "")
        base_csv = base / "works.csv"
        base_csv.write_text("work_id,contract_id,title,price\nW-1,D-1,Работа,100000.00\n", encoding="utf-8")
        with self.assertRaises(report.DataError):
            report.load(base)

    def test_cancelled_without_date(self):
        """cancelled без даты: считаем отменённой и предупреждаем."""
        base = self.make_dir("D-1,Студия,200000.00,2026-08-25\n",
                             "W-1,D-1,Работа,100000.00,done,2026-09-01,\n"
                             "W-2,D-1,Вторая,100000.00,cancelled,,\n", "")
        warnings = []
        contracts, works, payments = report.load(base, warnings)
        res = report.build_report(contracts, works, payments, date(2026, 10, 1), warnings)
        self.assertEqual(res[0].payable, Decimal("100000.00"))
        self.assertTrue(any("cancelled без даты" in w for w in warnings))

    def test_platezh_iz_chuzhogo_dogovora(self):
        """Привязка к работе из другого договора не ломает итоги."""
        base = self.make_dir(
            "D-1,Студия,100000.00,2026-08-25\nD-2,Иван,50000.00,2026-09-01\n",
            "W-1,D-1,Работа,100000.00,planned,,\nW-2,D-2,Другая,50000.00,planned,,\n",
            "P-1,D-1,W-2,Ошибочная привязка,30000.00,paid,2026-10-01,2026-10-02,\n")
        warnings = []
        contracts, works, payments = report.load(base, warnings)
        res = {r.contract.contract_id: r
               for r in report.build_report(contracts, works, payments, date(2026, 11, 1), warnings)}
        self.assertEqual(res["D-1"].paid_total, Decimal("30000.00"))
        self.assertEqual(res["D-2"].paid_total, Decimal("0.00"))
        self.assertEqual(res["D-1"].left, Decimal("70000.00"))
        self.assertTrue(any("из другого договора" in w or "нераспределённый" in w for w in warnings))

    def test_dogovor_pozzhe_daty(self):
        """Договор, подписанный после отчётной даты, в отчёт не попадает."""
        base = self.make_dir("D-1,Студия,100000.00,2026-12-01\n",
                             "W-1,D-1,Работа,100000.00,planned,,\n", "")
        warnings = []
        contracts, works, payments = report.load(base, warnings)
        res = report.build_report(contracts, works, payments, date(2026, 11, 10), warnings)
        self.assertEqual(res, [])
        self.assertTrue(any("позже отчётной даты" in w for w in warnings))

    def test_cli_bad_data(self):
        base = self.make_dir("D-1,Студия,abc,2026-08-25\n", "", "")
        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf):
            import contextlib
            with contextlib.redirect_stderr(err):
                code = report.main(["--data", str(base), "--date", "2026-11-10"])
        self.assertEqual(code, 2)
        self.assertIn("ошибка данных", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
