from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from app.database import create_session_factory
from app.models import Assignment, AssignmentOrigin, AssignmentStatus, EmailActionItem, EmailActionStatus, EmailActionType
from app.services import AssignmentInput, upsert_assignment


DASHBOARD = Path(__file__).resolve().parents[1] / "app" / "dashboard.py"


def prepare_dashboard(monkeypatch, tmp_path):
    factory = create_session_factory(tmp_path / "dashboard.sqlite3")
    monkeypatch.setattr("app.database.SessionLocal", factory)
    monkeypatch.setattr("app.database.init_db", lambda: None)
    return factory, AppTest.from_file(str(DASHBOARD), default_timeout=20)


def test_empty_dashboard_starts(monkeypatch, tmp_path):
    _, dashboard = prepare_dashboard(monkeypatch, tmp_path)
    dashboard.run()
    assert not dashboard.exception
    assert dashboard.title[0].value == "🐾 HuskySync"
    assert any("No pending email" in item.value for item in dashboard.info)


def test_semester_menu_hides_past_semesters_by_default(monkeypatch, tmp_path):
    factory, dashboard = prepare_dashboard(monkeypatch, tmp_path)
    current = datetime.now(UTC)
    with factory() as session:
        # A term code in the course name wins over the due date: 1263 is Spring 2026.
        upsert_assignment(session, AssignmentInput(
            course_code="OPIM 5110", course_name="OPIM-5110-Operations-SEC001-1263", title="Old paper",
            due_date=current + timedelta(hours=30), origin=AssignmentOrigin.BLACKBOARD, source_key="old",
        ))
        upsert_assignment(session, AssignmentInput(
            course_code="CSE 3100", course_name="Systems", title="Lab 2",
            due_date=current + timedelta(hours=20), origin=AssignmentOrigin.BLACKBOARD, source_key="lab-2",
        ))
        session.commit()
    dashboard.run()
    assert not dashboard.exception
    semester = next(item for item in dashboard.selectbox if item.label == "Semester")
    assert "Spring 2026" in semester.options and semester.options[-1] == "All semesters"
    assert semester.value != "Spring 2026"
    assert list(dashboard.dataframe[0].value["Assignment"]) == ["Lab 2"]
    semester.select("All semesters").run()
    assert sorted(dashboard.dataframe[0].value["Assignment"]) == ["Lab 2", "Old paper"]


def test_filter_calendar_action_and_email_conversion(monkeypatch, tmp_path):
    factory, dashboard = prepare_dashboard(monkeypatch, tmp_path)
    current = datetime.now(UTC)
    with factory() as session:
        assignment = upsert_assignment(session, AssignmentInput(
            course_code="CSE 3100", course_name="Systems", title="Lab 2",
            due_date=current + timedelta(hours=20), origin=AssignmentOrigin.BLACKBOARD,
            direct_url="https://huskyct.uconn.edu/lab-2", source_key="lab-2",
        ))
        assignment_id = assignment.id
        session.add(EmailActionItem(
            sender="professor@uconn.edu", subject="Project deadline", received_at=current,
            extracted_type=EmailActionType.DEADLINE, summary="Project due tomorrow",
            suggested_deadline=current + timedelta(days=3), status=EmailActionStatus.PENDING,
            source_key="email-1",
        ))
        session.commit()
    calls = []
    def sync(item_id):
        calls.append(item_id)
        return SimpleNamespace(created=True, event_id="event-1")
    monkeypatch.setattr("app.calendar_sync.sync_assignment", sync)
    dashboard.run()
    assert not dashboard.exception
    assert any("within 48 hours" in item.value for item in dashboard.warning)
    dashboard.button(key=f"urgent_calendar_{assignment_id}").click().run()
    assert calls == [assignment_id]
    assert not dashboard.exception
    with factory() as session:
        email_id = session.query(EmailActionItem).one().id
    dashboard.button(key=f"email_convert_{email_id}").click().run()
    assert not dashboard.exception
    with factory() as session:
        item = session.get(EmailActionItem, email_id)
        assert item.status == EmailActionStatus.CONVERTED
        assert session.query(Assignment).count() == 2
        assert session.get(Assignment, item.converted_assignment_id).origin == AssignmentOrigin.EMAIL
    course_box = next(item for item in dashboard.selectbox if item.label == "Course")
    course_box.select("EMAIL").run()
    assert not dashboard.exception
    assert list(dashboard.dataframe[0].value["Course"]) == ["EMAIL"]
    with factory() as session:
        task_id = session.get(EmailActionItem, email_id).converted_assignment_id
    dashboard.button(key=f"task_complete_{task_id}").click().run()
    assert not dashboard.exception
    with factory() as session:
        assert session.get(Assignment, task_id).status == AssignmentStatus.SUBMITTED
    assert list(dashboard.dataframe[0].value["Status"]) == ["Submitted"]


def test_public_demo_uses_sample_data_and_disables_account_actions(monkeypatch, tmp_path):
    # demo_app.py sets these itself; registering them here restores the environment afterwards.
    monkeypatch.setenv("HUSKYSYNC_DEMO", "1")
    monkeypatch.setenv("HUSKYSYNC_DATA_DIR", str(tmp_path))
    factory = create_session_factory(tmp_path / "demo.sqlite3")
    monkeypatch.setattr("app.database.SessionLocal", factory)
    monkeypatch.setattr("app.database.init_db", lambda: None)
    demo = AppTest.from_file(str(DASHBOARD.parents[1] / "demo_app.py"), default_timeout=20).run()
    assert not demo.exception
    assert any("Demo mode" in item.value for item in demo.info)
    for label in ("Sync HuskyCT", "Scan Emails", "Sync all urgent"):
        assert next(button for button in demo.button if button.label == label).disabled
    assert next(button for button in demo.button if button.key == "task_sync_selected").disabled
    with factory() as session:
        assert session.query(Assignment).count() == 12
        assert session.query(Assignment).filter(Assignment.status == AssignmentStatus.SUBMITTED).count() > 0

    demo.button(key="load_sample_emails").click().run()
    assert not demo.exception
    with factory() as session:
        assert session.query(EmailActionItem).count() == 5
    demo.button(key="reset_demo").click().run()
    assert not demo.exception
    with factory() as session:
        assert session.query(EmailActionItem).count() == 0
        assert session.query(Assignment).count() == 12
