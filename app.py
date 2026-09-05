import os
import sqlite3
import secrets
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    jsonify,
    abort,
)

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "change-this-secret-key"
)

DB_PATH = os.environ.get("DB_PATH", "feedback.db")

TEACHER_PASSWORD = os.environ.get(
    "TEACHER_PASSWORD",
    "1234"
)

SHEETS_WEBHOOK_URL = os.environ.get(
    "GOOGLE_SHEETS_WEBHOOK_URL",
    ""
).strip()

KST = timezone(timedelta(hours=9))


# =========================================================
# DATABASE
# =========================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK(id=1),
            teacher_password TEXT NOT NULL DEFAULT '1234'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS students (
            student_number TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            password TEXT NOT NULL DEFAULT '1234',
            group_name TEXT NOT NULL DEFAULT '',
            session_token TEXT,
            last_seen INTEGER NOT NULL DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_number TEXT NOT NULL,
            sender_name TEXT NOT NULL,
            target_number TEXT NOT NULL,
            target_name TEXT NOT NULL,
            score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 5),
            content TEXT NOT NULL,
            reply TEXT NOT NULL DEFAULT '',
            evaluation_type TEXT NOT NULL DEFAULT 'peer',
            created_at TEXT NOT NULL,
            cloud_id TEXT UNIQUE
        )
    """)

    conn.execute("""
        INSERT OR IGNORE INTO settings(id)
        VALUES(1)
    """)

    # settings 컬럼 확인
    scols = {
        r[1]
        for r in conn.execute(
            "PRAGMA table_info(settings)"
        ).fetchall()
    }

    if "teacher_password" not in scols:
        conn.execute("""
            ALTER TABLE settings
            ADD COLUMN teacher_password TEXT NOT NULL DEFAULT '1234'
        """)

    # students 컬럼 확인
    cols = {
        r[1]
        for r in conn.execute(
            "PRAGMA table_info(students)"
        ).fetchall()
    }

    if "password" not in cols:
        conn.execute("""
            ALTER TABLE students
            ADD COLUMN password TEXT NOT NULL DEFAULT '1234'
        """)

    if "group_name" not in cols:
        conn.execute("""
            ALTER TABLE students
            ADD COLUMN group_name TEXT NOT NULL DEFAULT ''
        """)

    # feedback 컬럼 확인
    fcols = {
        r[1]
        for r in conn.execute(
            "PRAGMA table_info(feedback)"
        ).fetchall()
    }

    if "evaluation_type" not in fcols:
        conn.execute("""
            ALTER TABLE feedback
            ADD COLUMN evaluation_type TEXT NOT NULL DEFAULT 'peer'
        """)

    if "reply" not in fcols:
        conn.execute("""
            ALTER TABLE feedback
            ADD COLUMN reply TEXT NOT NULL DEFAULT ''
        """)

    if "cloud_id" not in fcols:
        conn.execute("""
            ALTER TABLE feedback
            ADD COLUMN cloud_id TEXT
        """)

    conn.commit()
    conn.close()


# =========================================================
# TIME
# =========================================================

def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat(
        timespec="seconds"
    )


def format_kst(value):
    if not value:
        return ""

    try:
        s = str(value)

        if "T" in s:
            dt = datetime.fromisoformat(
                s.replace("Z", "+00:00")
            )
        else:
            dt = datetime.strptime(
                s,
                "%Y-%m-%d %H:%M:%S"
            ).replace(
                tzinfo=timezone.utc
            )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt.astimezone(
            KST
        ).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    except Exception:
        return str(value)


app.jinja_env.filters["kst"] = format_kst


# =========================================================
# GOOGLE SHEETS
# =========================================================

def sheet_url(action=None):
    if not SHEETS_WEBHOOK_URL:
        return ""

    if not action:
        return SHEETS_WEBHOOK_URL

    separator = (
        "&"
        if "?" in SHEETS_WEBHOOK_URL
        else "?"
    )

    return (
        SHEETS_WEBHOOK_URL
        + separator
        + urllib.parse.urlencode(
            {"action": action}
        )
    )


def sheet_sync(event, data):
    """
    Render -> Google Apps Script -> Google Sheets
    """

    # 환경변수 확인
    if not SHEETS_WEBHOOK_URL:
        app.logger.error(
            "[Sheets] GOOGLE_SHEETS_WEBHOOK_URL가 비어 있습니다."
        )
        return False

    app.logger.warning(
        "[Sheets] POST 시작 | event=%s | url_configured=%s",
        event,
        bool(SHEETS_WEBHOOK_URL)
    )

    payload = {
        "event": event,
        **data
    }

    try:
        body = json.dumps(
            payload,
            ensure_ascii=False
        ).encode("utf-8")

        req = urllib.request.Request(
            SHEETS_WEBHOOK_URL,
            data=body,
            headers={
                "Content-Type": "application/json"
            },
            method="POST",
        )

        with urllib.request.urlopen(
            req,
            timeout=15
        ) as response:

            status = response.status

            raw = response.read().decode(
                "utf-8"
            )

        app.logger.warning(
            "[Sheets] POST 응답 | event=%s | status=%s | response=%s",
            event,
            status,
            raw[:1000]
        )

        if status < 200 or status >= 300:
            app.logger.error(
                "[Sheets] HTTP 오류 | status=%s",
                status
            )
            return False

        try:
            result = json.loads(raw)
        except Exception:
            result = {}

        if result.get("ok") is False:
            app.logger.error(
                "[Sheets] Apps Script 오류 | %s",
                result
            )
            return False

        app.logger.warning(
            "[Sheets] 동기화 성공 | event=%s",
            event
        )

        return True

    except Exception as e:
        app.logger.exception(
            "[Sheets] POST 실패 | event=%s | error=%s",
            event,
            e
        )
        return False


def sheet_export():
    """
    Google Sheets -> Render SQLite 복구
    """

    if not SHEETS_WEBHOOK_URL:
        app.logger.warning(
            "[Sheets] export 불가: URL 없음"
        )
        return None

    try:
        url = sheet_url("export")

        app.logger.warning(
            "[Sheets] export 요청"
        )

        with urllib.request.urlopen(
            url,
            timeout=15
        ) as response:

            raw = response.read().decode(
                "utf-8"
            )

        data = json.loads(raw)

        app.logger.warning(
            "[Sheets] export 응답: %s",
            raw[:1000]
        )

        if data.get("ok") is not True:
            app.logger.error(
                "[Sheets] export 실패: %s",
                data
            )
            return None

        return data

    except Exception as e:
        app.logger.exception(
            "[Sheets] export 예외"
        )
        return None


# =========================================================
# SHEETS PAYLOAD
# =========================================================

def row_feedback_payload(row):
    return {
        "feedback_id": row["cloud_id"],
        "sender_number": row["sender_number"],
        "sender_name": row["sender_name"],
        "target_number": row["target_number"],
        "target_name": row["target_name"],
        "score": row["score"],
        "content": row["content"],
        "reply": row["reply"],
        "evaluation_type": row["evaluation_type"],
        "created_at": row["created_at"],
    }


# =========================================================
# INITIAL SHEETS BACKUP
# =========================================================

def sync_local_snapshot():
    """
    Sheets가 아직 없을 때
    현재 SQLite 데이터를 Google Sheets에 최초 백업
    """

    if not SHEETS_WEBHOOK_URL:
        return

    conn = get_db()

    students = [
        dict(r)
        for r in conn.execute(
            """
            SELECT
                student_number,
                name,
                password,
                group_name
            FROM students
            ORDER BY student_number
            """
        ).fetchall()
    ]

    feedbacks = [
        row_feedback_payload(r)
        for r in conn.execute(
            """
            SELECT *
            FROM feedback
            ORDER BY id
            """
        ).fetchall()
    ]

    settings = conn.execute(
        """
        SELECT teacher_password
        FROM settings
        WHERE id=1
        """
    ).fetchone()

    teacher_password = (
        settings["teacher_password"]
        if settings
        else TEACHER_PASSWORD
    )

    conn.close()

    # cloud_id 없는 평가에 ID 부여
    for feedback in feedbacks:

        if not feedback["feedback_id"]:

            new_id = secrets.token_hex(16)

            conn = get_db()

            conn.execute(
                """
                UPDATE feedback
                SET cloud_id=?
                WHERE sender_number=?
                AND target_number=?
                AND created_at=?
                """,
                (
                    new_id,
                    feedback["sender_number"],
                    feedback["target_number"],
                    feedback["created_at"],
                )
            )

            conn.commit()
            conn.close()

            feedback["feedback_id"] = new_id

    app.logger.warning(
        "[Sheets] 최초 bulk_sync 시작 | 학생=%d | 평가=%d",
        len(students),
        len(feedbacks)
    )

    sheet_sync(
        "bulk_sync",
        {
            "students": students,
            "feedbacks": feedbacks,
            "teacher_password": teacher_password,
        }
    )


# =========================================================
# RESTORE FROM SHEETS
# =========================================================

def restore_from_sheets():
    """
    Render가 재시작되면
    Google Sheets 데이터를 SQLite로 복구
    """

    data = sheet_export()

    if data is None:
        app.logger.warning(
            "[Sheets] 복구 데이터 없음"
        )
        return

    students = data.get(
        "students",
        []
    ) or []

    feedbacks = data.get(
        "feedbacks",
        []
    ) or []

    students_sheet_exists = bool(
        data.get(
            "students_sheet_exists"
        )
    )

    feedback_sheet_exists = bool(
        data.get(
            "feedback_sheet_exists"
        )
    )

    settings = data.get(
        "settings",
        {}
    ) or {}

    app.logger.warning(
        "[Sheets] 복구 시작 | 학생=%d | 평가=%d | 학생시트=%s | 평가시트=%s",
        len(students),
        len(feedbacks),
        students_sheet_exists,
        feedback_sheet_exists
    )

    conn = get_db()

    try:

        # 학생 시트가 존재하면 학생 복구
        if students_sheet_exists:

            conn.execute(
                "DELETE FROM students"
            )

            for student in students:

                number = str(
                    student.get(
                        "student_number",
                        ""
                    )
                ).strip()

                if not number:
                    continue

                name = str(
                    student.get(
                        "name",
                        number
                    )
                )

                password = str(
                    student.get(
                        "password",
                        "1234"
                    )
                )

                group_name = str(
                    student.get(
                        "group_name",
                        ""
                    )
                )

                conn.execute(
                    """
                    INSERT OR REPLACE INTO students
                    (
                        student_number,
                        name,
                        password,
                        group_name,
                        session_token,
                        last_seen
                    )
                    VALUES(?,?,?,?,NULL,0)
                    """,
                    (
                        number,
                        name,
                        password,
                        group_name,
                    )
                )

        # 평가 복구
        if feedback_sheet_exists and feedbacks:

            conn.execute(
                "DELETE FROM feedback"
            )

            for feedback in feedbacks:

                try:

                    score = int(
                        feedback.get(
                            "score",
                            0
                        )
                    )

                    if not 1 <= score <= 5:
                        continue

                    cloud_id = str(
                        feedback.get(
                            "id"
                        )
                        or feedback.get(
                            "feedback_id"
                        )
                        or secrets.token_hex(16)
                    )

                    sender_number = str(
                        feedback.get(
                            "sender_number",
                            ""
                        )
                    )

                    target_number = str(
                        feedback.get(
                            "target_number",
                            ""
                        )
                    )

                    sender_name = str(
                        feedback.get(
                            "sender_name",
                            ""
                        )
                        or sender_number
                    )

                    target_name = str(
                        feedback.get(
                            "target_name",
                            ""
                        )
                        or target_number
                    )

                    created_at = str(
                        feedback.get(
                            "created_at",
                            ""
                        )
                        or now_iso()
                    )

                    evaluation_type = (
                        feedback.get(
                            "evaluation_type"
                        )
                    )

                    if evaluation_type not in (
                        "peer",
                        "presenter"
                    ):
                        evaluation_type = "peer"

                    conn.execute(
                        """
                        INSERT INTO feedback
                        (
                            sender_number,
                            sender_name,
                            target_number,
                            target_name,
                            score,
                            content,
                            reply,
                            evaluation_type,
                            created_at,
                            cloud_id
                        )
                        VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            sender_number,
                            sender_name,
                            target_number,
                            target_name,
                            score,
                            str(
                                feedback.get(
                                    "content",
                                    ""
                                )
                            ),
                            str(
                                feedback.get(
                                    "reply",
                                    ""
                                )
                            ),
                            evaluation_type,
                            created_at,
                            cloud_id,
                        )
                    )

                    # 평가에 등장하는 학생이
                    # 학생 시트에 없으면 최소 복구
                    for number, name in (
                        (
                            sender_number,
                            sender_name
                        ),
                        (
                            target_number,
                            target_name
                        ),
                    ):

                        if number:

                            conn.execute(
                                """
                                INSERT OR IGNORE INTO students
                                (
                                    student_number,
                                    name,
                                    password,
                                    group_name,
                                    session_token,
                                    last_seen
                                )
                                VALUES(?,?,?,?,NULL,0)
                                """,
                                (
                                    number,
                                    name or number,
                                    "1234",
                                    "",
                                )
                            )

                except Exception as e:

                    app.logger.warning(
                        "[Sheets] 평가 복구 건너뜀: %s",
                        e
                    )

        # 선생님 비밀번호
        if settings.get(
            "teacher_password"
        ):

            conn.execute(
                """
                UPDATE settings
                SET teacher_password=?
                WHERE id=1
                """,
                (
                    str(
                        settings[
                            "teacher_password"
                        ]
                    ),
                )
            )

        conn.commit()

    finally:
        conn.close()

    # 시트가 없던 최초 실행이면
    # 현재 DB를 Sheets에 백업
    if (
        not students_sheet_exists
        or not feedback_sheet_exists
    ):

        app.logger.warning(
            "[Sheets] 시트가 없어서 최초 snapshot 실행"
        )

        sync_local_snapshot()


