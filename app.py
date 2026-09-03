import os, sqlite3, secrets, json, urllib.request
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, abort

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")
DB_PATH = os.environ.get("DB_PATH", "feedback.db")
TEACHER_PASSWORD = os.environ.get("TEACHER_PASSWORD", "1234")
SHEETS_WEBHOOK_URL = os.environ.get("GOOGLE_SHEETS_WEBHOOK_URL", "").strip()
KST = timezone(timedelta(hours=9))

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1))")
    conn.execute("CREATE TABLE IF NOT EXISTS students (student_number TEXT PRIMARY KEY, name TEXT NOT NULL, password TEXT NOT NULL DEFAULT '1234', group_name TEXT NOT NULL DEFAULT '', session_token TEXT, last_seen INTEGER NOT NULL DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, sender_number TEXT NOT NULL, sender_name TEXT NOT NULL, target_number TEXT NOT NULL, target_name TEXT NOT NULL, score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 5), content TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '', evaluation_type TEXT NOT NULL DEFAULT 'peer', created_at TEXT NOT NULL)")
    conn.execute("INSERT OR IGNORE INTO settings(id) VALUES(1)")
    # 기존 DB 호환
    cols = {r[1] for r in conn.execute("PRAGMA table_info(students)").fetchall()}
    if "password" not in cols: conn.execute("ALTER TABLE students ADD COLUMN password TEXT NOT NULL DEFAULT '1234'")
    if "group_name" not in cols: conn.execute("ALTER TABLE students ADD COLUMN group_name TEXT NOT NULL DEFAULT ''")
    fcols = {r[1] for r in conn.execute("PRAGMA table_info(feedback)").fetchall()}
    if "evaluation_type" not in fcols: conn.execute("ALTER TABLE feedback ADD COLUMN evaluation_type TEXT NOT NULL DEFAULT 'peer'")
    conn.commit(); conn.close()

