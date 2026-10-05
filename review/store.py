"""Хранилище задач.

Для демонстрации — в памяти, с загрузкой из jsonl. Интерфейс намеренно узкий:
подменить на базу — значит переписать один класс.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .models import Field_, Flag, Item, Status, Task


def _field(raw: dict | None) -> Field_:
    raw = raw or {}
    return Field_(value=raw.get("value"), confidence=float(raw.get("confidence", 1.0)), corrected=raw.get("corrected"))


def task_from_dict(raw: dict) -> Task:
    items = [
        Item(
            name=_field(i.get("name")),
            qty=_field(i.get("qty")),
            unit=_field(i.get("unit")),
            flags=[Flag(f) for f in i.get("flags", [])],
        )
        for i in raw.get("items", [])
    ]
    return Task(
        id=str(raw["id"]),
        source=raw.get("source", ""),
        received_at=raw.get("received_at", ""),
        customer=raw.get("customer", ""),
        items=items,
        status=Status(raw.get("status", "pending")),
        note=raw.get("note", ""),
        model=raw.get("model", ""),
    )


class Store:
    def __init__(self, tasks: Iterable[Task] = ()) -> None:
        self._tasks: dict[str, Task] = {t.id: t for t in tasks}

    @classmethod
    def from_jsonl(cls, path: str | Path) -> "Store":
        path = Path(path)
        tasks = []
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                tasks.append(task_from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError) as e:
                raise ValueError(f"{path}:{lineno} — {e}") from e
        return cls(tasks)

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def list(self, status: Status | None = None, only_attention: bool = False) -> list[Task]:
        """Очередь: сначала то, в чём модель менее уверена."""
        tasks = list(self._tasks.values())
        if status is not None:
            tasks = [t for t in tasks if t.status == status]
        if only_attention:
            tasks = [t for t in tasks if t.needs_attention]
        return sorted(tasks, key=lambda t: (t.min_confidence, t.id))

    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in Status}
        for t in self._tasks.values():
            out[t.status.value] += 1
        out["attention"] = sum(1 for t in self._tasks.values() if t.status == Status.PENDING and t.needs_attention)
        out["total"] = len(self._tasks)
        return out

    def stats(self) -> dict[str, float]:
        """Сводка для шапки: сколько проверено и сколько правили руками."""
        reviewed = [t for t in self._tasks.values() if t.status != Status.PENDING]
        corrected = [t for t in reviewed if t.status == Status.CORRECTED]
        rejected = [t for t in reviewed if t.status == Status.REJECTED]
        edited_fields = sum(len(i.edits()) for t in reviewed for i in t.items)
        total_fields = sum(len(t.items) * 3 for t in reviewed)
        return {
            "reviewed": len(reviewed),
            "correction_rate": len(corrected) / len(reviewed) if reviewed else 0.0,
            "rejection_rate": len(rejected) / len(reviewed) if reviewed else 0.0,
            "field_error_rate": edited_fields / total_fields if total_fields else 0.0,
        }

    def export_dataset(self) -> list[dict]:
        """Проверенные разборы в виде набора для прогона качества.

        Отклонённые не попадают: по ним нет эталона, только пометка оператора.
        """
        out = []
        for task in sorted(self._tasks.values(), key=lambda t: t.id):
            if task.status not in (Status.APPROVED, Status.CORRECTED):
                continue
            out.append(
                {
                    "id": task.id,
                    "input": task.source,
                    "expected": {"items": [i.final() for i in task.items]},
                    "meta": {
                        "reviewed_by": task.reviewed_by,
                        "reviewed_at": task.reviewed_at,
                        "was_corrected": task.status == Status.CORRECTED,
                    },
                }
            )
        return out