# =========================================================
# STUDENT SESSION
# =========================================================

def touch_student():

    number = session.get(
        "student_number"
    )

    token = session.get(
        "student_token"
    )

    if not number or not token:
        return False

    conn = get_db()

    row = conn.execute(
        """
        SELECT *
        FROM students
        WHERE student_number=?
        """,
        (number,)
    ).fetchone()

    if (
        not row
        or row["session_token"] != token
    ):

        conn.close()
        session.clear()

        return False

    conn.execute(
        """
        UPDATE students
        SET last_seen=?
        WHERE student_number=?
        """,
        (
            int(
                datetime.now(
                    timezone.utc
                ).timestamp()
            ),
            number,
        )
    )

    conn.commit()
    conn.close()

    session["student_name"] = row["name"]

    return True


def student_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        if not touch_student():
            return redirect(
                url_for("login")
            )

        return function(
            *args,
            **kwargs
        )

    return wrapper


def teacher_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        if not session.get(
            "teacher"
        ):
            return redirect(
                url_for("teacher_login")
            )

        return function(
            *args,
            **kwargs
        )

    return wrapper


# =========================================================
# STUDENT
# =========================================================

def get_student(number):

    conn = get_db()

    row = conn.execute(
        """
        SELECT *
        FROM students
        WHERE student_number=?
        """,
        (number,)
    ).fetchone()

    conn.close()

    return row


