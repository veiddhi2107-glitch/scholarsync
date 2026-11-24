from datetime import date, datetime
from functools import wraps
import os
import re
import sqlite3
from urllib.parse import urlparse
from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-scholarsync-secret-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "scholarsync.db")


def init_db_if_needed():
    if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0:
        conn = sqlite3.connect(DB_PATH)
        try:
            schema_path = os.path.join(BASE_DIR, "schema.sql")
            seed_path = os.path.join(BASE_DIR, "seed.sql")
            if os.path.exists(schema_path):
                with open(schema_path, "r", encoding="utf-8") as f:
                    conn.executescript(f.read())
            if os.path.exists(seed_path):
                with open(seed_path, "r", encoding="utf-8") as f:
                    conn.executescript(f.read())
            conn.commit()
        finally:
            conn.close()


init_db_if_needed()

APPLICATION_STATUSES = [
    "Applied",
    "Under Review",
    "Shortlisted",
    "Selected",
    "Rejected",
    "Withdrawn",
]
ALL_TRACKABLE_STATUSES = [
    "Interested",
    "Saved",
    "Applied",
    "Under Review",
    "Shortlisted",
    "Selected",
    "Rejected",
    "Withdrawn",
]
ELIGIBILITY_STATUS_CHOICES = [
    "Eligible",
    "Potentially Eligible",
    "Insufficient Information",
    "Not Eligible",
]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON;")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def ensure_schema_updates():
    if not os.path.exists(DB_PATH):
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if cols:
            if "graduation_year" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN graduation_year INTEGER;")
            if "is_admin" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0;")
            conn.commit()
    finally:
        conn.close()


ensure_schema_updates()


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def login_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if session.get("user_id") is None:
            flash("Please sign in to access that page.", "info")
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapped_view


def admin_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if session.get("user_id") is None:
            flash("Please sign in as an administrator to access that page.", "info")
            return redirect(url_for("login"))
        user = get_current_user()
        if not user or not user["is_admin"]:
            flash("Administrator privileges are required to view that page.", "error")
            return redirect(url_for("dashboard"))
        return view_func(*args, **kwargs)
    return wrapped_view


def is_safe_local_url(target_url):
    if not target_url or not isinstance(target_url, str):
        return False
    parsed = urlparse(target_url)
    return parsed.scheme == "" and parsed.netloc == "" and target_url.startswith("/") and not target_url.startswith("//")


def sanitize_apply_url(raw_url):
    cleaned = (raw_url or "").strip()
    if not cleaned:
        return ""
    parsed = urlparse(cleaned)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return cleaned
    return ""


def parse_deadline_date(deadline_str):
    if not deadline_str:
        return None
    cleaned = str(deadline_str).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def format_deadline_countdown(deadline_str):
    due_date = parse_deadline_date(deadline_str)
    if due_date is None:
        return None, deadline_str or "No deadline", None
    delta_days = (due_date - date.today()).days
    if delta_days < 0:
        return due_date, f"Closed ({abs(delta_days)}d ago)", delta_days
    if delta_days == 0:
        return due_date, "Due today", 0
    if delta_days == 1:
        return due_date, "1 day left", 1
    return due_date, f"{delta_days} days left", delta_days


def sync_saved_deadline_notifications(db, user_id, window_days=7):
    saved_opps = db.execute(
        """
        SELECT o.id, o.title, o.deadline
        FROM saved_opportunities so
        JOIN opportunities o ON o.id = so.opportunity_id
        WHERE so.user_id = ?
        """,
        (user_id,),
    ).fetchall()

    inserted_any = False
    for opp in saved_opps:
        _, _, days_left = format_deadline_countdown(opp["deadline"])
        if days_left is not None and 0 <= days_left <= window_days:
            if days_left == 0:
                msg = f"Deadline alert: '{opp['title']}' (saved) is due today ({opp['deadline']})."
            elif days_left == 1:
                msg = f"Deadline alert: '{opp['title']}' (saved) has 1 day left ({opp['deadline']})."
            else:
                msg = f"Deadline alert: '{opp['title']}' (saved) is due in {days_left} days ({opp['deadline']})."

            existing = db.execute(
                "SELECT 1 FROM notifications WHERE user_id = ? AND message = ?",
                (user_id, msg),
            ).fetchone()
            if not existing:
                db.execute(
                    "INSERT INTO notifications (user_id, message, is_read) VALUES (?, ?, 0)",
                    (user_id, msg),
                )
                inserted_any = True

    if inserted_any:
        db.commit()


@app.context_processor
def inject_globals():
    user = get_current_user()
    unread_notifications = 0
    if user:
        db = get_db()
        sync_saved_deadline_notifications(db, user["id"])
        unread_notifications = db.execute(
            "SELECT COUNT(*) AS cnt FROM notifications WHERE user_id = ? AND is_read = 0",
            (user["id"],),
        ).fetchone()["cnt"]
    return {
        "current_user": user,
        "unread_notifications": unread_notifications,
    }


def split_tokens(raw_text):
    if not raw_text:
        return []
    return [t.strip() for t in re.split(r"[,;/|]+", str(raw_text)) if t.strip()]