def now_iso(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def format_kst(v):
    if not v: return ""
    try:
        s=str(v); dt=datetime.fromisoformat(s.replace("Z", "+00:00")) if "T" in s else datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception: return str(v)
app.jinja_env.filters["kst"] = format_kst

def touch_student():
    n=session.get("student_number"); t=session.get("student_token")
    if not n or not t: return False
    conn=get_db(); row=conn.execute("SELECT * FROM students WHERE student_number=?",(n,)).fetchone()
    if not row or row["session_token"] != t: conn.close(); session.clear(); return False
    conn.execute("UPDATE students SET last_seen=? WHERE student_number=?",(int(datetime.now(timezone.utc).timestamp()),n)); conn.commit(); conn.close(); session["student_name"]=row["name"]; return True

def student_required(f):
    @wraps(f)
    def w(*a,**kw):
        if not touch_student(): return redirect(url_for("login"))
        return f(*a,**kw)
    return w

def teacher_required(f):
    @wraps(f)
    def w(*a,**kw):
        if not session.get("teacher"): return redirect(url_for("teacher_login"))
        return f(*a,**kw)
    return w

def sheet_sync(event, data):
    if not SHEETS_WEBHOOK_URL: return
    try:
        body=json.dumps({"event":event, **data}, ensure_ascii=False).encode()
        req=urllib.request.Request(SHEETS_WEBHOOK_URL,data=body,headers={"Content-Type":"application/json"},method="POST")
        urllib.request.urlopen(req,timeout=3).read()
    except Exception as e: app.logger.warning("Google Sheets sync failed: %s",e)

def get_student(n):
    conn=get_db(); r=conn.execute("SELECT * FROM students WHERE student_number=?",(n,)).fetchone(); conn.close(); return r

@app.route("/")
def index():
    if session.get("teacher"): return redirect(url_for("teacher"))
    if session.get("student_number") and touch_student(): return redirect(url_for("student"))
    return render_template("lobby.html")

@app.route("/login",methods=["GET","POST"])
def login():
    if request.method=="POST":
        n=request.form.get("student_number","").strip(); name=request.form.get("name","").strip(); pw=request.form.get("password","")
        if not n or not name or not pw: flash("학번, 이름, 비밀번호를 입력해 주세요."); return render_template("login.html")
        if not n.isdigit(): flash("학번은 숫자로 입력해 주세요."); return render_template("login.html")
        conn=get_db(); row=conn.execute("SELECT * FROM students WHERE student_number=?",(n,)).fetchone()
        if row:
            if row["password"] != pw: conn.close(); flash("비밀번호가 맞지 않습니다."); return render_template("login.html")
            token=secrets.token_urlsafe(32); conn.execute("UPDATE students SET name=?,session_token=?,last_seen=? WHERE student_number=?",(name,token,int(datetime.now(timezone.utc).timestamp()),n))
        else:
            token=secrets.token_urlsafe(32); conn.execute("INSERT INTO students(student_number,name,password,session_token,last_seen) VALUES(?,?,?,?,?)",(n,name,"1234",token,int(datetime.now(timezone.utc).timestamp())))
        conn.commit(); conn.close(); session.clear(); session.update(student_number=n,student_name=name,student_token=token); return redirect(url_for("student"))
    return render_template("login.html")

@app.route("/student")
@student_required
def student():
    conn=get_db(); me=conn.execute("SELECT * FROM students WHERE student_number=?",(session["student_number"],)).fetchone(); group=me["group_name"]
    members=conn.execute("SELECT student_number,name FROM students WHERE group_name=? AND student_number<>? ORDER BY student_number",(group,session["student_number"])).fetchall() if group else []
    received=conn.execute("SELECT * FROM feedback WHERE target_number=? ORDER BY id DESC",(session["student_number"],)).fetchall(); sent=conn.execute("SELECT * FROM feedback WHERE sender_number=? ORDER BY id DESC",(session["student_number"],)).fetchall(); avg=conn.execute("SELECT AVG(score) a FROM feedback WHERE target_number=?",(session["student_number"],)).fetchone()["a"]; peer_avg=conn.execute("SELECT AVG(score) a FROM feedback WHERE target_number=? AND evaluation_type='peer'",(session["student_number"],)).fetchone()["a"]; presenter_avg=conn.execute("SELECT AVG(score) a FROM feedback WHERE target_number=? AND evaluation_type='presenter'",(session["student_number"],)).fetchone()["a"]
    conn.close(); return render_template("student.html",me=me,members=members,received=received,sent=sent,avg=avg,peer_avg=peer_avg,presenter_avg=presenter_avg)

@app.route("/api/feedback",methods=["POST"])
@student_required
def add_feedback():
    target=request.form.get("target_number","").strip(); content=request.form.get("content","").strip(); evaluation_type=request.form.get("evaluation_type","peer").strip()
    if evaluation_type not in ("peer","presenter"): evaluation_type="peer"
    try: score=int(request.form.get("score",""))
    except: score=0
    if not target or not 1<=score<=5 or not content: return jsonify(ok=False,message="학번, 점수(1~5), 평가 내용을 확인해 주세요."),400
    conn=get_db(); sender=conn.execute("SELECT * FROM students WHERE student_number=?",(session["student_number"],)).fetchone(); target_row=conn.execute("SELECT * FROM students WHERE student_number=?",(target,)).fetchone()
    if not target_row: conn.close(); return jsonify(ok=False,message="존재하지 않는 학생입니다."),404
    if target==session["student_number"]: conn.close(); return jsonify(ok=False,message="자기 자신은 평가할 수 없습니다."),403
    if evaluation_type=="peer" and (not sender["group_name"] or sender["group_name"]!=target_row["group_name"]): conn.close(); return jsonify(ok=False,message="조원 평가는 같은 조원만 가능합니다."),403
    # 한 학생이 같은 조원을 여러 번 평가하지 않고 기존 평가를 수정
    existing=conn.execute("SELECT id FROM feedback WHERE sender_number=? AND target_number=? AND evaluation_type=? ORDER BY id DESC LIMIT 1",(sender["student_number"],target,evaluation_type)).fetchone()
    created=now_iso()
    if existing: conn.execute("UPDATE feedback SET score=?,content=?,created_at=? WHERE id=?",(score,content,created,existing["id"]))
    else: conn.execute("INSERT INTO feedback(sender_number,sender_name,target_number,target_name,score,content,evaluation_type,created_at) VALUES(?,?,?,?,?,?,?,?)",(sender["student_number"],sender["name"],target,target_row["name"],score,content,evaluation_type,created))
    conn.commit(); conn.close(); sheet_sync("feedback",{"sender_number":sender["student_number"],"target_number":target,"score":score,"content":content,"evaluation_type":evaluation_type,"created_at":created}); return jsonify(ok=True)

@app.route("/api/feedback/<int:feedback_id>/reply",methods=["POST"])
@student_required
def reply_feedback(feedback_id):
    reply=request.form.get("reply","").strip()
    if len(reply)>1000: return jsonify(ok=False,message="답변은 1000자 이하입니다."),400
    conn=get_db(); row=conn.execute("SELECT * FROM feedback WHERE id=? AND target_number=?",(feedback_id,session["student_number"])).fetchone()
    if not row: conn.close(); return jsonify(ok=False,message="평가를 찾을 수 없습니다."),404
    conn.execute("UPDATE feedback SET reply=? WHERE id=?",(reply,feedback_id)); conn.commit(); conn.close(); return jsonify(ok=True)

@app.route("/api/student-search")
@student_required
def student_search():
    q=request.args.get("q","").strip(); conn=get_db(); r=conn.execute("SELECT student_number,name,group_name FROM students WHERE student_number=?",(q,)).fetchone(); conn.close()
    if not r: return jsonify(found=False)
    me=get_student(session["student_number"]); ok=bool(me and r["student_number"]!=me["student_number"] and (request.args.get("type","peer")=="presenter" or (me["group_name"] and me["group_name"]==r["group_name"])))
    return jsonify(found=ok,student_number=r["student_number"],name=r["name"],group_name=r["group_name"])

@app.route("/teacher/login",methods=["GET","POST"])
def teacher_login():
    if request.method=="POST":
        if request.form.get("password")==TEACHER_PASSWORD: session.clear(); session["teacher"]=True; return redirect(url_for("teacher"))
        flash("비밀번호가 맞지 않습니다.")
    return render_template("teacher_login.html")

@app.route("/teacher")
@teacher_required
def teacher():
    conn=get_db(); students=conn.execute("SELECT s.student_number,s.name,s.group_name,AVG(f.score) avg_score,AVG(CASE WHEN f.evaluation_type='peer' THEN f.score END) peer_avg,AVG(CASE WHEN f.evaluation_type='presenter' THEN f.score END) presenter_avg,COUNT(f.id) feedback_count FROM students s LEFT JOIN feedback f ON f.target_number=s.student_number GROUP BY s.student_number ORDER BY s.student_number").fetchall(); feedbacks=conn.execute("SELECT * FROM feedback ORDER BY id DESC").fetchall(); conn.close(); return render_template("teacher.html",students=students,feedbacks=feedbacks)

@app.route("/teacher/group",methods=["POST"])
@teacher_required
def set_group():
    n=request.form.get("student_number","").strip(); g=request.form.get("group_name","").strip()[:30]
    conn=get_db(); r=conn.execute("SELECT name FROM students WHERE student_number=?",(n,)).fetchone()
    if not r: conn.close(); flash("학생을 찾을 수 없습니다."); return redirect(url_for("teacher"))
    conn.execute("UPDATE students SET group_name=? WHERE student_number=?",(g,n)); conn.commit(); conn.close(); flash(f"{n} {r['name']} 학생을 {g or '조 없음'}으로 지정했습니다."); return redirect(url_for("teacher"))

@app.route("/teacher/student/<student_number>/delete",methods=["POST"])
@teacher_required
def delete_student(student_number):
    conn=get_db(); r=conn.execute("SELECT name FROM students WHERE student_number=?",(student_number,)).fetchone()
    if not r: conn.close(); abort(404)
    conn.execute("DELETE FROM feedback WHERE sender_number=? OR target_number=?",(student_number,student_number)); conn.execute("DELETE FROM students WHERE student_number=?",(student_number,)); conn.commit(); conn.close(); flash(f"{student_number} {r['name']} 학생 계정을 삭제했습니다."); return redirect(url_for("teacher"))

@app.route("/teacher/student/<student_number>/force-logout",methods=["POST"])
@teacher_required
def force_logout(student_number):
    conn=get_db(); conn.execute("UPDATE students SET session_token=NULL WHERE student_number=?",(student_number,)); conn.commit(); conn.close(); flash("학생 접속을 강제로 종료했습니다."); return redirect(url_for("teacher"))

@app.route("/teacher/clear-feedbacks",methods=["POST"])
@teacher_required
def clear_feedbacks():
    conn=get_db(); conn.execute("DELETE FROM feedback"); conn.commit(); conn.close(); flash("모든 평가 내역을 초기화했습니다."); return redirect(url_for("teacher"))

@app.route("/teacher/student/<student_number>")
@teacher_required
def teacher_student(student_number):
    conn=get_db(); student=conn.execute("SELECT * FROM students WHERE student_number=?",(student_number,)).fetchone(); rows=conn.execute("SELECT * FROM feedback WHERE target_number=? ORDER BY id DESC",(student_number,)).fetchall() if student else [] ; avg=conn.execute("SELECT AVG(score) a FROM feedback WHERE target_number=?",(student_number,)).fetchone()["a"] if student else None; peer_avg=conn.execute("SELECT AVG(score) a FROM feedback WHERE target_number=? AND evaluation_type='peer'",(student_number,)).fetchone()["a"] if student else None; presenter_avg=conn.execute("SELECT AVG(score) a FROM feedback WHERE target_number=? AND evaluation_type='presenter'",(student_number,)).fetchone()["a"] if student else None; conn.close()
    if not student: abort(404)
    return render_template("teacher_student.html",student=student,feedbacks=rows,avg=avg,peer_avg=peer_avg,presenter_avg=presenter_avg)

@app.route("/teacher/feedbacks")
@teacher_required
def teacher_feedbacks():
    conn=get_db(); rows=conn.execute("SELECT * FROM feedback ORDER BY id DESC").fetchall(); conn.close(); return jsonify([{**dict(r),"created_at":format_kst(r["created_at"])} for r in rows])

@app.route("/logout")
def logout(): session.clear(); return redirect(url_for("index"))

init_db()
if __name__=="__main__": app.run(host=os.environ.get("HOST","0.0.0.0"),port=int(os.environ.get("PORT","5000")),debug=False)
