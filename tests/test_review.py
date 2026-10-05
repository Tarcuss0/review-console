import json

import pytest
from fastapi.testclient import TestClient

from review.app import create_app
from review.models import Field_, Flag, Item, Status, Task
from review.store import Store, task_from_dict


def field(value, confidence=1.0):
    return Field_(value=value, confidence=confidence)


def item(name="труба", qty=3, unit="шт", conf=1.0, flags=()):
    return Item(name=field(name, conf), qty=field(qty, conf), unit=field(unit, conf), flags=list(flags))


def task(tid="t1", items=None, source="письмо"):
    return Task(id=tid, source=source, received_at="2026-10-05 10:00", customer="ООО Тест", items=items or [item()])


@pytest.fixture
def client():
    store = Store(
        [
            task("low", [item(conf=0.4, flags=[Flag.LOW_CONFIDENCE])]),
            task("high", [item(conf=0.98)]),
            task("mid", [item(conf=0.8), item(conf=0.6, flags=[Flag.AMBIGUOUS_UNIT])]),
        ]
    )
    return TestClient(create_app(store))


# --- модель ------------------------------------------------------------------


def test_final_prefers_the_human_correction():
    f = Field_(value="труба", confidence=0.5, corrected="труба медная")
    assert f.final == "труба медная" and f.edited


def test_correction_equal_to_the_original_is_not_an_edit():
    assert not Field_(value="труба", corrected="труба").edited


def test_zero_correction_is_still_a_correction():
    """Ноль — осмысленная правка количества, его нельзя спутать с пустотой."""
    f = Field_(value=5, confidence=0.4, corrected=0)
    assert f.final == 0 and f.edited


def test_confidence_below_threshold_is_uncertain():
    assert Field_(value="x", confidence=0.74).uncertain
    assert not Field_(value="x", confidence=0.75).uncertain


def test_item_confidence_is_the_weakest_field():
    i = Item(name=field("труба", 0.9), qty=field(3, 0.4), unit=field("шт", 0.99))
    assert i.min_confidence == pytest.approx(0.4) and i.needs_attention


def test_flag_alone_raises_attention():
    i = item(conf=0.99, flags=[Flag.DUPLICATE])
    assert i.needs_attention


def test_task_collects_flags_without_duplicates():
    t = task(items=[item(flags=[Flag.DUPLICATE]), item(flags=[Flag.DUPLICATE, Flag.NOT_IN_CATALOG])])
    assert t.flags == [Flag.DUPLICATE, Flag.NOT_IN_CATALOG]


# --- очередь -----------------------------------------------------------------


def test_queue_puts_the_least_certain_first(client):
    ids = [t["id"] for t in client.get("/api/tasks").json()["tasks"]]
    assert ids == ["low", "mid", "high"]


def test_attention_filter_hides_the_clean_ones(client):
    ids = [t["id"] for t in client.get("/api/tasks?attention=true").json()["tasks"]]
    assert ids == ["low", "mid"]


def test_unknown_status_is_rejected(client):
    assert client.get("/api/tasks?status=нет-такого").status_code == 400


def test_missing_task_is_404(client):
    assert client.get("/api/tasks/нет").status_code == 404


# --- решения -----------------------------------------------------------------


def test_approve_without_edits_marks_approved(client):
    body = client.post("/api/tasks/high/decision", json={"action": "approve"}).json()
    assert body["status"] == "approved"


def test_approve_with_edits_marks_corrected(client):
    body = client.post(
        "/api/tasks/low/decision",
        json={"action": "approve", "edits": [{"item": 0, "field": "qty", "value": 7}]},
    ).json()
    assert body["status"] == "corrected"
    assert body["items"][0]["qty"]["final"] == 7
    assert body["items"][0]["qty"]["edited"] is True


def test_edit_keeps_what_the_model_said(client):
    """Исходное значение модели не затирается — иначе нечего сравнивать потом."""
    body = client.post(
        "/api/tasks/low/decision",
        json={"action": "approve", "edits": [{"item": 0, "field": "name", "value": "труба медная"}]},
    ).json()
    assert body["items"][0]["name"]["value"] == "труба"
    assert body["items"][0]["name"]["corrected"] == "труба медная"