def is_paid_stipend(stipend_val):
    if not stipend_val:
        return False
    cleaned = str(stipend_val).strip().lower()
    if not cleaned or cleaned in ("unpaid", "none", "n/a", "na", "0", "free", "not specified"):
        return False
    if "unpaid" in cleaned or "no stipend" in cleaned:
        return False
    return bool(re.search(r"[0-9₹$]|stipend|paid|grant|scholarship|award|fellowship|prize", cleaned))


def evaluate_smart_eligibility(user, skill_names, interest_names, opp):
    reasons = []
    blockers = []
    missing_profile = []

    user_degree = (user["degree"] or "").strip()
    user_year = user["academic_year"]
    user_field = (user["field_of_study"] or "").strip()
    user_skills_lower = {s.lower() for s in skill_names}

    target_degree = (opp["target_degree"] or "Any").strip()
    min_year = opp["min_year"] if opp["min_year"] is not None else 1
    max_year = opp["max_year"] if opp["max_year"] is not None else 5
    req_skills = split_tokens(opp["required_skills"])
    elig_text = (opp["eligibility_text"] or "").strip()

    earned_points = 0
    possible_points = 0

    possible_points += 30
    if user_year is None:
        missing_profile.append("Academic year is missing from your profile.")
    elif min_year <= user_year <= max_year:
        earned_points += 30
        reasons.append(f"Academic Year {user_year} is within the allowed Year {min_year}–{max_year} range.")
    else:
        blockers.append(f"Requires Academic Year {min_year}–{max_year} (your profile is Year {user_year}).")

    possible_points += 25
    if target_degree.lower() in ("any", "all", "open", ""):
        earned_points += 25
        reasons.append("Open to all undergraduate/postgraduate degrees.")
    elif not user_degree:
        missing_profile.append("Degree is missing from your profile.")
    else:
        deg_tokens = [d.lower() for d in split_tokens(target_degree)] or [target_degree.lower()]
        u_deg_l = user_degree.lower()
        if any(tok in u_deg_l or u_deg_l in tok for tok in deg_tokens):
            earned_points += 25
            reasons.append(f"Degree ({user_degree}) matches target requirement ({target_degree}).")
        else:
            blockers.append(f"Targets {target_degree} students (your degree is {user_degree}).")

    possible_points += 25
    matched_skills = []
    if not req_skills:
        earned_points += 25
        reasons.append("No mandatory prerequisite skills listed.")
    elif not skill_names:
        missing_profile.append(f"Add skills to verify match against required skills: {', '.join(req_skills)}.")
    else:
        matched_skills = [
            rs for rs in req_skills
            if any(rs.lower() in us or us in rs.lower() for us in user_skills_lower)
        ]
        unmatched_skills = [rs for rs in req_skills if rs not in matched_skills]
        earned_points += round(25 * (len(matched_skills) / len(req_skills)))
        if matched_skills:
            reasons.append(f"Matched {len(matched_skills)}/{len(req_skills)} required skills: {', '.join(matched_skills)}.")
        if unmatched_skills:
            reasons.append(f"Unmatched listed skills: {', '.join(unmatched_skills)}.")

    possible_points += 20
    searchable = " ".join([
        (opp["title"] or "").lower(),
        (opp["category"] or "").lower(),
        (opp["description"] or "").lower(),
        elig_text.lower(),
    ])
    field_matched = bool(user_field) and (
        user_field.lower() in searchable
        or any(word in searchable for word in user_field.lower().split() if len(word) > 3)
    )
    matched_interests = [i for i in interest_names if i.lower() in searchable]

    if not user_field:
        missing_profile.append("Field of study is missing from your profile.")
    elif field_matched:
        earned_points += 14
        reasons.append(f"Field of study ({user_field}) aligns with this listing.")
    else:
        earned_points += 6
        reasons.append(f"General domain match for {user_field}.")

    if matched_interests:
        earned_points += 6
        reasons.append(f"Aligns with your interests: {', '.join(matched_interests[:3])}.")
    elif not interest_names:
        missing_profile.append("Add interests to improve domain relevance scoring.")

    if elig_text:
        reasons.append(f"Eligibility criteria: {elig_text}")

    match_percent = round((earned_points / possible_points) * 100) if possible_points else 0

    if blockers:
        status, badge_tone = "Not Eligible", "not-eligible"
    elif missing_profile and (not skill_names or not user_degree or user_year is None):
        status, badge_tone = "Insufficient Information", "insufficient"
    elif (req_skills and len(matched_skills) < len(req_skills)) or (
        elig_text and any(kw in elig_text.lower() for kw in ("cgpa", "gpa", "percentage", "%", "income", "domicile", "girls", "women"))
    ):
        status, badge_tone = "Potentially Eligible", "potential"
    else:
        status, badge_tone = "Eligible", "eligible"

    return {
        "status": status,
        "badge_tone": badge_tone,
        "match_percent": match_percent,
        "reasons": reasons,
        "blockers": blockers,
        "missing_profile": missing_profile,
    }


