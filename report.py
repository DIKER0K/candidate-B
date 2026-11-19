#!/usr/bin/env python3
"""Отчёт по договорам: работы, выплаты, остатки на дату.

Читает три CSV из папки data/ и печатает отчёт по работам и по договорам.
Деньги считаются в Decimal, без float.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

CENT = Decimal("0.01")


class DataError(Exception):
    """Данные не позволяют посчитать отчёт: файла нет, колонки нет, сумма не число."""


def money(value: str, where: str = "") -> Decimal:
    """Разбор денежной строки в Decimal с копейками (округление половины вверх)."""
    text = (value or "").strip().replace(" ", "").replace(",", ".")
    if not text:
        return Decimal("0.00")
    try:
        return Decimal(text).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise DataError(f"{where}: сумма {value!r} не является числом") from None


def parse_date(value: str) -> date | None:
    """Дата из YYYY-MM-DD. Пустая или некорректная строка -> None."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        year, month, day = text.split("-")
        return date(int(year), int(month), int(day))
    except (ValueError, TypeError):
        return None


def fmt(amount: Decimal) -> str:
    """Формат денег для консоли: 1234567.89 -> 1 234 567,89"""
    negative = amount < 0
    quant = abs(amount).quantize(CENT, rounding=ROUND_HALF_UP)
    whole, cents = f"{quant:.2f}".split(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    text = " ".join(groups) + "," + cents
    return ("-" if negative else "") + text


@dataclass
class Contract:
    contract_id: str
    contractor: str
    amount: Decimal
    signed_at: date | None


@dataclass
class Work:
    work_id: str
    contract_id: str
    title: str
    price: Decimal
    status_raw: str
    completed_at: date | None
    cancelled_at: date | None
    # заполняется при расчёте
    paid_direct: Decimal = Decimal("0.00")
    paid_advance: Decimal = Decimal("0.00")

    @property
    def paid(self) -> Decimal:
        return self.paid_direct + self.paid_advance

    def status_on(self, on_date: date) -> str:
        """Статус работы на отчётную дату, а не «как в выгрузке»."""
        if self.cancelled_at and self.cancelled_at <= on_date:
            return "cancelled"
        if self.completed_at and self.completed_at <= on_date:
            return "done"
        # Дат нет — доверяем статусу из выгрузки (в отчёте будет предупреждение).
        if not self.cancelled_at and not self.completed_at:
            if self.status_raw == "cancelled":
                return "cancelled"
            if self.status_raw == "done":
                return "done"
        return "planned"


@dataclass
class Payment:
    payment_id: str
    contract_id: str
    work_id: str
    purpose: str
    amount: Decimal
    status: str
    created_at: date | None
    paid_at: date | None
    refunded_at: date | None


@dataclass
class ContractResult:
    contract: Contract
    on_date: date
    works: list[Work] = field(default_factory=list)
    paid_total: Decimal = Decimal("0.00")
    queued: Decimal = Decimal("0.00")
    refunded: Decimal = Decimal("0.00")
    unallocated_advance: Decimal = Decimal("0.00")

    @property
    def payable(self) -> Decimal:
        """Сумма работ к оплате на отчётную дату (отменённые к этой дате не входят)."""
        return sum((w.price for w in self.works if w.status_on(self.on_date) != "cancelled"),
                   Decimal("0.00"))

    @property
    def left(self) -> Decimal:
        return self.payable - self.paid_total


REQUIRED_COLUMNS = {
    "contracts.csv": ["contract_id", "contractor", "amount", "signed_at"],
    "works.csv": ["work_id", "contract_id", "title", "price", "status",
                  "completed_at", "cancelled_at"],
    "payments.csv": ["payment_id", "contract_id", "work_id", "purpose", "amount",
                     "status", "created_at", "paid_at", "refunded_at"],
}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise DataError(f"нет файла {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        columns = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS.get(path.name, []) if c not in columns]
        if missing:
            raise DataError(f"{path.name}: нет колонок {', '.join(missing)}")
        return list(reader)


def load(data_dir: Path, warnings: list[str] | None = None
         ) -> tuple[dict[str, Contract], dict[str, Work], list[Payment]]:
    warnings = warnings if warnings is not None else []
    contracts = {}
    for row in read_csv(data_dir / "contracts.csv"):
        c = Contract(
            contract_id=row["contract_id"].strip(),
            contractor=row["contractor"].strip(),
            amount=money(row["amount"], f"contracts.csv:{row['contract_id']}"),
            signed_at=parse_date(row["signed_at"]),
        )
        contracts[c.contract_id] = c

    works = {}
    for row in read_csv(data_dir / "works.csv"):
        w = Work(
            work_id=row["work_id"].strip(),
            contract_id=row["contract_id"].strip(),
            title=row["title"].strip(),
            price=money(row["price"], f"works.csv:{row['work_id']}"),
            status_raw=row["status"].strip(),
            completed_at=parse_date(row.get("completed_at", "")),
            cancelled_at=parse_date(row.get("cancelled_at", "")),
        )
        if w.status_raw == "cancelled" and w.cancelled_at is None:
            warnings.append(f"работа {w.work_id}: статус cancelled без даты отмены — считаем отменённой")
        if w.status_raw == "done" and w.completed_at is None:
            warnings.append(f"работа {w.work_id}: статус done без даты приёмки — считаем выполненной")
        if w.status_raw not in ("planned", "done", "cancelled"):
            warnings.append(f"работа {w.work_id}: неизвестный статус {w.status_raw!r}")
        works[w.work_id] = w

    payments = []
    for row in read_csv(data_dir / "payments.csv"):
        p = Payment(
            payment_id=row["payment_id"].strip(),
            contract_id=row["contract_id"].strip(),
            work_id=(row.get("work_id") or "").strip(),
            purpose=(row.get("purpose") or "").strip(),
            amount=money(row["amount"], f"payments.csv:{row['payment_id']}"),
            status=row["status"].strip(),
            created_at=parse_date(row.get("created_at", "")),
            paid_at=parse_date(row.get("paid_at", "")),
            refunded_at=parse_date(row.get("refunded_at", "")),
        )
        if p.status == "refunded" and p.refunded_at is None:
            warnings.append(f"платёж {p.payment_id}: возврат без даты — считаем деньги выплаченными")
        if p.status == "paid" and p.paid_at is None:
            warnings.append(f"платёж {p.payment_id}: paid без даты платежа — учитываем по дате заведения")
        payments.append(p)

    return contracts, works, payments


def _direct_work(works: dict[str, Work], payment: Payment) -> Work | None:
    """Работа, к которой платёж привязан, — только если она из того же договора."""
    if not payment.work_id:
        return None
    work = works.get(payment.work_id)
    if work is None or work.contract_id != payment.contract_id:
        return None
    return work


def _effective_paid(payment: Payment, on_date: date) -> Decimal:
    """Сколько денег по этому платежу реально было у подрядчика на отчётную дату.

    Возврат вычитает деньги только с даты возврата: до неё платёж считается выплатой.
    """
    if payment.status == "paid":
        paid_on = payment.paid_at or payment.created_at
        if paid_on is not None and paid_on <= on_date:
            return payment.amount
        return Decimal("0.00")

    if payment.status == "refunded":
        if payment.refunded_at and payment.refunded_at <= on_date:
            return Decimal("0.00")
        paid_on = payment.paid_at or payment.created_at
        if paid_on is not None and paid_on <= on_date:
            return payment.amount
        return Decimal("0.00")

    return Decimal("0.00")


def build_report(contracts: dict[str, Contract], works: dict[str, Work],
                 payments: list[Payment], on_date: date,
                 warnings: list[str] | None = None) -> list[ContractResult]:
    warnings = warnings if warnings is not None else []

    # Копии работ: расчёт не должен менять входные данные (иначе повторный
    # вызов на тех же объектах даёт другие цифры).
    works = {
        wid: replace(w, paid_direct=Decimal("0.00"), paid_advance=Decimal("0.00"))
        for wid, w in works.items()
    }

    results: dict[str, ContractResult] = {}
    for cid, c in contracts.items():
        # Договор, подписанный после отчётной даты, в отчёт не попадает.
        if c.signed_at and c.signed_at > on_date:
            warnings.append(f"договор {cid} подписан {c.signed_at:%d.%m.%Y} — позже отчётной даты, пропущен")
            continue
        results[cid] = ContractResult(contract=c, on_date=on_date)

    skipped = {cid for cid, c in contracts.items() if cid not in results}
    for w in works.values():
        if w.contract_id in results:
            results[w.contract_id].works.append(w)
        elif w.contract_id in skipped:
            warnings.append(f"работа {w.work_id}: договор {w.contract_id} пропущен (подписан позже отчётной даты)")
        else:
            warnings.append(f"работа {w.work_id}: неизвестный договор {w.contract_id}")

    for p in payments:
        res = results.get(p.contract_id)
        if res is None:
            if p.contract_id not in contracts:
                warnings.append(f"платёж {p.payment_id}: неизвестный договор {p.contract_id}")
            continue

        if p.status == "refunded":
            # Возврат вычитает деньги с даты возврата; до неё платёж — выплата.
            if p.refunded_at and p.refunded_at <= on_date:
                res.refunded += p.amount
                continue
            amount = _effective_paid(p, on_date)
            if amount:
                res.paid_total += amount
                work = _direct_work(works, p)
                if work is not None:
                    work.paid_direct += amount
            continue

        if p.status == "queued":
            queued_on = p.created_at or p.paid_at
            if queued_on is None:
                warnings.append(f"платёж {p.payment_id}: в очереди без даты — показан в отчёте")
                res.queued += p.amount
            elif queued_on <= on_date:
                res.queued += p.amount
            continue

        if p.status != "paid":
            warnings.append(f"платёж {p.payment_id}: неизвестный статус {p.status!r}, пропущен")
            continue

        paid_on = p.paid_at or p.created_at
        if paid_on is None or paid_on > on_date:
            continue

        res.paid_total += p.amount
        if p.work_id:
            work = _direct_work(works, p)
            if work is not None:
                work.paid_direct += p.amount
            else:
                warnings.append(
                    f"платёж {p.payment_id}: работа {p.work_id} не найдена или из другого договора — "
                    f"учтён как нераспределённый")

    # Авансы: платежи без привязки (или с битой привязкой) разносим FIFO —
    # сначала выполненные работы по дате приёмки, затем запланированные.
    for res in results.values():
        cid = res.contract.contract_id
        unbound = Decimal("0.00")
        for p in payments:
            if p.contract_id != cid:
                continue
            amount = _effective_paid(p, on_date)
            if not amount:
                continue
            if _direct_work(works, p) is None:
                unbound += amount

        if unbound <= 0:
            continue

        order = sorted(
            [w for w in res.works if w.status_on(on_date) != "cancelled"],
            key=lambda w: (
                0 if w.status_on(on_date) == "done" else 1,
                w.completed_at or date.max,
                w.work_id,
            ),
        )
        left = unbound
        for w in order:
            if left <= 0:
                break
            need = w.price - w.paid_direct - w.paid_advance
            if need <= 0:
                continue
            part = min(need, left)
            w.paid_advance += part
            left -= part
        res.unallocated_advance = left

    return list(results.values())


def print_report(results: list[ContractResult], on_date: date,
                 warnings: list[str] | None = None) -> None:
    print(f"Отчёт по договорам на {on_date:%d.%m.%Y}")
    print()

    total_payable = Decimal("0.00")
    total_paid = Decimal("0.00")
    total_left = Decimal("0.00")

    for res in sorted(results, key=lambda r: r.contract.contract_id):
        c = res.contract
        print(f"Договор {c.contract_id} — {c.contractor}, сумма {fmt(c.amount)} ₽")
        if c.signed_at:
            print(f"  подписан {c.signed_at:%d.%m.%Y}")
        print()
        header = f"  {'Работа':<46} {'Цена':>13} {'Выплачено':>13} {'Осталось':>13}  Статус"
        print(header)
        print("  " + "-" * (len(header) - 2))

        for w in sorted(res.works, key=lambda x: x.work_id):
            st = w.status_on(on_date)
            left = w.price - w.paid
            title = w.title if len(w.title) <= 44 else w.title[:43] + "…"
            mark = {"done": "выполнена", "planned": "запланирована", "cancelled": "отменена"}[st]
            if st == "cancelled":
                print(f"  {title:<46} {fmt(w.price):>13} {fmt(w.paid):>13} {'—':>13}  {mark}")
            else:
                print(f"  {title:<46} {fmt(w.price):>13} {fmt(w.paid):>13} {fmt(left):>13}  {mark}")

        print()
        print("  Итого по договору:")
        print(f"    подлежит оплате (без отменённых на дату): {fmt(res.payable)} ₽")
        print(f"    выплачено:                                {fmt(res.paid_total)} ₽")
        if res.left < 0:
            print(f"    переплата по договору:                    {fmt(-res.left)} ₽")
        else:
            print(f"    осталось оплатить:                        {fmt(res.left)} ₽")
        if res.queued:
            print(f"    в очереди на оплату (деньги не ушли):     {fmt(res.queued)} ₽")
        if res.refunded:
            print(f"    возвращено подрядчиком:                   {fmt(res.refunded)} ₽")
        if res.unallocated_advance:
            print(f"    аванс без распределения по работам:       {fmt(res.unallocated_advance)} ₽")
        print()

        total_payable += res.payable
        total_paid += res.paid_total
        total_left += res.left

    print("=" * 78)
    tail = (f"переплата {fmt(-total_left)} ₽" if total_left < 0
            else f"осталось {fmt(total_left)} ₽")
    print(f"Всего по всем договорам: подлежит оплате {fmt(total_payable)} ₽, "
          f"выплачено {fmt(total_paid)} ₽, {tail}")

    if warnings:
        print()
        print("Предупреждения по данным:")
        for w in warnings:
            print(f"  - {w}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Отчёт по договорам, работам и платежам")
    parser.add_argument("--data", default="data", help="папка с CSV (по умолчанию ./data)")
    parser.add_argument("--date", default="2026-11-10", help="отчётная дата, YYYY-MM-DD")
    args = parser.parse_args(argv)

    data_dir = Path(args.data)
    if not data_dir.is_dir():
        print(f"нет папки с данными: {data_dir}", file=sys.stderr)
        return 2

    on_date = parse_date(args.date)
    if on_date is None:
        print(f"неверная дата: {args.date} (ожидается YYYY-MM-DD)", file=sys.stderr)
        return 2

    warnings: list[str] = []
    try:
        contracts, works, payments = load(data_dir, warnings)
    except DataError as exc:
        print(f"ошибка данных: {exc}", file=sys.stderr)
        return 2
    results = build_report(contracts, works, payments, on_date, warnings)
    print_report(results, on_date, warnings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
