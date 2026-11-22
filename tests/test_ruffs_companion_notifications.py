"""RUFFS: avisos para la app de Android con la app cerrada.

El teléfono lee /api/companion/notifications: una copia de los avisos de tareas
que NO se consume (la página del PC sigue recibiendo los suyos) y los
recordatorios que vienen, para programar alarmas a su hora."""

from datetime import datetime, timedelta, timezone

from src.task_scheduler import TaskScheduler


def _sched():
    s = TaskScheduler.__new__(TaskScheduler)   # sin arrancar el planificador
    s._pending_notifications = []
    s._companion_feed = []
    s._companion_seq = 0
    return s


def test_feed_is_not_consumed_by_the_pc_and_is_owner_scoped():
    s = _sched()
    s.add_notification("Resumen", "success", task_id="t1", owner="ana", body="Listo")
    s.add_notification("Otro", "success", task_id="t2", owner="beto", body="x")
    s.add_notification("Correo", "error", task_id="t3", owner="ana")
    assert [n["task_id"] for n in s.pop_notifications(owner="ana")] == ["t1", "t3"]   # la página del PC
    seq, feed = s.companion_notifications(owner="ana", after=0)                         # el teléfono
    assert seq == 3 and [n["task_id"] for n in feed] == ["t1", "t3"]
    assert [n["seq"] for n in feed] == [1, 3]
    assert s.companion_notifications(owner="ana", after=3) == (3, [])                   # nada nuevo
    assert [n["task_id"] for n in s.companion_notifications(owner="beto", after=0)[1]] == ["t2"]


def test_feed_is_capped():
    s = _sched()
    for i in range(130):
        s.add_notification(f"T{i}", "success", task_id=str(i), owner="ana", body="b")
    seq, feed = s.companion_notifications(owner="ana", after=0)
    assert seq == 130 and len(feed) == 100 and feed[0]["seq"] == 31


def test_due_dates_to_epoch_ms():
    from companion.routes import due_epoch_ms
    assert due_epoch_ms("2026-11-22T15:30:00Z") == int(datetime(2026, 11, 22, 15, 30, tzinfo=timezone.utc).timestamp() * 1000)
    local = datetime(2026, 11, 22, 9, 0)                    # sin zona = hora del PC
    assert due_epoch_ms("2026-11-22T09:00:00") == int(local.astimezone().timestamp() * 1000)
    assert due_epoch_ms("") is None and due_epoch_ms("mañana") is None


def test_upcoming_reminders_window(monkeypatch):
    import companion.routes as R
    now = datetime.now().astimezone()

    class N:
        def __init__(self, id, owner, due, content="", items=None, archived=False):
            self.id, self.owner, self.due_date, self.content, self.items = id, owner, due, content, items
            self.title, self.repeat, self.archived = f"Nota {id}", "none", archived

    rows = [N("soon", "ana", (now + timedelta(minutes=30)).isoformat(), "Llamar"),
            N("later", "ana", (now + timedelta(days=5)).isoformat()),
            N("past", "ana", (now - timedelta(hours=2)).isoformat()),
            N("other", "beto", (now + timedelta(minutes=10)).isoformat()),
            N("list", None, (now + timedelta(hours=1)).isoformat(), items='[{"text":"pan","done":false},{"text":"x","done":true}]')]

    class Q:
        def filter(self, *a, **k): return self
        def all(self): return rows

    class DB:
        def query(self, *a): return Q()
        def close(self): pass

    import core.database as D
    monkeypatch.setattr(D, "SessionLocal", lambda: DB())
    out = R.upcoming_reminders("ana")
    assert [r["id"] for r in out] == ["soon", "list"]          # ni la de otro dueño, ni pasadas, ni a 5 días
    assert out[0]["body"] == "Llamar" and out[1]["body"] == "pan"
