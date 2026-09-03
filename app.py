import os
import sqlite3
from datetime import datetime, timezone, timedelta
import secrets
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, abort

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")
DB_PATH = os.environ.get("DB_PATH", "feedback.db")
TEACHER_PASSWORD = os.environ.get("TEACHER_PASSWORD", "1234")
KST = timezone(timedelta(hours=9))


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            presenter_number TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS students (
            student_number TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            session_token TEXT,
            last_seen INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_number TEXT NOT NULL,
            sender_name TEXT NOT NULL,
            presenter_number TEXT NOT NULL,
            presenter_name TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("INSERT OR IGNORE INTO settings (id, presenter_number) VALUES (1, '')")
    conn.commit()
    conn.close()


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_kst(value):
    """DB의 UTC 시간을 한국 시간(Asia/Seoul)으로 표시."""
    if not value:
        return ""
    try:
        text = str(value)
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text[:-1] + "+00:00")
        elif "T" in text:
            dt = datetime.fromisoformat(text)
        else:
            # 기존 SQLite CURRENT_TIMESTAMP 형식은 UTC
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(value)


app.jinja_env.filters["kst"] = format_kst


def get_current_presenter():
    conn = get_db()
    row = conn.execute("""
        SELECT s.student_number, s.name
        FROM settings st
        LEFT JOIN students s ON s.student_number = st.presenter_number
        WHERE st.id = 1
    """).fetchone()
    conn.close()
    if row and row["student_number"]:
        return {"number": row["student_number"], "name": row["name"]}
    return None


def current_presenter_setting():
    conn = get_db()
    row = conn.execute("SELECT presenter_number FROM settings WHERE id=1").fetchone()
    conn.close()
    return row["presenter_number"] if row else ""


def touch_student():
    """학생의 마지막 접속 시간과 현재 접속 토큰을 확인하고 기록."""
    if not session.get("student_number"):
        return False
    student_number = session["student_number"]
    conn = get_db()
    row = conn.execute(
        "SELECT student_number, name, session_token FROM students WHERE student_number=?",
        (student_number,)
    ).fetchone()
    if not row or not session.get("student_token") or row["session_token"] != session.get("student_token"):
        conn.close()
        session.clear()
        return False
    conn.execute(
        "UPDATE students SET last_seen=? WHERE student_number=?",
        (int(datetime.now(timezone.utc).timestamp()), student_number)
    )
    conn.commit()
    conn.close()
    session["student_name"] = row["name"]
    return True