# =========================================================
# INDEX
# =========================================================

@app.route("/")
def index():

    if session.get("teacher"):
        return redirect(
            url_for("teacher")
        )

    if (
        session.get("student_number")
        and touch_student()
    ):
        return redirect(
            url_for("student")
        )

    return render_template(
        "lobby.html"
    )


# =========================================================
# STUDENT LOGIN
# =========================================================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if request.method == "POST":

        number = request.form.get(
            "student_number",
            ""
        ).strip()

        name = request.form.get(
            "name",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        if (
            not number
            or not name
            or not password
        ):

            flash(
                "학번, 이름, 비밀번호를 입력해 주세요."
            )

            return render_template(
                "login.html"
            )

        if not number.isdigit():

            flash(
                "학번은 숫자로 입력해 주세요."
            )

            return render_template(
                "login.html"
            )

        conn = get_db()

        row = conn.execute(
            """
            SELECT *
            FROM students
            WHERE student_number=?
            """,
            (number,)
        ).fetchone()

        if row:

            if row["password"] != password:

                conn.close()

                flash(
                    "비밀번호가 맞지 않습니다."
                )

                return render_template(
                    "login.html"
                )

            token = secrets.token_urlsafe(32)

            conn.execute(
                """
                UPDATE students
                SET
                    name=?,
                    session_token=?,
                    last_seen=?
                WHERE student_number=?
                """,
                (
                    name,
                    token,
                    int(
                        datetime.now(
                            timezone.utc
                        ).timestamp()
                    ),
                    number,
                )
            )

        else:

            token = secrets.token_urlsafe(32)

            conn.execute(
                """
                INSERT INTO students
                (
                    student_number,
                    name,
                    password,
                    session_token,
                    last_seen
                )
                VALUES(?,?,?,?,?)
                """,
                (
                    number,
                    name,
                    "1234",
                    token,
                    int(
                        datetime.now(
                            timezone.utc
                        ).timestamp()
                    ),
                )
            )

        conn.commit()

        saved = conn.execute(
            """
            SELECT
                student_number,
                name,
                password,
                group_name
            FROM students
            WHERE student_number=?
            """,
            (number,)
        ).fetchone()

        conn.close()

        # =================================================
        # ★ 학생 로그인 시 Google Sheets 저장
        # =================================================

        app.logger.warning(
            "========================================"
        )

        app.logger.warning(
            "[Student Login] 학생 로그인 완료"
        )

        app.logger.warning(
            "[Student Login] 학번=%s",
            number
        )

        app.logger.warning(
            "[Student Login] 학생 데이터=%s",
            dict(saved) if saved else None
        )

        app.logger.warning(
            "[Student Login] Google Sheets 동기화 시작"
        )

        sync_result = sheet_sync(
            "student_upsert",
            dict(saved)
            if saved
            else {}
        )

        app.logger.warning(
            "[Student Login] Google Sheets 동기화 결과=%s",
            sync_result
        )

        app.logger.warning(
            "========================================"
        )

        # 세션
        session.clear()

        session.update(
            student_number=number,
            student_name=name,
            student_token=token,
        )

        return redirect(
            url_for("student")
        )

    return render_template(
        "login.html"
    )


# =========================================================
# STUDENT PAGE
# =========================================================

@app.route("/student")
@student_required
def student():

    conn = get_db()

    me = conn.execute(
        """
        SELECT *
        FROM students
        WHERE student_number=?
        """,
        (
            session[
                "student_number"
            ],
        )
    ).fetchone()

    group = me["group_name"]

    if group:

        members = conn.execute(
            """
            SELECT student_number,name
            FROM students
            WHERE group_name=?
            AND student_number<>?
            ORDER BY student_number
            """,
            (
                group,
                session[
                    "student_number"
                ],
            )
        ).fetchall()

    else:
        members = []

    received = conn.execute(
        """
        SELECT *
        FROM feedback
        WHERE target_number=?
        ORDER BY id DESC
        """,
        (
            session[
                "student_number"
            ],
        )
    ).fetchall()

    sent = conn.execute(
        """
        SELECT *
        FROM feedback
        WHERE sender_number=?
        ORDER BY id DESC
        """,
        (
            session[
                "student_number"
            ],
        )
    ).fetchall()

    avg = conn.execute(
        """
        SELECT AVG(score) a
        FROM feedback
        WHERE target_number=?
        """,
        (
            session[
                "student_number"
            ],
        )
    ).fetchone()["a"]

    peer_avg = conn.execute(
        """
        SELECT AVG(score) a
        FROM feedback
        WHERE target_number=?
        AND evaluation_type='peer'
        """,
        (
            session[
                "student_number"
            ],
        )
    ).fetchone()["a"]

    presenter_avg = conn.execute(
        """
        SELECT AVG(score) a
        FROM feedback
        WHERE target_number=?
        AND evaluation_type='presenter'
        """,
        (
            session[
                "student_number"
            ],
        )
    ).fetchone()["a"]

    conn.close()

    return render_template(
        "student.html",
        me=me,
        members=members,
        received=received,
        sent=sent,
        avg=avg,
        peer_avg=peer_avg,
        presenter_avg=presenter_avg,
    )


# =========================================================
# ADD FEEDBACK
# =========================================================

@app.route(
    "/api/feedback",
    methods=["POST"]
)
@student_required
def add_feedback():

    target = request.form.get(
        "target_number",
        ""
    ).strip()

    content = request.form.get(
        "content",
        ""
    ).strip()

    evaluation_type = request.form.get(
        "evaluation_type",
        "peer"
    ).strip()

    if evaluation_type not in (
        "peer",
        "presenter"
    ):
        evaluation_type = "peer"

    try:
        score = int(
            request.form.get(
                "score",
                ""
            )
        )
    except Exception:
        score = 0

    if (
        not target
        or not 1 <= score <= 5
        or not content
    ):

        return jsonify(
            ok=False,
            message=(
                "학번, 점수(1~5), "
                "평가 내용을 확인해 주세요."
            )
        ), 400

    conn = get_db()

    sender = conn.execute(
        """
        SELECT *
        FROM students
        WHERE student_number=?
        """,
        (
            session[
                "student_number"
            ],
        )
    ).fetchone()

    target_row = conn.execute(
        """
        SELECT *
        FROM students
        WHERE student_number=?
        """,
        (target,)
    ).fetchone()

    if not target_row:

        conn.close()

        return jsonify(
            ok=False,
            message="존재하지 않는 학생입니다."
        ), 404

    if (
        target
        == session[
            "student_number"
        ]
    ):

        conn.close()

        return jsonify(
            ok=False,
            message="자기 자신은 평가할 수 없습니다."
        ), 403

    if (
        evaluation_type == "peer"
        and (
            not sender["group_name"]
            or sender["group_name"]
            != target_row["group_name"]
        )
    ):

        conn.close()

        return jsonify(
            ok=False,
            message="조원 평가는 같은 조원만 가능합니다."
        ), 403

    existing = conn.execute(
        """
        SELECT *
        FROM feedback
        WHERE sender_number=?
        AND target_number=?
        AND evaluation_type=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            sender["student_number"],
            target,
            evaluation_type,
        )
    ).fetchone()

    created = now_iso()

    if existing:

        cloud_id = (
            existing["cloud_id"]
            or secrets.token_hex(16)
        )

        conn.execute(
            """
            UPDATE feedback
            SET
                score=?,
                content=?,
                created_at=?,
                cloud_id=?
            WHERE id=?
            """,
            (
                score,
                content,
                created,
                cloud_id,
                existing["id"],
            )
        )

        feedback_id = existing["id"]

    else:

        cloud_id = secrets.token_hex(16)

        conn.execute(
            """
            INSERT INTO feedback
            (
                sender_number,
                sender_name,
                target_number,
                target_name,
                score,
                content,
                evaluation_type,
                created_at,
                cloud_id
            )
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                sender["student_number"],
                sender["name"],
                target,
                target_row["name"],
                score,
                content,
                evaluation_type,
                created,
                cloud_id,
            )
        )

        feedback_id = conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

    conn.commit()

    saved = conn.execute(
        """
        SELECT *
        FROM feedback
        WHERE id=?
        """,
        (feedback_id,)
    ).fetchone()

    conn.close()

    # Google Sheets 저장
    sync_result = sheet_sync(
        "feedback",
        row_feedback_payload(saved)
    )

    app.logger.warning(
        "[Feedback] Sheets sync result=%s",
        sync_result
    )

    return jsonify(
        ok=True
    )


