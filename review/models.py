"""Модель данных рабочего места проверяющего.

Задача на проверку — это разбор одного входящего письма: что извлекла модель,
насколько она была уверена в каждом поле и что с этим сделал человек.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Status(str, Enum):
    PENDING = "pending"      # ждёт проверки
    APPROVED = "approved"    # проверено, принято
    CORRECTED = "corrected"  # проверено, поля исправлены
    REJECTED = "rejected"    # разбор негоден целиком


class Flag(str, Enum):
    LOW_CONFIDENCE = "low_confidence"    # модель не уверена в поле
    NOT_IN_CATALOG = "not_in_catalog"    # позиции нет в справочнике
    AMBIGUOUS_UNIT = "ambiguous_unit"    # единица измерения допускает два чтения
    DUPLICATE = "duplicate"              # позиция повторяется в письме


# Порог, ниже которого поле считается неуверенным и подсвечивается оператору.
LOW_CONFIDENCE = 0.75


@dataclass
class Field_:
    """Значение поля вместе с уверенностью модели и правкой человека."""

    value: Any
    confidence: float = 1.0
    corrected: Any = None

    @property
    def final(self) -> Any:
        """Что уходит дальше: правка человека, если она была."""
        return self.value if self.corrected is None else self.corrected

    @property
    def edited(self) -> bool:
        return self.corrected is not None and self.corrected != self.value

    @property
    def uncertain(self) -> bool:
        return self.confidence < LOW_CONFIDENCE


@dataclass
class Item:
    """Одна позиция заказа."""

    name: Field_
    qty: Field_
    unit: Field_
    flags: list[Flag] = field(default_factory=list)

    @property
    def min_confidence(self) -> float:
        return min(f.confidence for f in (self.name, self.qty, self.unit))

    @property
    def needs_attention(self) -> bool:
        return bool(self.flags) or any(f.uncertain for f in (self.name, self.qty, self.unit))

    def final(self) -> dict[str, Any]:
        return {"name": self.name.final, "qty": self.qty.final, "unit": self.unit.final}

    def edits(self) -> dict[str, Any]:
        return {n: getattr(self, n).corrected for n in ("name", "qty", "unit") if getattr(self, n).edited}


@dataclass
class Task:
    """Разбор одного письма, ожидающий проверки."""

    id: str
    source: str                      # текст письма
    received_at: str
    customer: str
    items: list[Item]
    status: Status = Status.PENDING
    note: str = ""                   # комментарий оператора при отклонении
    reviewed_at: str | None = None
    reviewed_by: str | None = None
    model: str = ""

    @property
    def min_confidence(self) -> float:
        return min((i.min_confidence for i in self.items), default=1.0)

    @property
    def flags(self) -> list[Flag]:
        seen: list[Flag] = []
        for item in self.items:
            for f in item.flags:
                if f not in seen:
                    seen.append(f)
        return seen

    @property
    def needs_attention(self) -> bool:
        return any(i.needs_attention for i in self.items)

    def mark_reviewed(self, status: Status, who: str, note: str = "") -> None:
        self.status = status
        self.note = note
        self.reviewed_by = who
        self.reviewed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["min_confidence"] = round(self.min_confidence, 3)
        d["needs_attention"] = self.needs_attention
        d["flags"] = [f.value for f in self.flags]
        for raw_item, item in zip(d["items"], self.items):
            raw_item["flags"] = [f.value for f in item.flags]
            raw_item["needs_attention"] = item.needs_attention
            for name in ("name", "qty", "unit"):
                raw_item[name]["final"] = getattr(item, name).final
                raw_item[name]["edited"] = getattr(item, name).edited
                raw_item[name]["uncertain"] = getattr(item, name).uncertain
        return d