def compute_profile_completion(user, skill_names, interest_names):
    checks = [
        ("Full Name", bool((user["full_name"] or "").strip())),
        ("Email", bool((user["email"] or "").strip())),
        ("College / University", bool((user["college"] or "").strip())),
        ("Degree", bool((user["degree"] or "").strip())),
        ("Academic Year", user["academic_year"] is not None),
        ("Graduation Year", user["graduation_year"] is not None),
        ("Field of Study", bool((user["field_of_study"] or "").strip())),
        ("Skills", len(skill_names) > 0),
        ("Interests", len(interest_names) > 0),
    ]
    completed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    return {
        "percent": round((completed / total) * 100),
        "completed": completed,
        "total": total,
        "missing": [label for label, ok in checks if not ok],
    }


def get_user_skills_and_interests(db, user_id):
    skill_rows = db.execute(
        """
        SELECT s.id, s.name
        FROM skills s
        JOIN user_skills us ON us.skill_id = s.id
        WHERE us.user_id = ?
        ORDER BY s.name
        """,
        (user_id,),
    ).fetchall()

    interest_rows = db.execute(
        """
        SELECT i.id, i.name
        FROM interests i
        JOIN user_interests ui ON ui.interest_id = i.id
        WHERE ui.user_id = ?
        ORDER BY i.name
        """,
        (user_id,),
    ).fetchall()
    return skill_rows, interest_rows


def build_recommendations(user, skill_names, interest_names, opportunities):
    status_priority = {
        "Eligible": 0,
        "Potentially Eligible": 1,
        "Insufficient Information": 2,
        "Not Eligible": 3,
    }
    scored = []
    for opp in opportunities:
        eval_result = evaluate_smart_eligibility(user, skill_names, interest_names, opp)
        _, countdown_label, days_left = format_deadline_countdown(opp["deadline"])
        scored.append({
            "opp": opp,
            "eval": eval_result,
            "countdown": countdown_label,
            "days_left": days_left,
        })
    scored.sort(
        key=lambda item: (
            status_priority.get(item["eval"]["status"], 4),
            -item["eval"]["match_percent"],
            item["days_left"] if item["days_left"] is not None and item["days_left"] >= 0 else 999999,
        )
    )
    return scored


def parse_opportunity_form(form):
    title = form.get("title", "").strip()
    organization = form.get("organization", "").strip()
    category = form.get("category", "").strip()
    description = form.get("description", "").strip()
    deadline = form.get("deadline", "").strip()
    mode = form.get("mode", "").strip() or "Remote"
    location = form.get("location", "").strip()
    stipend = form.get("stipend", "").strip()
    target_degree = form.get("target_degree", "").strip() or "Any"
    required_skills = form.get("required_skills", "").strip()
    eligibility_text = form.get("eligibility_text", "").strip()
    apply_url = sanitize_apply_url(form.get("apply_url", ""))
    is_verified = 1 if form.get("is_verified") in ("1", "true", "on", "yes") else 0

    errors = []
    if not title:
        errors.append("Opportunity title is required.")
    if not organization:
        errors.append("Organization name is required.")
    if not category:
        errors.append("Category is required.")
    if not description:
        errors.append("Description is required.")

    try:
        min_year = int(form.get("min_year", "1").strip() or "1")
        max_year = int(form.get("max_year", "5").strip() or "5")
        if min_year < 1 or max_year > 5 or min_year > max_year:
            errors.append("Academic year range must be valid within Years 1 to 5.")
    except ValueError:
        min_year, max_year = 1, 5
        errors.append("Minimum and maximum academic years must be numbers (1–5).")

    data = {
        "title": title,
        "organization": organization,
        "category": category,
        "description": description,
        "deadline": deadline,
        "mode": mode,
        "location": location,
        "stipend": stipend,
        "target_degree": target_degree,
        "min_year": min_year,
        "max_year": max_year,
        "required_skills": required_skills,
        "eligibility_text": eligibility_text,
        "apply_url": apply_url,
        "is_verified": is_verified,
    }
    return data, errors