# =========================================================
# REPLY
# =========================================================

@app.route(
    "/api/feedback/<int:feedback_id>/reply",
    methods=["POST"]
)
@student_required
def reply_feedback(feedback_id):

    reply = request.form.get(
        "reply",
        ""
    ).strip()

    if len(reply) > 1000:

        return jsonify(
            ok=False,
            message="답변은 1000자 이하입니다."
        ), 400

    conn = get_db()

    row = conn.execute(
        """
        SELECT *
        FROM feedback
        WHERE id=?
        AND target_number=?
        """,
        (
            feedback_id,
            session[
                "student_number"
            ],
        )
    ).fetchone()

    if not row:

        conn.close()

        return jsonify(
            ok=False,
            message="평가를 찾을 수 없습니다."
        ), 404

    cloud_id = (
        row["cloud_id"]
        or secrets.token_hex(16)
    )

    conn.execute(
        """
        UPDATE feedback
        SET
            reply=?,
            cloud_id=?
        WHERE id=?
        """,
        (
            reply,
            cloud_id,
            feedback_id,
        )
    )

    conn.commit()

    conn.close()

    sync_result = sheet_sync(
        "reply",
        {
            "feedback_id": cloud_id,
            "reply": reply,
        }
    )

    app.logger.warning(
        "[Reply] Sheets sync result=%s",
        sync_result
    )

    return jsonify(
        ok=True
    )