def student_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not touch_student():
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def teacher_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("teacher"):
            return redirect(url_for("teacher_login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/")
def index():
    if session.get("teacher"):
        return redirect(url_for("teacher"))
    if session.get("student_number"):
        if touch_student():
            return redirect(url_for("student"))
    return render_template("lobby.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        student_number = request.form.get("student_number", "").strip()
        name = request.form.get("name", "").strip()

        if not student_number or not name:
            flash("학번과 이름을 모두 입력해 주세요.")
            return render_template("login.html")
        if not student_number.isdigit():
            flash("학번은 숫자로 입력해 주세요.")
            return render_template("login.html")
        if len(student_number) > 20:
            flash("학번이 너무 깁니다.")
            return render_template("login.html")
        if len(name) > 30:
            flash("이름은 30자 이하로 입력해 주세요.")
            return render_template("login.html")

        now = int(datetime.now(timezone.utc).timestamp())
        conn = get_db()
        existing = conn.execute(
            "SELECT student_number FROM students WHERE student_number=?",
            (student_number,)
        ).fetchone()

        # 학생 정보와 코멘트는 영구 보존한다. 로그인할 때마다 새 접속 토큰을 발급해
        # 선생님이 이전 접속을 강제로 종료할 수 있게 한다.
        session_token = secrets.token_urlsafe(32)
        if existing:
            conn.execute(
                "UPDATE students SET name=?, session_token=?, last_seen=? WHERE student_number=?",
                (name, session_token, now, student_number)
            )
        else:
            conn.execute(
                "INSERT INTO students (student_number, name, session_token, last_seen) VALUES (?, ?, ?, ?)",
                (student_number, name, session_token, now)
            )
        conn.commit()
        conn.close()

        session.clear()
        session["student_number"] = student_number
        session["student_name"] = name
        session["student_token"] = session_token
        return redirect(url_for("student"))

    return render_template("login.html")


@app.route("/student")
@student_required
def student():
    presenter = get_current_presenter()
    return render_template(
        "student.html",
        student_number=session["student_number"],
        student_name=session["student_name"],
        presenter=presenter
    )


@app.route("/api/comments", methods=["POST"])
@student_required
def add_comment():
    presenter = get_current_presenter()
    if not presenter:
        return jsonify(ok=False, message="현재 발표자가 설정되지 않았습니다."), 400
    if session["student_number"] == presenter["number"]:
        return jsonify(ok=False, message="현재 발표자 본인은 코멘트를 보낼 수 없습니다."), 403

    content = request.form.get("content", "").strip()
    if not content:
        return jsonify(ok=False, message="코멘트를 입력해 주세요."), 400
    if len(content) > 500:
        return jsonify(ok=False, message="코멘트는 500자 이하로 입력해 주세요."), 400

    conn = get_db()
    conn.execute(
        """INSERT INTO comments
           (sender_number, sender_name, presenter_number, presenter_name, content, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session["student_number"], session["student_name"], presenter["number"], presenter["name"], content, utc_now_iso())
    )
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.route("/presenter")
@student_required
def presenter():
    presenter_number = session["student_number"]
    conn = get_db()
    comments = conn.execute(
        """SELECT id, sender_number, sender_name, content, created_at
           FROM comments WHERE presenter_number=? ORDER BY id DESC""",
        (presenter_number,)
    ).fetchall()
    conn.close()
    return render_template(
        "presenter.html",
        student_name=session["student_name"],
        student_number=presenter_number,
        comments=comments
    )


@app.route("/api/presenter-comments")
@student_required
def presenter_comments():
    presenter_number = session["student_number"]
    conn = get_db()
    comments = conn.execute(
        """SELECT id, sender_number, sender_name, content, created_at
           FROM comments WHERE presenter_number=? ORDER BY id DESC""",
        (presenter_number,)
    ).fetchall()
    conn.close()
    return jsonify([
        {
            "id": c["id"],
            "sender": f'{c["sender_number"]} {c["sender_name"]}',
            "content": c["content"],
            "created_at": format_kst(c["created_at"])
        } for c in comments
    ])


@app.route("/teacher/login", methods=["GET", "POST"])
def teacher_login():
    if request.method == "POST":
        if request.form.get("password", "") == TEACHER_PASSWORD:
            session.clear()
            session["teacher"] = True
            return redirect(url_for("teacher"))
        flash("비밀번호가 맞지 않습니다.")
    return render_template("teacher_login.html")


@app.route("/teacher")
@teacher_required
def teacher():
    conn = get_db()
    students = conn.execute("""
        SELECT s.student_number, s.name, COUNT(c.id) AS comment_count
        FROM students s
        LEFT JOIN comments c ON c.presenter_number = s.student_number
        GROUP BY s.student_number, s.name
        ORDER BY s.student_number
    """).fetchall()
    comments = conn.execute(
        """SELECT id, sender_number, sender_name, presenter_number, presenter_name, content, created_at
           FROM comments ORDER BY id DESC"""
    ).fetchall()
    conn.close()
    return render_template(
        "teacher.html",
        presenter=get_current_presenter(),
        students=students,
        comments=comments
    )


@app.route("/teacher/set-presenter", methods=["POST"])
@teacher_required
def set_presenter():
    presenter_number = request.form.get("presenter_number", "").strip()
    conn = get_db()
    if presenter_number:
        student = conn.execute(
            "SELECT student_number, name FROM students WHERE student_number=?",
            (presenter_number,)
        ).fetchone()
        if not student:
            conn.close()
            flash("등록된 학생의 학번을 선택해 주세요.")
            return redirect(url_for("teacher"))
        conn.execute("UPDATE settings SET presenter_number=? WHERE id=1", (presenter_number,))
        conn.commit()
        conn.close()
        flash(f"{student['student_number']} {student['name']} 학생을 발표자로 설정했습니다.")
    else:
        conn.execute("UPDATE settings SET presenter_number='' WHERE id=1")
        conn.commit()
        conn.close()
        flash("현재 발표자를 해제했습니다.")
    return redirect(url_for("teacher"))


@app.route("/teacher/student/<student_number>")
@teacher_required
def teacher_student_detail(student_number):
    conn = get_db()
    student = conn.execute(
        "SELECT student_number, name FROM students WHERE student_number=?",
        (student_number,)
    ).fetchone()
    if not student:
        conn.close()
        abort(404)
    comments = conn.execute(
        """SELECT id, sender_number, sender_name, presenter_number, presenter_name, content, created_at
           FROM comments WHERE presenter_number=? ORDER BY id DESC""",
        (student_number,)
    ).fetchall()
    conn.close()
    return render_template("teacher_student.html", student=student, comments=comments)


@app.route("/teacher/student/<student_number>/force-logout", methods=["POST"])
@teacher_required
def force_logout_student(student_number):
    conn = get_db()
    student = conn.execute(
        "SELECT student_number, name FROM students WHERE student_number=?",
        (student_number,)
    ).fetchone()
    if not student:
        conn.close()
        abort(404)
    # 접속 토큰을 폐기하면 해당 학생의 현재 브라우저 세션이 다음 요청에서 즉시 무효화된다.
    conn.execute("UPDATE students SET session_token=NULL WHERE student_number=?", (student_number,))
    conn.commit()
    conn.close()
    flash(f"{student['student_number']} {student['name']} 학생의 접속을 강제로 종료했습니다.")
    return redirect(url_for("teacher"))


@app.route("/teacher/clear-comments", methods=["POST"])
@teacher_required
def clear_comments():
    conn = get_db()
    conn.execute("DELETE FROM comments")
    conn.commit()
    conn.close()
    flash("모든 코멘트를 삭제했습니다.")
    return redirect(url_for("teacher"))


@app.route("/teacher/comments")
@teacher_required
def teacher_comments():
    conn = get_db()
    comments = conn.execute(
        """SELECT id, sender_number, sender_name, presenter_number, presenter_name, content, created_at
           FROM comments ORDER BY id DESC"""
    ).fetchall()
    conn.close()
    return jsonify([
        {
            "id": c["id"],
            "sender": f'{c["sender_number"]} {c["sender_name"]}',
            "presenter": f'{c["presenter_number"]} {c["presenter_name"]}',
            "content": c["content"],
            "created_at": format_kst(c["created_at"])
        } for c in comments
    ])


init_db()


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, debug=False)