@app.route("/")
def index():
    db = get_db()
    opp_count = db.execute("SELECT COUNT(*) AS cnt FROM opportunities").fetchone()["cnt"]
    return render_template("index.html", opp_count=opp_count)


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        college = request.form.get("college", "").strip()
        degree = request.form.get("degree", "").strip()
        field_of_study = request.form.get("field_of_study", "").strip()
        year_raw = request.form.get("academic_year", "").strip()

        errors = []
        if not full_name:
            errors.append("Full name is required.")
        if not email or "@" not in email:
            errors.append("Please enter a valid email address.")
        if len(password) < 6:
            errors.append("Password must be at least 6 characters long.")
        if password != confirm_password:
            errors.append("Passwords do not match.")
        if not college:
            errors.append("College or university name is required.")
        if not degree:
            errors.append("Current degree is required.")
        if not field_of_study:
            errors.append("Field of study is required.")

        try:
            academic_year = int(year_raw)
            if academic_year < 1 or academic_year > 5:
                errors.append("Academic year must be between 1 and 5.")
        except ValueError:
            errors.append("Please select a valid academic year.")
            academic_year = 2

        db = get_db()
        if not errors and db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
            errors.append("An account with this email already exists. Please log in.")

        if errors:
            for err in errors:
                flash(err, "error")
            return render_template(
                "register.html",
                form_data={
                    "full_name": full_name,
                    "email": email,
                    "college": college,
                    "degree": degree,
                    "academic_year": academic_year,
                    "field_of_study": field_of_study,
                },
            )

        cursor = db.execute(
            """
            INSERT INTO users (full_name, email, password_hash, college, degree, academic_year, field_of_study, is_admin)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (full_name, email, generate_password_hash(password), college, degree, academic_year, field_of_study),
        )
        db.commit()
        session.clear()
        session["user_id"] = cursor.lastrowid
        flash(f"Welcome to ScholarSync, {full_name}! Your account is ready.", "info")
        return redirect(url_for("dashboard"))

    return render_template("register.html", form_data={})


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not email or not password:
            flash("Please enter both your email and password.", "error")
            return render_template("login.html", email=email)

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Invalid email or password.", "error")
            return render_template("login.html", email=email)

        session.clear()
        session["user_id"] = user["id"]
        flash(f"Welcome back, {user['full_name']}!", "info")
        return redirect(url_for("admin_dashboard") if user["is_admin"] else url_for("dashboard"))

    return render_template("login.html", email="")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required
def dashboard():
    user = get_current_user()
    db = get_db()
    user_id = user["id"]
    active_tab = request.args.get("tab", "overview")
    sync_saved_deadline_notifications(db, user_id)

    skill_rows, interest_rows = get_user_skills_and_interests(db, user_id)
    skill_names = [r["name"] for r in skill_rows]
    interest_names = [r["name"] for r in interest_rows]
    profile_completion = compute_profile_completion(user, skill_names, interest_names)

    all_opportunities = db.execute("SELECT * FROM opportunities ORDER BY id DESC").fetchall()
    recommended_items = build_recommendations(user, skill_names, interest_names, all_opportunities)

    upcoming_deadlines = []
    for opp in all_opportunities:
        due_date, countdown_label, days_left = format_deadline_countdown(opp["deadline"])
        eval_result = evaluate_smart_eligibility(user, skill_names, interest_names, opp)
        if days_left is not None and days_left >= 0:
            upcoming_deadlines.append({
                "opp": opp,
                "due_date": due_date,
                "countdown": countdown_label,
                "days_left": days_left,
                "eval": eval_result,
            })
    upcoming_deadlines.sort(key=lambda x: x["days_left"])

    application_rows = db.execute(
        """
        SELECT a.*, o.title, o.organization, o.category, o.deadline, o.mode
        FROM applications a
        JOIN opportunities o ON o.id = a.opportunity_id
        WHERE a.user_id = ?
        ORDER BY a.updated_at DESC, a.id DESC
        """,
        (user_id,),
    ).fetchall()

    status_counts = {st: 0 for st in APPLICATION_STATUSES}
    for row in application_rows:
        if row["status"] in status_counts:
            status_counts[row["status"]] += 1

    saved_rows = db.execute(
        """
        SELECT so.id AS saved_id, so.saved_at, o.*
        FROM saved_opportunities so
        JOIN opportunities o ON o.id = so.opportunity_id
        WHERE so.user_id = ?
        ORDER BY so.saved_at DESC, so.id DESC
        """,
        (user_id,),
    ).fetchall()

    saved_with_meta = []
    for srow in saved_rows:
        _, countdown_label, _ = format_deadline_countdown(srow["deadline"])
        eval_result = evaluate_smart_eligibility(user, skill_names, interest_names, srow)
        saved_with_meta.append({
            "opp": srow,
            "saved_at": srow["saved_at"],
            "countdown": countdown_label,
            "eval": eval_result,
        })

    notifications = db.execute(
        """
        SELECT *
        FROM notifications
        WHERE user_id = ?
        ORDER BY is_read ASC, created_at DESC, id DESC
        """,
        (user_id,),
    ).fetchall()

    return render_template(
        "dashboard.html",
        user=user,
        active_tab=active_tab,
        skill_rows=skill_rows,
        interest_rows=interest_rows,
        skill_names=skill_names,
        interest_names=interest_names,
        skills_text=", ".join(skill_names),
        interests_text=", ".join(interest_names),
        profile_completion=profile_completion,
        recommended_items=recommended_items,
        upcoming_deadlines=upcoming_deadlines,
        applications=application_rows,
        total_applications=len(application_rows),
        status_counts=status_counts,
        application_statuses=APPLICATION_STATUSES,
        saved_items=saved_with_meta,
        saved_count=len(saved_with_meta),
        notifications=notifications,
    )


@app.route("/dashboard/profile", methods=["POST"])
@login_required
def update_profile():
    user_id = session["user_id"]
    full_name = request.form.get("full_name", "").strip()
    college = request.form.get("college", "").strip()
    degree = request.form.get("degree", "").strip()
    field_of_study = request.form.get("field_of_study", "").strip()
    year_raw = request.form.get("academic_year", "").strip()
    grad_year_raw = request.form.get("graduation_year", "").strip()
    skills_raw = request.form.get("skills", "").strip()
    interests_raw = request.form.get("interests", "").strip()

    if not full_name or not college or not degree or not field_of_study:
        flash("Please fill in your name, college, degree, and field of study.", "error")
        return redirect(url_for("dashboard", tab="profile"))

    try:
        academic_year = int(year_raw)
        if academic_year < 1 or academic_year > 5:
            raise ValueError()
    except ValueError:
        flash("Academic year must be between 1 and 5.", "error")
        return redirect(url_for("dashboard", tab="profile"))

    graduation_year = None
    if grad_year_raw:
        try:
            graduation_year = int(grad_year_raw)
            if graduation_year < 2020 or graduation_year > 2035:
                raise ValueError()
        except ValueError:
            flash("Please enter a valid graduation year between 2020 and 2035.", "error")
            return redirect(url_for("dashboard", tab="profile"))

    db = get_db()
    db.execute(
        """
        UPDATE users
        SET full_name = ?, college = ?, degree = ?, academic_year = ?, graduation_year = ?, field_of_study = ?
        WHERE id = ?
        """,
        (full_name, college, degree, academic_year, graduation_year, field_of_study, user_id),
    )

    if "skills" in request.form:
        db.execute("DELETE FROM user_skills WHERE user_id = ?", (user_id,))
        for s_name in dict.fromkeys([s.strip() for s in skills_raw.split(",") if s.strip()]):
            row = db.execute("SELECT id FROM skills WHERE LOWER(name) = LOWER(?)", (s_name,)).fetchone()
            skill_id = row["id"] if row else db.execute("INSERT INTO skills (name) VALUES (?)", (s_name,)).lastrowid
            db.execute("INSERT OR IGNORE INTO user_skills (user_id, skill_id) VALUES (?, ?)", (user_id, skill_id))

    if "interests" in request.form:
        db.execute("DELETE FROM user_interests WHERE user_id = ?", (user_id,))
        for i_name in dict.fromkeys([i.strip() for i in interests_raw.split(",") if i.strip()]):
            row = db.execute("SELECT id FROM interests WHERE LOWER(name) = LOWER(?)", (i_name,)).fetchone()
            interest_id = row["id"] if row else db.execute("INSERT INTO interests (name) VALUES (?)", (i_name,)).lastrowid
            db.execute("INSERT OR IGNORE INTO user_interests (user_id, interest_id) VALUES (?, ?)", (user_id, interest_id))

    db.commit()
    flash("Academic profile updated.", "info")
    return redirect(url_for("dashboard", tab="profile"))


@app.route("/dashboard/profile/skills/add", methods=["POST"])
@login_required
def add_user_skill():
    user_id = session["user_id"]
    skill_name = request.form.get("skill_name", "").strip()
    if not skill_name:
        flash("Please enter a skill name.", "error")
        return redirect(url_for("dashboard", tab="profile"))
    db = get_db()
    row = db.execute("SELECT id FROM skills WHERE LOWER(name) = LOWER(?)", (skill_name,)).fetchone()
    skill_id = row["id"] if row else db.execute("INSERT INTO skills (name) VALUES (?)", (skill_name,)).lastrowid
    db.execute("INSERT OR IGNORE INTO user_skills (user_id, skill_id) VALUES (?, ?)", (user_id, skill_id))
    db.commit()
    flash(f"Added skill: {skill_name}", "info")
    return redirect(url_for("dashboard", tab="profile"))


@app.route("/dashboard/profile/skills/<int:skill_id>/remove", methods=["POST"])
@login_required
def remove_user_skill(skill_id):
    db = get_db()
    db.execute("DELETE FROM user_skills WHERE user_id = ? AND skill_id = ?", (session["user_id"], skill_id))
    db.commit()
    flash("Skill removed.", "info")
    return redirect(url_for("dashboard", tab="profile"))


@app.route("/dashboard/profile/interests/add", methods=["POST"])
@login_required
def add_user_interest():
    user_id = session["user_id"]
    interest_name = request.form.get("interest_name", "").strip()
    if not interest_name:
        flash("Please enter an interest.", "error")
        return redirect(url_for("dashboard", tab="profile"))
    db = get_db()
    row = db.execute("SELECT id FROM interests WHERE LOWER(name) = LOWER(?)", (interest_name,)).fetchone()
    interest_id = row["id"] if row else db.execute("INSERT INTO interests (name) VALUES (?)", (interest_name,)).lastrowid
    db.execute("INSERT OR IGNORE INTO user_interests (user_id, interest_id) VALUES (?, ?)", (user_id, interest_id))
    db.commit()
    flash(f"Added interest: {interest_name}", "info")
    return redirect(url_for("dashboard", tab="profile"))


@app.route("/dashboard/profile/interests/<int:interest_id>/remove", methods=["POST"])
@login_required
def remove_user_interest(interest_id):
    db = get_db()
    db.execute("DELETE FROM user_interests WHERE user_id = ? AND interest_id = ?", (session["user_id"], interest_id))
    db.commit()
    flash("Interest removed.", "info")
    return redirect(url_for("dashboard", tab="profile"))


@app.route("/dashboard/saved/<int:opp_id>/remove", methods=["POST"])
@login_required
def remove_saved_opportunity(opp_id):
    db = get_db()
    db.execute("DELETE FROM saved_opportunities WHERE user_id = ? AND opportunity_id = ?", (session["user_id"], opp_id))
    db.commit()
    flash("Bookmark removed.", "info")
    return redirect(url_for("dashboard", tab="saved"))


@app.route("/dashboard/applications/<int:opp_id>/status", methods=["POST"])
@login_required
def update_application_status(opp_id):
    user_id = session["user_id"]
    new_status = request.form.get("status", "").strip()
    if new_status not in ALL_TRACKABLE_STATUSES:
        flash("Invalid status selected.", "error")
        return redirect(url_for("dashboard", tab="applications"))

    app_date = request.form.get("application_date", "").strip() or None
    notes = request.form.get("notes", "").strip() or None
    if new_status == "Applied" and not app_date:
        app_date = date.today().isoformat()

    db = get_db()
    updated = db.execute(
        """
        UPDATE applications
        SET status = ?,
            application_date = COALESCE(?, application_date),
            notes = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ? AND opportunity_id = ?
        """,
        (new_status, app_date, notes, user_id, opp_id),
    )
    db.commit()

    if updated.rowcount == 0:
        flash("Application record not found for your account.", "error")
    else:
        flash(f"Application updated to '{new_status}'.", "info")
    return redirect(url_for("dashboard", tab="applications"))


@app.route("/dashboard/notifications/read", methods=["POST"])
@login_required
def mark_notifications_read():
    user_id = session["user_id"]
    notif_id = request.form.get("notif_id")
    db = get_db()
    if notif_id:
        db.execute("UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?", (notif_id, user_id))
    else:
        db.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (user_id,))
    db.commit()
    flash("Notifications marked as read.", "info")
    return redirect(url_for("dashboard", tab="notifications"))


@app.route("/explore")
def explore():
    db = get_db()
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    mode = request.args.get("mode", "").strip()
    year_filter = request.args.get("year", "").strip()
    compensation = request.args.get("compensation", "").strip()
    eligibility_filter = request.args.get("eligibility", "").strip()
    sort_by = request.args.get("sort", "relevance").strip()
    if sort_by not in ("relevance", "deadline", "recently_added"):
        sort_by = "relevance"

    query = "SELECT * FROM opportunities WHERE 1=1"
    params = []

    if q:
        query += (
            " AND (title LIKE ? OR organization LIKE ? OR description LIKE ? "
            "OR category LIKE ? OR location LIKE ? OR required_skills LIKE ?)"
        )
        params.extend([f"%{q}%"] * 6)

    if category:
        query += " AND LOWER(category) = LOWER(?)"
        params.append(category)

    if mode:
        query += " AND LOWER(mode) = LOWER(?)"
        params.append(mode)

    selected_year = None
    if year_filter:
        try:
            selected_year = int(year_filter)
            if 1 <= selected_year <= 5:
                query += " AND COALESCE(min_year, 1) <= ? AND COALESCE(max_year, 5) >= ?"
                params.extend([selected_year, selected_year])
            else:
                selected_year = None
        except ValueError:
            selected_year = None

    raw_opps = db.execute(query, params).fetchall()

    categories = [
        r["category"]
        for r in db.execute(
            "SELECT DISTINCT category FROM opportunities WHERE category IS NOT NULL AND category != '' ORDER BY category"
        ).fetchall()
    ]
    modes = [
        r["mode"]
        for r in db.execute(
            "SELECT DISTINCT mode FROM opportunities WHERE mode IS NOT NULL AND mode != '' ORDER BY mode"
        ).fetchall()
    ]

    user = get_current_user()
    saved_ids = set()
    skill_names = []
    interest_names = []
    if user:
        saved_ids = {
            r["opportunity_id"]
            for r in db.execute("SELECT opportunity_id FROM saved_opportunities WHERE user_id = ?", (user["id"],)).fetchall()
        }
        skill_rows, interest_rows = get_user_skills_and_interests(db, user["id"])
        skill_names = [r["name"] for r in skill_rows]
        interest_names = [r["name"] for r in interest_rows]

    items = []
    for opp in raw_opps:
        paid = is_paid_stipend(opp["stipend"])
        if compensation == "paid" and not paid:
            continue
        if compensation == "unpaid_or_unspecified" and paid:
            continue

        eval_result = None
        if user:
            eval_result = evaluate_smart_eligibility(user, skill_names, interest_names, opp)
            if eligibility_filter and eval_result["status"] != eligibility_filter:
                continue

        due_date, countdown_label, days_left = format_deadline_countdown(opp["deadline"])
        items.append({
            "opp": opp,
            "eval": eval_result,
            "countdown": countdown_label,
            "days_left": days_left,
            "due_date": due_date,
            "is_paid": paid,
            "skills_list": split_tokens(opp["required_skills"]),
        })

    status_priority = {
        "Eligible": 0,
        "Potentially Eligible": 1,
        "Insufficient Information": 2,
        "Not Eligible": 3,
    }

    if sort_by == "deadline":
        items.sort(
            key=lambda it: (
                0 if (it["days_left"] is not None and it["days_left"] >= 0) else 1,
                it["days_left"] if it["days_left"] is not None and it["days_left"] >= 0 else 999999,
                -it["opp"]["id"],
            )
        )
    elif sort_by == "recently_added":
        items.sort(key=lambda it: -it["opp"]["id"])
    else:
        if user:
            items.sort(
                key=lambda it: (
                    status_priority.get(it["eval"]["status"], 4),
                    -it["eval"]["match_percent"],
                    it["days_left"] if it["days_left"] is not None and it["days_left"] >= 0 else 999999,
                )
            )
        else:
            items.sort(
                key=lambda it: (
                    0 if (it["days_left"] is not None and it["days_left"] >= 0) else 1,
                    it["days_left"] if it["days_left"] is not None and it["days_left"] >= 0 else 999999,
                    -it["opp"]["id"],
                )
            )

    return render_template(
        "explore.html",
        items=items,
        categories=categories,
        modes=modes,
        eligibility_choices=ELIGIBILITY_STATUS_CHOICES,
        filters={
            "q": q,
            "category": category,
            "mode": mode,
            "year": str(selected_year) if selected_year else "",
            "compensation": compensation,
            "eligibility": eligibility_filter if user else "",
            "sort": sort_by,
        },
        saved_ids=saved_ids,
    )


@app.route("/opportunity/<int:opp_id>")
def opportunity_detail(opp_id):
    db = get_db()
    opp = db.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,)).fetchone()
    if opp is None:
        flash("Opportunity not found.", "error")
        return redirect(url_for("explore"))

    user = get_current_user()
    is_saved = False
    current_status = None
    current_app_date = ""
    current_notes = ""
    eligibility_eval = None

    if user:
        is_saved = db.execute(
            "SELECT 1 FROM saved_opportunities WHERE user_id = ? AND opportunity_id = ?",
            (user["id"], opp_id),
        ).fetchone() is not None

        app_row = db.execute(
            "SELECT status, application_date, notes FROM applications WHERE user_id = ? AND opportunity_id = ?",
            (user["id"], opp_id),
        ).fetchone()
        if app_row:
            current_status = app_row["status"]
            current_app_date = app_row["application_date"] or ""
            current_notes = app_row["notes"] or ""

        skill_rows, interest_rows = get_user_skills_and_interests(db, user["id"])
        eligibility_eval = evaluate_smart_eligibility(
            user,
            [r["name"] for r in skill_rows],
            [r["name"] for r in interest_rows],
            opp,
        )

    return render_template(
        "opportunity_detail.html",
        opp=opp,
        is_saved=is_saved,
        current_status=current_status,
        current_app_date=current_app_date,
        current_notes=current_notes,
        eligibility_eval=eligibility_eval,
        all_statuses=ALL_TRACKABLE_STATUSES,
    )


@app.route("/opportunity/<int:opp_id>/save", methods=["POST"])
@login_required
def toggle_save_opportunity(opp_id):
    db = get_db()
    if db.execute("SELECT id FROM opportunities WHERE id = ?", (opp_id,)).fetchone() is None:
        flash("Opportunity not found.", "error")
        return redirect(url_for("explore"))

    user_id = session["user_id"]
    existing = db.execute(
        "SELECT 1 FROM saved_opportunities WHERE user_id = ? AND opportunity_id = ?",
        (user_id, opp_id),
    ).fetchone()

    if existing:
        db.execute("DELETE FROM saved_opportunities WHERE user_id = ? AND opportunity_id = ?", (user_id, opp_id))
        db.commit()
        flash("Removed from saved opportunities.", "info")
    else:
        db.execute("INSERT INTO saved_opportunities (user_id, opportunity_id) VALUES (?, ?)", (user_id, opp_id))
        db.commit()
        sync_saved_deadline_notifications(db, user_id)
        flash("Saved to your opportunity list!", "info")

    next_url = request.form.get("next") or url_for("opportunity_detail", opp_id=opp_id)
    if not is_safe_local_url(next_url):
        next_url = url_for("explore")
    return redirect(next_url)


@app.route("/opportunity/<int:opp_id>/track", methods=["POST"])
@login_required
def track_opportunity(opp_id):
    db = get_db()
    if db.execute("SELECT id FROM opportunities WHERE id = ?", (opp_id,)).fetchone() is None:
        flash("Opportunity not found.", "error")
        return redirect(url_for("explore"))

    status = request.form.get("status", "Interested").strip()
    if status not in ALL_TRACKABLE_STATUSES:
        flash("Invalid application status selected.", "error")
        return redirect(url_for("opportunity_detail", opp_id=opp_id))

    app_date = request.form.get("application_date", "").strip() or None
    notes = request.form.get("notes", "").strip() or None
    if status == "Applied" and not app_date:
        app_date = date.today().isoformat()

    user_id = session["user_id"]
    existing = db.execute(
        "SELECT id, application_date, notes FROM applications WHERE user_id = ? AND opportunity_id = ?",
        (user_id, opp_id),
    ).fetchone()

    if existing:
        final_date = app_date if app_date is not None else existing["application_date"]
        final_notes = notes if notes is not None else existing["notes"]
        db.execute(
            """
            UPDATE applications
            SET status = ?, application_date = ?, notes = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, final_date, final_notes, existing["id"]),
        )
    else:
        db.execute(
            """
            INSERT INTO applications (user_id, opportunity_id, status, application_date, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, opp_id, status, app_date, notes),
        )
    db.commit()
    flash(f"Application status updated to '{status}'.", "info")
    return redirect(url_for("opportunity_detail", opp_id=opp_id))


@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    edit_id_raw = request.args.get("edit", "").strip()
    editing_opp = None
    if edit_id_raw:
        try:
            editing_opp = db.execute(
                "SELECT * FROM opportunities WHERE id = ?", (int(edit_id_raw),)
            ).fetchone()
        except ValueError:
            editing_opp = None

    opportunities = db.execute("SELECT * FROM opportunities ORDER BY id DESC").fetchall()
    students = db.execute(
        """
        SELECT id, full_name, email, college, degree, academic_year, graduation_year, field_of_study, is_admin, created_at
        FROM users
        ORDER BY id DESC
        """
    ).fetchall()

    total_opps = len(opportunities)
    verified_opps = sum(1 for o in opportunities if o["is_verified"])
    total_students = sum(1 for u in students if not u["is_admin"])
    total_tracked_apps = db.execute("SELECT COUNT(*) AS cnt FROM applications").fetchone()["cnt"]

    return render_template(
        "admin.html",
        opportunities=opportunities,
        editing_opp=editing_opp,
        students=students,
        metrics={
            "total_opps": total_opps,
            "verified_opps": verified_opps,
            "total_students": total_students,
            "total_tracked_apps": total_tracked_apps,
        },
    )


@app.route("/admin/opportunities/create", methods=["POST"])
@admin_required
def admin_create_opportunity():
    data, errors = parse_opportunity_form(request.form)
    if errors:
        for err in errors:
            flash(err, "error")
        return redirect(url_for("admin_dashboard"))

    db = get_db()
    db.execute(
        """
        INSERT INTO opportunities (
            title, organization, category, description, deadline, mode, location,
            stipend, target_degree, min_year, max_year, required_skills, eligibility_text,
            apply_url, is_verified
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["title"],
            data["organization"],
            data["category"],
            data["description"],
            data["deadline"],
            data["mode"],
            data["location"],
            data["stipend"],
            data["target_degree"],
            data["min_year"],
            data["max_year"],
            data["required_skills"],
            data["eligibility_text"],
            data["apply_url"],
            data["is_verified"],
        ),
    )
    db.commit()
    flash(f"Created opportunity: {data['title']}.", "info")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/opportunities/<int:opp_id>/update", methods=["POST"])