# =========================================================
# STUDENT PASSWORD
# =========================================================

@app.route(
    "/student/change-password",
    methods=["POST"]
)
@student_required
def student_change_password():

    current = request.form.get(
        "current_password",
        ""
    )

    new = request.form.get(
        "new_password",
        ""
    )

    confirm = request.form.get(
        "confirm_password",
        ""
    )

    if (
        not current
        or not new
        or not confirm
    ):

        flash(
            "현재 비밀번호와 새 비밀번호를 모두 입력해 주세요."
        )

        return redirect(
            url_for("student")
        )

    if len(new) < 4:

        flash(
            "새 비밀번호는 4자 이상으로 설정해 주세요."
        )

        return redirect(
            url_for("student")
        )

    if new != confirm:

        flash(
            "새 비밀번호가 서로 다릅니다."
        )

        return redirect(
            url_for("student")
        )

    conn = get_db()

    row = conn.execute(
        """
        SELECT password
        FROM students
        WHERE student_number=?
        """,
        (
            session[
                "student_number"
            ],
        )
    ).fetchone()

    if (
        not row
        or row["password"] != current
    ):

        conn.close()

        flash(
            "현재 비밀번호가 맞지 않습니다."
        )

        return redirect(
            url_for("student")
        )

    conn.execute(
        """
        UPDATE students
        SET password=?
        WHERE student_number=?
        """,
        (
            new,
            session[
                "student_number"
            ],
        )
    )

    conn.commit()

    saved = conn.execute(
        """
        SELECT
            student_number,
            name,
            password,
            group_name
        FROM students
        WHERE student_number=?
        """,
        (
            session[
                "student_number"
            ],
        )
    ).fetchone()

    conn.close()

    sheet_sync(
        "student_upsert",
        dict(saved)
    )

    flash(
        "학생 비밀번호가 변경되었습니다."
    )

    return redirect(
        url_for("student")
    )


