"""Рабочее место проверяющего: HTTP-слой.

Оператор видит очередь, открывает разбор, правит поля и отправляет решение.
Правки копятся в тех же задачах и выгружаются набором для прогона качества.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .models import Status
from .store import Store

BASE = Path(__file__).parent
FIXTURES = BASE.parent / "fixtures" / "tasks.jsonl"


class FieldEdit(BaseModel):
    item: int = Field(ge=0, description="порядковый номер позиции в разборе")
    field: str = Field(description="name, qty или unit")
    value: Any


class Decision(BaseModel):
    action: str = Field(description="approve или reject")
    edits: list[FieldEdit] = Field(default_factory=list)
    note: str = ""
    operator: str = "оператор"


def create_app(store: Store | None = None) -> FastAPI:
    app = FastAPI(title="Проверка разборов", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.store = store or Store.from_jsonl(FIXTURES)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (BASE / "static" / "index.html").read_text(encoding="utf-8")

    @app.get("/api/tasks")
    def list_tasks(status: str | None = None, attention: bool = False) -> dict:
        s = app.state.store
        try:
            parsed = Status(status) if status else None
        except ValueError:
            raise HTTPException(400, f"неизвестный статус: {status}")
        tasks = s.list(status=parsed, only_attention=attention)
        return {
            "counts": s.counts(),
            "stats": s.stats(),
            "tasks": [
                {
                    "id": t.id,
                    "customer": t.customer,
                    "received_at": t.received_at,
                    "status": t.status.value,
                    "items": len(t.items),
                    "min_confidence": round(t.min_confidence, 3),
                    "needs_attention": t.needs_attention,
                    "flags": [f.value for f in t.flags],
                }
                for t in tasks
            ],
        }

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> dict:
        task = app.state.store.get(task_id)
        if task is None:
            raise HTTPException(404, "разбор не найден")
        return task.to_dict()

    @app.post("/api/tasks/{task_id}/decision")
    def decide(task_id: str, decision: Decision) -> dict:
        task = app.state.store.get(task_id)
        if task is None:
            raise HTTPException(404, "разбор не найден")
        if task.status != Status.PENDING:
            raise HTTPException(409, "разбор уже проверен")

        if decision.action == "reject":
            if not decision.note.strip():
                raise HTTPException(400, "при отклонении нужен комментарий")
            task.mark_reviewed(Status.REJECTED, decision.operator, decision.note)
            return task.to_dict()

        if decision.action != "approve":
            raise HTTPException(400, "действие должно быть approve или reject")

        for edit in decision.edits:
            if edit.item >= len(task.items):
                raise HTTPException(400, f"позиции {edit.item} нет в разборе")
            if edit.field not in ("name", "qty", "unit"):
                raise HTTPException(400, f"нельзя править поле {edit.field}")
            getattr(task.items[edit.item], edit.field).corrected = edit.value

        changed = any(i.edits() for i in task.items)
        task.mark_reviewed(Status.CORRECTED if changed else Status.APPROVED, decision.operator, decision.note)
        return task.to_dict()

    @app.get("/api/dataset")
    def dataset() -> JSONResponse:
        """Проверенные разборы в формате набора для прогона качества."""
        rows = app.state.store.export_dataset()
        jsonl = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
        return JSONResponse(content={"rows": rows, "jsonl": jsonl}, headers={"X-Rows": str(len(rows))})

    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    return app


app = create_app()