@admin_required
def admin_update_opportunity(opp_id):
    db = get_db()
    existing = db.execute("SELECT id FROM opportunities WHERE id = ?", (opp_id,)).fetchone()
    if existing is None:
        flash("Opportunity not found.", "error")
        return redirect(url_for("admin_dashboard"))

    data, errors = parse_opportunity_form(request.form)
    if errors:
        for err in errors:
            flash(err, "error")
        return redirect(url_for("admin_dashboard", edit=opp_id))

    db.execute(
        """
        UPDATE opportunities
        SET title = ?, organization = ?, category = ?, description = ?, deadline = ?,
            mode = ?, location = ?, stipend = ?, target_degree = ?, min_year = ?,
            max_year = ?, required_skills = ?, eligibility_text = ?, apply_url = ?,
            is_verified = ?
        WHERE id = ?
        """,
        (
            data["title"],
            data["organization"],
            data["category"],
            data["description"],
            data["deadline"],
            data["mode"],
            data["location"],
            data["stipend"],
            data["target_degree"],
            data["min_year"],
            data["max_year"],
            data["required_skills"],
            data["eligibility_text"],
            data["apply_url"],
            data["is_verified"],
            opp_id,
        ),
    )
    db.commit()
    flash(f"Updated opportunity: {data['title']}.", "info")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/opportunities/<int:opp_id>/verify", methods=["POST"])