# =========================================================
# STUDENT SEARCH
# =========================================================

@app.route(
    "/api/student-search"
)
@student_required
def student_search():

    q = request.args.get(
        "q",
        ""
    ).strip()

    evaluation_type = request.args.get(
        "type",
        "peer"
    )

    conn = get_db()

    row = conn.execute(
        """
        SELECT
            student_number,
            name,
            group_name
        FROM students
        WHERE student_number=?
        """,
        (q,)
    ).fetchone()

    conn.close()

    if not row:
        return jsonify(
            found=False
        )

    me = get_student(
        session[
            "student_number"
        ]
    )

    allowed = bool(
        me
        and row["student_number"]
        != me["student_number"]
        and (
            evaluation_type == "presenter"
            or (
                me["group_name"]
                and me["group_name"]
                == row["group_name"]
            )
        )
    )

    return jsonify(
        found=allowed,
        student_number=row[
            "student_number"
        ],
        name=row["name"],
        group_name=row[
            "group_name"
        ],
    )


# =========================================================
# TEACHER LOGIN
# =========================================================

def get_teacher_password():

    conn = get_db()

    row = conn.execute(
        """
        SELECT teacher_password
        FROM settings
        WHERE id=1
        """
    ).fetchone()

    conn.close()

    if (
        row
        and row["teacher_password"]
    ):
        return row[
            "teacher_password"
        ]

    return TEACHER_PASSWORD