def test_reject_requires_a_note(client):
    assert client.post("/api/tasks/low/decision", json={"action": "reject"}).status_code == 400


def test_reject_with_note_is_stored(client):
    body = client.post("/api/tasks/low/decision", json={"action": "reject", "note": "нет позиций"}).json()
    assert body["status"] == "rejected" and body["note"] == "нет позиций"


def test_second_decision_is_refused(client):
    client.post("/api/tasks/high/decision", json={"action": "approve"})
    assert client.post("/api/tasks/high/decision", json={"action": "approve"}).status_code == 409


def test_unknown_action_is_refused(client):
    assert client.post("/api/tasks/high/decision", json={"action": "удалить"}).status_code == 400


def test_edit_of_a_missing_item_is_refused(client):
    r = client.post(
        "/api/tasks/high/decision",
        json={"action": "approve", "edits": [{"item": 5, "field": "qty", "value": 1}]},
    )
    assert r.status_code == 400


def test_only_known_fields_can_be_edited(client):
    r = client.post(
        "/api/tasks/high/decision",
        json={"action": "approve", "edits": [{"item": 0, "field": "price", "value": 100}]},
    )
    assert r.status_code == 400


# --- выгрузка набора ---------------------------------------------------------


def test_dataset_contains_only_reviewed(client):
    client.post("/api/tasks/high/decision", json={"action": "approve"})
    rows = client.get("/api/dataset").json()["rows"]
    assert [r["id"] for r in rows] == ["high"]


def test_rejected_never_reaches_the_dataset(client):
    client.post("/api/tasks/low/decision", json={"action": "reject", "note": "мусор"})
    assert client.get("/api/dataset").json()["rows"] == []


def test_dataset_carries_the_corrected_values(client):
    client.post(
        "/api/tasks/low/decision",
        json={"action": "approve", "edits": [{"item": 0, "field": "qty", "value": 7}]},
    )
    row = client.get("/api/dataset").json()["rows"][0]
    assert row["expected"]["items"][0]["qty"] == 7
    assert row["meta"]["was_corrected"] is True


def test_dataset_jsonl_is_one_object_per_line(client):
    client.post("/api/tasks/high/decision", json={"action": "approve"})
    client.post("/api/tasks/mid/decision", json={"action": "approve"})
    lines = client.get("/api/dataset").json()["jsonl"].splitlines()
    assert len(lines) == 2
    assert all(json.loads(line)["id"] for line in lines)


# --- сводка ------------------------------------------------------------------


def test_stats_count_field_level_errors(client):
    client.post(
        "/api/tasks/low/decision",
        json={"action": "approve", "edits": [{"item": 0, "field": "qty", "value": 7}]},
    )
    stats = client.get("/api/tasks").json()["stats"]
    assert stats["reviewed"] == 1
    assert stats["correction_rate"] == 1.0
    assert stats["field_error_rate"] == pytest.approx(1 / 3)


def test_stats_are_zero_before_any_review(client):
    stats = client.get("/api/tasks").json()["stats"]
    assert stats["reviewed"] == 0 and stats["field_error_rate"] == 0.0


def test_counts_track_the_queue(client):
    client.post("/api/tasks/low/decision", json={"action": "reject", "note": "мусор"})
    counts = client.get("/api/tasks").json()["counts"]
    assert counts["pending"] == 2 and counts["rejected"] == 1 and counts["attention"] == 1


# --- загрузка ----------------------------------------------------------------


def test_fixtures_load(tmp_path):
    store = Store.from_jsonl("fixtures/tasks.jsonl")
    assert store.counts()["total"] == 10
    assert store.list(only_attention=True)[0].min_confidence < 0.5


def test_broken_line_names_the_line_number(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"id": "a", "items": []}\nне json\n', encoding="utf-8")
    with pytest.raises(ValueError, match=":2"):
        Store.from_jsonl(p)


def test_missing_confidence_defaults_to_certain():
    t = task_from_dict({"id": "x", "items": [{"name": {"value": "труба"}, "qty": {"value": 1}, "unit": {"value": "шт"}}]})
    assert t.min_confidence == 1.0 and not t.needs_attention


def test_index_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Проверка разборов" in r.text
