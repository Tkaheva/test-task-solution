#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

# ---------- Деньги ----------

def D(x: str | Decimal) -> Decimal:
    if isinstance(x, Decimal):
        return x
    x = (x or "").strip()
    if not x:
        return Decimal("0.00")
    return Decimal(x)

def money(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def fmt(x: Decimal) -> str:
    return f"{money(x):.2f}"

# ---------- Модели ----------

@dataclass
class Contract:
    contract_id: str
    contractor: str
    amount: Decimal
    signed_at: Optional[date]

@dataclass
class Work:
    work_id: str
    contract_id: str
    title: str
    price: Decimal
    status: str
    completed_at: Optional[date]
    cancelled_at: Optional[date]

@dataclass
class Payment:
    payment_id: str
    contract_id: str
    work_id: Optional[str]
    purpose: str
    amount: Decimal
    status: str
    created_at: Optional[date]
    paid_at: Optional[date]
    refunded_at: Optional[date]

# ---------- Загрузка ----------

def parse_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").date()

def load_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def load_contracts(data_dir: Path) -> list[Contract]:
    return [
        Contract(
            contract_id=r["contract_id"].strip(),
            contractor=r["contractor"].strip(),
            amount=D(r["amount"]),
            signed_at=parse_date(r["signed_at"]),
        )
        for r in load_csv(data_dir / "contracts.csv")
    ]

def load_works(data_dir: Path) -> list[Work]:
    return [
        Work(
            work_id=r["work_id"].strip(),
            contract_id=r["contract_id"].strip(),
            title=r["title"].strip(),
            price=D(r["price"]),
            status=r["status"].strip(),
            completed_at=parse_date(r["completed_at"]),
            cancelled_at=parse_date(r["cancelled_at"]),
        )
        for r in load_csv(data_dir / "works.csv")
    ]

def load_payments(data_dir: Path) -> list[Payment]:
    return [
        Payment(
            payment_id=r["payment_id"].strip(),
            contract_id=r["contract_id"].strip(),
            work_id=(r["work_id"] or "").strip() or None,
            purpose=r["purpose"].strip(),
            amount=D(r["amount"]),
            status=r["status"].strip(),
            created_at=parse_date(r["created_at"]),
            paid_at=parse_date(r["paid_at"]),
            refunded_at=parse_date(r["refunded_at"]),
        )
        for r in load_csv(data_dir / "payments.csv")
    ]

# ---------- Логика «на дату» ----------

def payment_is_paid_on(p: Payment, as_of: date) -> bool:
    """Деньги фактически ушли и не вернулись к дате отчёта."""
    if p.status == "paid":
        return p.paid_at is not None and p.paid_at <= as_of
    if p.status == "refunded":
        # ушли, но вернулись — нетто 0, в выплачено не включаем
        return False
    # queued — ещё не ушли
    return False

def payment_is_queued_on(p: Payment, as_of: date) -> bool:
    return p.status == "queued" and (p.created_at is None or p.created_at <= as_of)

def work_done_on(w: Work, as_of: date) -> bool:
    return w.status == "done" and w.completed_at is not None and w.completed_at <= as_of

def work_cancelled_on(w: Work, as_of: date) -> bool:
    return w.status == "cancelled" and w.cancelled_at is not None and w.cancelled_at <= as_of

def work_relevant_on(w: Work, as_of: date) -> bool:
    """Работа учитывается в отчёте, если она уже существует к дате."""
    if w.status == "planned":
        return True  
    if w.status == "done":
        return work_done_on(w, as_of)
    if w.status == "cancelled":
        return work_cancelled_on(w, as_of)
    return False

# ---------- Распределение платежей без привязки (FIFO) ----------

@dataclass
class WorkReport:
    work_id: str
    contract_id: str
    title: str
    status: str
    price: Decimal
    paid_direct: Decimal = Decimal("0.00")
    paid_from_common: Decimal = Decimal("0.00")
    queued: Decimal = Decimal("0.00")

    @property
    def paid(self) -> Decimal:
        return self.paid_direct + self.paid_from_common

    @property
    def remaining(self) -> Decimal:
        if self.status == "cancelled":
            return Decimal("0.00")
        return max(self.price - self.paid, Decimal("0.00"))

@dataclass
class ContractReport:
    contract_id: str
    contractor: str
    amount: Decimal
    works: list[WorkReport] = field(default_factory=list)
    common_paid: Decimal = Decimal("0.00")      # нераспределённый остаток
    common_queued: Decimal = Decimal("0.00")

    @property
    def total_paid(self) -> Decimal:
        return sum((w.paid for w in self.works), Decimal("0.00")) + self.common_paid

    @property
    def total_remaining(self) -> Decimal:
        return sum((w.remaining for w in self.works), Decimal("0.00"))

    @property
    def total_queued(self) -> Decimal:
        return sum((w.queued for w in self.works), Decimal("0.00")) + self.common_queued

# ---------- Построение отчёта ----------

def build_report(contracts, works, payments, as_of: date) -> list[ContractReport]:
    reports: dict[str, ContractReport] = {}
    for c in contracts:
        reports[c.contract_id] = ContractReport(
            contract_id=c.contract_id, contractor=c.contractor, amount=c.amount
        )

    # 1. Работы, релевантные на дату
    works_by_contract: dict[str, list[Work]] = {}
    for w in works:
        if not work_relevant_on(w, as_of):
            continue
        works_by_contract.setdefault(w.contract_id, []).append(w)

    for cid, ws in works_by_contract.items():
        cr = reports.get(cid)
        if cr is None:
            continue
        for w in sorted(ws, key=lambda x: x.work_id):
            cr.works.append(WorkReport(
                work_id=w.work_id, contract_id=w.contract_id, title=w.title,
                status=w.status, price=w.price,
            ))

    # 2. Прямые платежи к работам
    work_index: dict[str, WorkReport] = {}
    for cr in reports.values():
        for wr in cr.works:
            work_index[wr.work_id] = wr

    # 3. Платежи без привязки — копим по договорам
    common_paid: dict[str, Decimal] = {}
    common_queued: dict[str, Decimal] = {}

    for p in payments:
        if p.contract_id not in reports:
            continue
        if p.work_id and p.work_id in work_index:
            wr = work_index[p.work_id]
            if payment_is_paid_on(p, as_of):
                wr.paid_direct += p.amount
            elif payment_is_queued_on(p, as_of):
                wr.queued += p.amount
        else:
            if payment_is_paid_on(p, as_of):
                common_paid[p.contract_id] = common_paid.get(p.contract_id, Decimal("0.00")) + p.amount
            elif payment_is_queued_on(p, as_of):
                common_queued[p.contract_id] = common_queued.get(p.contract_id, Decimal("0.00")) + p.amount

    # 4. FIFO-распределение общих платежей по работам
    for cid, cr in reports.items():
        pool = common_paid.get(cid, Decimal("0.00"))
        for wr in cr.works:  # уже отсортированы по work_id
            if pool <= 0:
                break
            if wr.status == "cancelled":
                continue
            need = max(wr.price - wr.paid_direct, Decimal("0.00"))
            if need <= 0:
                continue
            take = min(need, pool)
            wr.paid_from_common = take
            pool -= take
        cr.common_paid = pool  
        cr.common_queued = common_queued.get(cid, Decimal("0.00"))

    return [reports[c.contract_id] for c in contracts if c.contract_id in reports]

# ---------- Вывод ----------

def print_table(reports: list[ContractReport], as_of: date) -> None:
    print(f"\n=== Отчёт по договорам на {as_of.isoformat()} ===\n")
    grand_paid = Decimal("0.00")
    grand_remaining = Decimal("0.00")
    grand_queued = Decimal("0.00")
    for cr in reports:
        print(f"Договор {cr.contract_id} — {cr.contractor} — сумма {fmt(cr.amount)} ₽")
        if not cr.works:
            print("  (работ нет)\n")
            continue
        print(f"  {'Работа':<32} {'Статус':<10} {'Цена':>12} {'Выплачено':>12} {'Осталось':>12} {'В очереди':>12}")
        for wr in cr.works:
            print(f"  {wr.work_id + ' ' + wr.title[:25]:<32} {wr.status:<10} "
                  f"{fmt(wr.price):>12} {fmt(wr.paid):>12} {fmt(wr.remaining):>12} {fmt(wr.queued):>12}")
        print(f"  {'ИТОГО по договору':<32} {'':<10} "
              f"{fmt(cr.amount):>12} {fmt(cr.total_paid):>12} {fmt(cr.total_remaining):>12} {fmt(cr.total_queued):>12}")
        if cr.common_paid > 0:
            print(f"  (нераспределённый аванс/переплата: {fmt(cr.common_paid)} ₽)")
        print()
        grand_paid += cr.total_paid
        grand_remaining += cr.total_remaining
        grand_queued += cr.total_queued
    print(f"=== ВСЕГО: выплачено {fmt(grand_paid)} ₽, осталось {fmt(grand_remaining)} ₽, в очереди {fmt(grand_queued)} ₽ ===\n")

def to_dict(reports: list[ContractReport], as_of: date) -> dict:
    return {
        "as_of": as_of.isoformat(),
        "contracts": [
            {
                "contract_id": cr.contract_id,
                "contractor": cr.contractor,
                "amount": fmt(cr.amount),
                "total_paid": fmt(cr.total_paid),
                "total_remaining": fmt(cr.total_remaining),
                "total_queued": fmt(cr.total_queued),
                "common_paid": fmt(cr.common_paid),
                "common_queued": fmt(cr.common_queued),
                "works": [
                    {
                        "work_id": wr.work_id,
                        "title": wr.title,
                        "status": wr.status,
                        "price": fmt(wr.price),
                        "paid": fmt(wr.paid),
                        "paid_direct": fmt(wr.paid_direct),
                        "paid_from_common": fmt(wr.paid_from_common),
                        "remaining": fmt(wr.remaining),
                        "queued": fmt(wr.queued),
                    }
                    for wr in cr.works
                ],
            }
            for cr in reports
        ],
    }

def write_csv(reports: list[ContractReport], out: Path) -> None:
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["contract_id", "contractor", "work_id", "title", "status",
                    "price", "paid", "paid_direct", "paid_from_common", "remaining", "queued"])
        for cr in reports:
            if not cr.works:
                w.writerow([cr.contract_id, cr.contractor, "", "", "", "", "", "", "", "", ""])
            for wr in cr.works:
                w.writerow([cr.contract_id, cr.contractor, wr.work_id, wr.title, wr.status,
                            fmt(wr.price), fmt(wr.paid), fmt(wr.paid_direct),
                            fmt(wr.paid_from_common), fmt(wr.remaining), fmt(wr.queued)])
            w.writerow([cr.contract_id, cr.contractor, "ИТОГО", "", "",
                        fmt(cr.amount), fmt(cr.total_paid), "", "",
                        fmt(cr.total_remaining), fmt(cr.total_queued)])

def main() -> int:
    ap = argparse.ArgumentParser(description="Отчёт «аванс по договору» на дату")
    ap.add_argument("--data-dir", default="data", help="папка с CSV")
    ap.add_argument("--as-of", required=True, help="дата отчёта, YYYY-MM-DD")
    ap.add_argument("--format", choices=["table", "csv", "json"], default="table")
    ap.add_argument("--out", help="файл для csv/json")
    args = ap.parse_args()

    as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date()
    data_dir = Path(args.data_dir)

    contracts = load_contracts(data_dir)
    works = load_works(data_dir)
    payments = load_payments(data_dir)
    reports = build_report(contracts, works, payments, as_of)

    if args.format == "table":
        print_table(reports, as_of)
    elif args.format == "json":
        text = json.dumps(to_dict(reports, as_of), ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text)
    elif args.format == "csv":
        if not args.out:
            print("--out обязателен для csv", file=sys.stderr)
            return 2
        write_csv(reports, Path(args.out))
        print(f"CSV записан: {args.out}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