@admin_required
def admin_toggle_verified(opp_id):
    db = get_db()
    opp = db.execute("SELECT id, title, is_verified FROM opportunities WHERE id = ?", (opp_id,)).fetchone()
    if opp is None:
        flash("Opportunity not found.", "error")
        return redirect(url_for("admin_dashboard"))

    new_val = 0 if opp["is_verified"] else 1
    db.execute("UPDATE opportunities SET is_verified = ? WHERE id = ?", (new_val, opp_id))
    db.commit()
    state_label = "verified" if new_val else "unverified"
    flash(f"Marked '{opp['title']}' as {state_label}.", "info")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/opportunities/<int:opp_id>/delete", methods=["POST"])
@admin_required
def admin_delete_opportunity(opp_id):
    db = get_db()
    opp = db.execute("SELECT id, title FROM opportunities WHERE id = ?", (opp_id,)).fetchone()
    if opp is None:
        flash("Opportunity not found.", "error")
        return redirect(url_for("admin_dashboard"))

    db.execute("DELETE FROM opportunities WHERE id = ?", (opp_id,))
    db.commit()
    flash(f"Deleted opportunity: {opp['title']}.", "info")
    return redirect(url_for("admin_dashboard"))


@app.errorhandler(404)
def page_not_found(err):
    return render_template("error.html", code=404, title="Page Not Found", message="The page you requested does not exist."), 404


@app.errorhandler(500)
def internal_server_error(err):
    return render_template("error.html", code=500, title="Something Went Wrong", message="An unexpected server error occurred. Please try again."), 500


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