@app.route(
    "/teacher/login",
    methods=["GET", "POST"]
)
def teacher_login():

    if request.method == "POST":

        if (
            request.form.get(
                "password"
            )
            == get_teacher_password()
        ):

            session.clear()
            session["teacher"] = True

            return redirect(
                url_for("teacher")
            )

        flash(
            "비밀번호가 맞지 않습니다."
        )

    return render_template(
        "teacher_login.html"
    )


# =========================================================
# TEACHER PAGE
# =========================================================

@app.route("/teacher")
@teacher_required
def teacher():

    conn = get_db()

    students = conn.execute(
        """
        SELECT
            s.student_number,
            s.name,
            s.group_name,
            AVG(f.score) avg_score,
            AVG(
                CASE
                    WHEN f.evaluation_type='peer'
                    THEN f.score
                END
            ) peer_avg,
            AVG(
                CASE
                    WHEN f.evaluation_type='presenter'
                    THEN f.score
                END
            ) presenter_avg,
            COUNT(f.id) feedback_count
        FROM students s
        LEFT JOIN feedback f
            ON f.target_number=s.student_number
        GROUP BY s.student_number
        ORDER BY s.student_number
        """
    ).fetchall()

    feedbacks = conn.execute(
        """
        SELECT *
        FROM feedback
        ORDER BY id DESC
        """
    ).fetchall()

    conn.close()

    return render_template(
        "teacher.html",
        students=students,
        feedbacks=feedbacks,
    )


# =========================================================
# TEACHER PASSWORD
# =========================================================

@app.route(
    "/teacher/change-password",
    methods=["POST"]
)
@teacher_required
def teacher_change_password():

    current = request.form.get(
        "current_password",
        ""
    )

    new = request.form.get(
        "new_password",
        ""
    )

    confirm = request.form.get(
        "confirm_password",
        ""
    )

    if (
        not current
        or not new
        or not confirm
    ):

        flash(
            "현재 비밀번호와 새 비밀번호를 모두 입력해 주세요."
        )

        return redirect(
            url_for("teacher")
        )

    if len(new) < 4:

        flash(
            "새 비밀번호는 4자 이상으로 설정해 주세요."
        )

        return redirect(
            url_for("teacher")
        )

    if new != confirm:

        flash(
            "새 비밀번호가 서로 다릅니다."
        )

        return redirect(
            url_for("teacher")
        )

    if current != get_teacher_password():

        flash(
            "현재 관리자 비밀번호가 맞지 않습니다."
        )

        return redirect(
            url_for("teacher")
        )

    conn = get_db()

    conn.execute(
        """
        UPDATE settings
        SET teacher_password=?
        WHERE id=1
        """,
        (new,)
    )

    conn.commit()
    conn.close()

    sheet_sync(
        "teacher_password",
        {
            "teacher_password": new
        }
    )

    flash(
        "선생님 관리 비밀번호가 변경되었습니다."
    )

    return redirect(
        url_for("teacher")
    )


# =========================================================
# GROUP ASSIGN
# =========================================================

@app.route(
    "/teacher/group",
    methods=["POST"]
)
@teacher_required
def set_group():

    number = request.form.get(
        "student_number",
        ""
    ).strip()

    group_name = request.form.get(
        "group_name",
        ""
    ).strip()[:30]

    conn = get_db()

    row = conn.execute(
        """
        SELECT name
        FROM students
        WHERE student_number=?
        """,
        (number,)
    ).fetchone()

    if not row:

        conn.close()

        flash(
            "학생을 찾을 수 없습니다."
        )

        return redirect(
            url_for("teacher")
        )

    conn.execute(
        """
        UPDATE students
        SET group_name=?
        WHERE student_number=?
        """,
        (
            group_name,
            number,
        )
    )

    conn.commit()

    saved = conn.execute(
        """
        SELECT
            student_number,
            name,
            password,
            group_name
        FROM students
        WHERE student_number=?
        """,
        (number,)
    ).fetchone()

    conn.close()

    sheet_sync(
        "student_upsert",
        dict(saved)
    )

    flash(
        f"{number} {row['name']} 학생을 "
        f"{group_name or '조 없음'}으로 지정했습니다."
    )

    return redirect(
        url_for("teacher")
    )


# =========================================================
# DELETE STUDENT
# =========================================================

@app.route(
    "/teacher/student/<student_number>/delete",
    methods=["POST"]
)
@teacher_required
def delete_student(student_number):

    conn = get_db()

    row = conn.execute(
        """
        SELECT name
        FROM students
        WHERE student_number=?
        """,
        (student_number,)
    ).fetchone()

    if not row:

        conn.close()

        abort(404)

    conn.execute(
        """
        DELETE FROM feedback
        WHERE sender_number=?
        OR target_number=?
        """,
        (
            student_number,
            student_number,
        )
    )

    conn.execute(
        """
        DELETE FROM students
        WHERE student_number=?
        """,
        (student_number,)
    )

    conn.commit()
    conn.close()

    sheet_sync(
        "student_delete",
        {
            "student_number": student_number
        }
    )

    flash(
        f"{student_number} {row['name']} "
        "학생 계정을 삭제했습니다."
    )

    return redirect(
        url_for("teacher")
    )


