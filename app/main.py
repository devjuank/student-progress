import logging

from fastapi import FastAPI, HTTPException

from app import store
from app.models import ProgressRecord
from app.privacy import redact

logger = logging.getLogger("student-progress")
logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

app = FastAPI(title="student-progress")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/lessons")
def list_lessons():
    return [lesson.__dict__ for lesson in store.LESSONS]


def _get_student(student_id: str):
    student = store.STUDENTS.get(student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="student not found")
    return student


@app.get("/students/{student_id}/progress")
def get_progress(student_id: str):
    student = _get_student(student_id)
    records = [r for r in store.PROGRESS if r.student_id == student_id]
    completed = sum(1 for r in records if r.completed)
    total = len(store.LESSONS)
    percentage = round(100 * completed / total) if total else 0
    return {
        "student_id": student.id,
        "completed": completed,
        "total": total,
        "percentage": percentage,
    }


@app.post("/students/{student_id}/progress")
def record_progress(student_id: str, payload: dict):
    student = _get_student(student_id)
    record = ProgressRecord(
        student_id=student.id,
        lesson_id=payload["lesson_id"],
        completed=True,
        score=int(payload.get("score", 0)),
    )
    store.PROGRESS.append(record)
    logger.info("progress recorded %s", redact({"student_id": student.id, "lesson_id": record.lesson_id}))
    return {"ok": True}


@app.get("/leaderboard")
def leaderboard(limit: int = 10):
    """Top students by average score, for the weekly ranking shown in the app."""
    if limit < 1 or limit > 50:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 50")
    rows = []
    for student in store.STUDENTS.values():
        scores = [r.score for r in store.PROGRESS if r.student_id == student.id]
        try:
            average = sum(scores) / len(scores)
        except Exception:
            continue
        rows.append({
            "name": student.full_name,
            "country": student.country,
            "average_score": round(average, 1),
        })
    rows.sort(key=lambda row: row["average_score"], reverse=True)
    return rows[:limit]