# =========================================================
# FORCE LOGOUT
# =========================================================

@app.route(
    "/teacher/student/<student_number>/force-logout",
    methods=["POST"]
)
@teacher_required
def force_logout(student_number):

    conn = get_db()

    conn.execute(
        """
        UPDATE students
        SET session_token=NULL
        WHERE student_number=?
        """,
        (student_number,)
    )

    conn.commit()
    conn.close()

    flash(
        "학생 접속을 강제로 종료했습니다."
    )

    return redirect(
        url_for("teacher")
    )


# =========================================================
# GOOGLE SHEETS TEST
# =========================================================

@app.route(
    "/teacher/sheets-test",
    methods=["GET", "POST"]
)
@teacher_required
def sheets_test():

    app.logger.warning(
        "[Sheets TEST] 테스트 시작"
    )

    ok = sheet_sync(
        "student_upsert",
        {
            "student_number": "__SYNC_TEST__",
            "name": "Google Sheets 연결 테스트",
            "password": "1234",
            "group_name": "",
        }
    )

    if ok:

        flash(
            "Google Sheets 저장 테스트 성공! "
            "학생 시트에 __SYNC_TEST__가 생성/갱신되었는지 확인하세요."
        )

        app.logger.warning(
            "[Sheets TEST] 성공"
        )

    else:

        flash(
            "Google Sheets 저장 테스트 실패! "
            "Render 로그를 확인하세요."
        )

        app.logger.error(
            "[Sheets TEST] 실패"
        )

    return redirect(
        url_for("teacher")
    )


# =========================================================
# CLEAR FEEDBACKS
# =========================================================

@app.route(
    "/teacher/clear-feedbacks",
    methods=["POST"]
)
@teacher_required
def clear_feedbacks():

    conn = get_db()

    conn.execute(
        "DELETE FROM feedback"
    )

    conn.commit()
    conn.close()

    sheet_sync(
        "reset_feedbacks",
        {}
    )

    flash(
        "모든 평가 내역을 초기화했습니다."
    )

    return redirect(
        url_for("teacher")
    )


# =========================================================
# TEACHER STUDENT DETAIL
# =========================================================

@app.route(
    "/teacher/student/<student_number>"
)
@teacher_required
def teacher_student(student_number):

    conn = get_db()

    student = conn.execute(
        """
        SELECT *
        FROM students
        WHERE student_number=?
        """,
        (student_number,)
    ).fetchone()

    if student:

        rows = conn.execute(
            """
            SELECT *
            FROM feedback
            WHERE target_number=?
            ORDER BY id DESC
            """,
            (student_number,)
        ).fetchall()

        avg = conn.execute(
            """
            SELECT AVG(score) a
            FROM feedback
            WHERE target_number=?
            """,
            (student_number,)
        ).fetchone()["a"]

        peer_avg = conn.execute(
            """
            SELECT AVG(score) a
            FROM feedback
            WHERE target_number=?
            AND evaluation_type='peer'
            """,
            (student_number,)
        ).fetchone()["a"]

        presenter_avg = conn.execute(
            """
            SELECT AVG(score) a
            FROM feedback
            WHERE target_number=?
            AND evaluation_type='presenter'
            """,
            (student_number,)
        ).fetchone()["a"]

    else:

        rows = []
        avg = None
        peer_avg = None
        presenter_avg = None

    conn.close()

    if not student:
        abort(404)

    return render_template(
        "teacher_student.html",
        student=student,
        feedbacks=rows,
        avg=avg,
        peer_avg=peer_avg,
        presenter_avg=presenter_avg,
    )


# =========================================================
# TEACHER FEEDBACK API
# =========================================================

@app.route(
    "/teacher/feedbacks"
)
@teacher_required
def teacher_feedbacks():

    conn = get_db()

    rows = conn.execute(
        """
        SELECT *
        FROM feedback
        ORDER BY id DESC
        """
    ).fetchall()

    conn.close()

    return jsonify(
        [
            {
                **dict(row),
                "created_at": format_kst(
                    row["created_at"]
                ),
            }
            for row in rows
        ]
    )


# =========================================================
# STARTUP
# =========================================================

init_db()

restore_from_sheets()


if __name__ == "__main__":

    app.run(
        host=os.environ.get(
            "HOST",
            "0.0.0.0"
        ),
        port=int(
            os.environ.get(
                "PORT",
                "5000"
            )
        ),
        debug=False,
    )
