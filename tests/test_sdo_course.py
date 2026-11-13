from __future__ import annotations

import secrets
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from bs4 import BeautifulSoup
from pydantic import ValidationError
from pymirea import Config, _settings, configure

from mirea_connect.sdo_course import CourseError, CourseSDO, QuizQuestion, form_values

BASE = "https://online-edu.mirea.ru"


@pytest.fixture(autouse=True)
def configured():
    previous = _settings._cfg
    configure(Config(session_keys=secrets.token_urlsafe(32)))
    yield
    _settings._cfg = previous


def outline(module="page", name="Материал", hidden=True):
    return f'''<h1>Курс</h1><li class="section" data-id="100" data-number="1" data-sectionname="Тема">
    <li class="activity modtype_{module}" id="module-20"><div data-activityname="{name}" class="{"hiddenactivity" if hidden else ""}">
    <div class="activityname"><a href="{BASE}/mod/{module}/view.php?id=20">{name}</a></div></div></li></li>'''


def form(module="page", cmid="20", marker="", visible="0", body="<p>Текст</p>"):
    return f'''<form method="post" action="modedit.php"><input name="sesskey" value="csrf-secret" type="hidden">
    <input name="course" value="1"><input name="section" value="1"><input name="coursemodule" value="{cmid}">
    <input name="modulename" value="{module}"><input name="name" value="Материал">
    <input name="cmidnumber" value="{marker}"><input name="visible" value="{visible}">
    <textarea name="page[text]">{body}</textarea><textarea name="introeditor[text]"></textarea>
    <input name="introeditor[format]" value="1"><input type="submit" name="cancel" value="Cancel"></form>'''


def test_form_serialization_keeps_csrf_and_successful_controls_only():
    soup = BeautifulSoup(
        """<form><input type="hidden" name="sesskey" value="secret">
      <input type="hidden" name="x" value="0"><input type="checkbox" name="x" value="1" checked>
      <input type="checkbox" name="off" value="1"><input name="disabled" disabled>
      <input name="cancel" type="submit"><select name="s"><option value="a">A</option><option value="b" selected>B</option></select>
      <textarea name="text">A &amp; B</textarea><select name="tags[]" multiple><option value="a" selected>A</option><option value="b" selected>B</option></select></form>""",
        "html.parser",
    )
    assert form_values(soup.form) == {"sesskey": "secret", "x": "1", "s": "b", "text": "A & B", "tags[]": ["a", "b"]}


@pytest.mark.parametrize("kwargs", [{"correct": 2}, {"correct": -1}, {"answers": ["a", "a"]}, {"answers": ["a", " "]}])
def test_question_rejects_invalid_answers(kwargs):
    with pytest.raises(ValidationError):
        QuizQuestion(**({"name": "Q", "text": "?", "answers": ["a", "b"], "correct": 0} | kwargs))


async def test_create_hidden_page_preserves_form_defaults_and_reuses_exact_retry():
    state = {"created": False, "marker": "", "body": ""}
    with respx.mock() as router:
        router.get(BASE + "/my/").respond(200, text='"sesskey":"private-key"')
        router.get(BASE + "/course/view.php?id=1").mock(
            side_effect=lambda r: httpx.Response(
                200,
                text=outline()
                if state["created"]
                else '<li class="section" data-id="100" data-number="1" data-sectionname="Тема"></li>',
            )
        )
        router.get(BASE + "/course/modedit.php?add=page&course=1&section=1&return=0").respond(200, text=form(cmid=""))
        router.get(BASE + "/course/modedit.php?update=20&return=0").mock(
            side_effect=lambda r: httpx.Response(200, text=form(marker=state["marker"], body=state["body"]))
        )

        def save(request):
            values = parse_qs(request.content.decode(), keep_blank_values=True)
            assert values["visible"] == ["0"]
            assert values["sesskey"] == ["csrf-secret"]
            assert values["coursecontentnotification"] == ["0"]
            assert "cancel" not in values
            state.update(created=True, marker=values["cmidnumber"][0], body=values["page[text]"][0])
            return httpx.Response(303, headers={"Location": "/course/view.php?id=1"})

        post = router.post(BASE + "/course/modedit.php").mock(side_effect=save)
        api = CourseSDO({"K": "v"})
        try:
            result = await api.create_activity(1, 1, "page", "Материал", "<script>\nТекст")
            assert result["hidden"] and result["created"] and result["cmid"] == 20
            assert state["body"] == "<p>&lt;script&gt;<br>\nТекст</p>"
            assert (await api.create_activity(1, 1, "page", "Материал", "<script>\nТекст"))["reused"]
            assert post.call_count == 1
            with pytest.raises(CourseError, match="другим содержимым"):
                await api.create_activity(1, 1, "page", "Материал", "Другой текст")
            assert post.call_count == 1
        finally:
            await api.close()


async def test_reject_foreign_form_and_redirect_without_transmitting_csrf():
    with respx.mock() as router:
        router.get(BASE + "/my/").respond(200, text='"sesskey":"private-key"')
        router.get(BASE + "/course/view.php?id=1").respond(
            200, text='<li class="section" data-id="100" data-number="1"></li>'
        )
        router.get(BASE + "/course/modedit.php?add=page&course=1&section=1&return=0").respond(
            200, text=form(cmid="").replace('action="modedit.php"', 'action="https://evil.example/collect"')
        )
        api = CourseSDO({"K": "v"})
        try:
            with pytest.raises(CourseError, match="адрес вне"):
                await api.create_activity(1, 1, "page", "Материал", "Текст")
            assert all(c.request.method == "GET" for c in router.calls)
            router.get(BASE + "/course/view.php?id=1").respond(302, headers={"Location": "https://evil.example"})
            with pytest.raises(CourseError, match="адрес вне"):
                await api.outline(1)
        finally:
            await api.close()


async def test_off_course_module_cannot_be_changed():
    with respx.mock() as router:
        router.get(BASE + "/my/").respond(200, text='"sesskey":"private-key"')
        router.get(BASE + "/course/view.php?id=1").respond(200, text=outline())
        api = CourseSDO({"K": "v"})
        try:
            with pytest.raises(CourseError, match="не найден"):
                await api.set_visibility(1, 999, False)
            assert all(c.request.method == "GET" for c in router.calls)
        finally:
            await api.close()


async def test_question_write_requires_hidden_quiz():
    with respx.mock() as router:
        router.get(BASE + "/my/").respond(200, text='"sesskey":"private-key"')
        router.get(BASE + "/course/view.php?id=1").respond(200, text=outline(module="quiz", hidden=False))
        router.get(BASE + "/course/modedit.php?update=20&return=0").respond(200, text=form(module="quiz", visible="1"))
        api = CourseSDO({"K": "v"})
        try:
            with pytest.raises(CourseError, match="только в скрытый"):
                await api.add_questions(1, 20, [QuizQuestion(name="Q", text="?", answers=["a", "b"], correct=0)])
            assert all(c.request.method == "GET" for c in router.calls)
        finally:
            await api.close()


async def test_network_error_is_not_retried_after_write():
    with respx.mock() as router:
        post = router.post(BASE + "/course/modedit.php").mock(side_effect=httpx.ReadTimeout("timeout"))
        api = CourseSDO({"K": "v"})
        try:
            with pytest.raises(httpx.ReadTimeout):
                await api.page("/course/modedit.php", data={"x": "y"})
            assert post.call_count == 1
        finally:
            await api.close()


async def test_frozen_visibility_and_plaintext_badge_are_not_errors():
    with respx.mock() as router:
        router.get(BASE + "/my/").respond(200, text='"sesskey":"private-key"')
        router.get(BASE + "/course/view.php?id=1").respond(200, text=outline(module="quiz"))
        frozen = form(module="quiz").replace(
            '<input name="visible" value="0">',
            '<div id="fitem_id_visible"><div data-fieldtype="modvisible">Скрыть на странице курса</div></div>',
        )
        router.get(BASE + "/course/modedit.php?update=20&return=0").respond(
            200, text=frozen + '<span class="alert-danger">Очень вероятно</span>'
        )
        api = CourseSDO({"K": "v"})
        try:
            _, values = await api.edit_form(1, 20)
            assert values["visible"] == "0"
        finally:
            await api.close()


async def test_validation_failure_does_not_report_success():
    with respx.mock() as router:
        router.post(BASE + "/course/modedit.php").respond(
            200, text='<div class="invalid-feedback" id="id_error_name">Required</div>'
        )
        api = CourseSDO({"K": "v"})
        try:
            with pytest.raises(CourseError, match="id_error_name"):
                await api.page("/course/modedit.php", data={"name": ""})
        finally:
            await api.close()


async def test_read_file_cannot_fetch_an_unlisted_url(monkeypatch):
    api = CourseSDO({"K": "v"})

    async def material(*args):
        return {"files": [{"url": BASE + "/pluginfile.php/a.pdf", "name": "a.pdf"}]}

    monkeypatch.setattr(api, "read_activity", material)
    try:
        with pytest.raises(CourseError, match="не найден"):
            await api.read_file(1, 20, BASE + "/pluginfile.php/private.pdf")
    finally:
        await api.close()


async def test_section_create_hides_before_saving_content(monkeypatch):
    api = CourseSDO({"K": "v"})
    events = []
    states = iter(
        [
            {"sections": [{"id": 100, "number": 0, "name": "Общее"}]},
            {"sections": [{"id": 100, "number": 0, "name": "Общее"}, {"id": 101, "number": 1, "name": "Тема 1"}]},
            {"sections": [{"id": 101, "number": 1, "name": "Новая тема", "hidden": True}]},
        ]
    )

    async def get_outline(course_id):
        return next(states)

    async def action(course_id, action, ids):
        events.append(action)

    async def page(path, *, data=None):
        if data is not None:
            events.append("save_content")
            assert events == ["section_add", "section_hide", "save_content"]
            assert data["summary_editor[text]"] == "<p>Описание</p>"
        source = '<form method="post" action="editsection.php"><input name="sesskey" value="csrf"><input name="id" value="101"><textarea name="summary_editor[text]"></textarea></form>'
        return None, BeautifulSoup(source, "html.parser")

    monkeypatch.setattr(api, "outline", get_outline)
    monkeypatch.setattr(api, "ajax_action", action)
    monkeypatch.setattr(api, "page", page)
    try:
        result = await api.create_section(1, "Новая тема", "Описание")
        assert result["hidden"] and result["created"]
    finally:
        await api.close()


async def test_authoring_tools_registered_with_write_annotations(tmp_path):
    from pymirea.store import SessionStore

    from mirea_connect.local import build_local

    mcp, _ = build_local(SessionStore(tmp_path / "session"))
    tools = {t.name: t for t in await mcp.list_tools()}
    assert tools["sdo_course"].annotations.read_only_hint
    for name in [
        "sdo_create_section",
        "sdo_update_section",
        "sdo_create_activity",
        "sdo_update_activity",
        "sdo_set_visibility",
        "sdo_add_quiz_questions",
    ]:
        assert tools[name].annotations.read_only_hint is False
    assert "questions" in tools["sdo_add_quiz_questions"].input_schema["properties"]


async def test_quiz_question_save_attach_and_verify():
    state = {"attached": False, "saved": {}}
    question_path = "/question/bank/editquestion/question.php"

    def question_form(saved=False):
        values = {
            "id": "123" if saved else "",
            "cmid": "20",
            "qtype": "multichoice",
            "_qf__qtype_multichoice_edit_form": "1",
            "sesskey": "csrf-secret",
        }
        if saved:
            values.update(state["saved"])
            values["id"] = "123"
        from html import escape

        return (
            '<form method="post" action="question.php">'
            + "".join(
                f'<input name="{escape(k, quote=True)}" value="{escape(v, quote=True)}">' for k, v in values.items()
            )
            + "</form>"
        )

    def quiz_page(request):
        content = '<input name="cmid" value="20"><input name="category" value="99">'
        if state["attached"]:
            content += '<li class="slot" id="slot-1"><span class="questionname">Q1</span><a href="/question/bank/editquestion/question.php?id=123">Edit</a></li>'
        return httpx.Response(200, text=content)

    with respx.mock() as router:
        router.get(BASE + "/my/").respond(200, text='"sesskey":"private-key"')
        router.get(BASE + "/course/view.php?id=1").respond(200, text=outline(module="quiz"))
        router.get(BASE + "/course/modedit.php?update=20&return=0").respond(200, text=form(module="quiz"))
        router.get(BASE + "/mod/quiz/edit.php?cmid=20").mock(side_effect=quiz_page)
        router.get(BASE + question_path).mock(
            side_effect=lambda r: httpx.Response(200, text=question_form("id=123" in str(r.url)))
        )

        def save(request):
            state["saved"] = {k: v[0] for k, v in parse_qs(request.content.decode(), keep_blank_values=True).items()}
            assert state["saved"]["answer[0][text]"] == "<p>Wrong</p>"
            assert state["saved"]["fraction[0]"] == "0.0"
            assert state["saved"]["fraction[1]"] == "1.0"
            assert state["saved"]["appendqnumstring"] == ""
            return httpx.Response(303, headers={"Location": question_path + "?id=123"})

        def attach(request):
            values = parse_qs(request.content.decode())
            assert values == {"cmid": ["20"], "addquestion": ["123"], "sesskey": ["private-key"]}
            state["attached"] = True
            return httpx.Response(303, headers={"Location": "/mod/quiz/edit.php?cmid=20"})

        saved = router.post(BASE + question_path).mock(side_effect=save)
        attached = router.post(BASE + "/mod/quiz/edit.php").mock(side_effect=attach)
        api = CourseSDO({"K": "v"})
        q = QuizQuestion(name="Q1", text="Question?", answers=["Wrong", "Right"], correct=1)
        try:
            result = await api.add_questions(1, 20, [q])
            assert result["count"] == 1 and result["added"][0]["question_id"] == 123
            with pytest.raises(CourseError, match="уже есть вопросы"):
                await api.add_questions(1, 20, [q])
            assert saved.call_count == attached.call_count == 1
        finally:
            await api.close()


async def test_visibility_permission_denied_is_not_retried_or_bypassed():
    with respx.mock() as router:
        router.get(BASE + "/my/").respond(200, text='"sesskey":"private-key"')
        router.get(BASE + "/course/view.php?id=1").respond(200, text=outline())
        router.get(BASE + "/course/modedit.php?update=20&return=0").respond(200, text=form())
        post = router.post(BASE + "/lib/ajax/service.php").respond(
            200, json=[{"error": True, "exception": {"errorcode": "nopermissions"}}]
        )
        api = CourseSDO({"K": "v"})
        try:
            with pytest.raises(CourseError, match="nopermissions"):
                await api.set_visibility(1, 20, False)
            assert post.call_count == 1
            # Reasserting an already satisfied state is a read-only no-op.
            assert (await api.set_visibility(1, 20, True))["updated"] is False
            assert post.call_count == 1
        finally:
            await api.close()


async def test_assign_submissions_listing_and_filtering():
    grading_html = f'''
    <table class="flexible table generaltable">
      <thead>
        <tr><th>Select</th><th>Name</th><th>ID</th><th>Email</th><th>Group</th><th>Status</th><th>Grade</th><th>Date</th><th>Text</th><th>Files</th><th>Comments</th><th>Feedback</th></tr>
      </thead>
      <tbody>
        <tr>
          <td><input type="checkbox"></td>
          <td class="cell c1 username"><a href="{BASE}/user/view.php?id=101&course=1"><span class="userinitials">ИИ</span>Иванов Иван Иванович</a></td>
          <td class="cell c2 idnumber">24T001</td>
          <td class="cell c3 email">ivanov@edu.mirea.ru</td>
          <td class="cell c4 department">ЭФБО-01-24</td>
          <td class="cell c5 status">Нет ответа</td>
          <td class="cell c6 grade">Оценка пользователя / 5,0 <a href="{BASE}/mod/assign/view.php?id=20&action=grader&userid=101">Оценка</a></td>
          <td class="cell c7">-</td>
          <td class="cell c8">-</td>
          <td class="cell c9">-</td>
          <td class="cell c10">-</td>
          <td class="cell c12">-</td>
        </tr>
        <tr>
          <td><input type="checkbox"></td>
          <td class="cell c1 username"><a href="{BASE}/user/view.php?id=84838&course=1"><span class="userinitials">ПП</span>Петров Петр Петрович</a></td>
          <td class="cell c2 idnumber">24Т1172</td>
          <td class="cell c3 email">petrov.p.p@edu.mirea.ru</td>
          <td class="cell c4 department">ИВБО-02-24</td>
          <td class="cell c5 status"></td>
          <td class="cell c6 grade">Оценка пользователя / 3,0 <a href="{BASE}/mod/assign/view.php?id=20&action=grader&userid=84838">Оценка</a></td>
          <td class="cell c7">пятница, 6 ноября 2026, 01:34</td>
          <td class="cell c8">-</td>
          <td class="cell c9"><a href="{BASE}/pluginfile.php/123/assignsubmission_file/submission_files/1/report.docx">report.docx</a></td>
          <td class="cell c10">-</td>
          <td class="cell c12">-</td>
        </tr>
      </tbody>
    </table>
    '''
    with respx.mock() as router:
        router.get(BASE + "/my/").respond(200, text='"sesskey":"private-key"')
        router.get(BASE + "/course/view.php?id=1").respond(200, text=outline(module="assign", name="Практика"))
        router.get(BASE + "/mod/assign/view.php?id=20&action=grading").respond(200, text=grading_html)
        api = CourseSDO({"K": "v"})
        try:
            # All submissions
            res_all = await api.assign_submissions(1, 20)
            assert res_all["total_matches"] == 2
            assert len(res_all["submissions"]) == 2

            # Filter by query
            res_petrov = await api.assign_submissions(1, 20, query="Петров")
            assert res_petrov["total_matches"] == 1
            sub = res_petrov["submissions"][0]
            assert sub["user_id"] == 84838
            assert sub["name"] == "Петров Петр Петрович"
            assert sub["group"] == "ИВБО-02-24"
            assert sub["status"] == "Сдано"
            assert sub["files"][0]["name"] == "report.docx"
            assert sub["grade"] is None
            assert sub["grade_max"] == "3,0"

            # Filter by group
            res_grp = await api.assign_submissions(1, 20, group="ЭФБО-01-24")
            assert res_grp["total_matches"] == 1
            assert res_grp["submissions"][0]["name"] == "Иванов Иван Иванович"
        finally:
            await api.close()


async def test_grade_submission_submits_moodle_form():
    form_html = f'''
    <form method="post" action="{BASE}/mod/assign/view.php" id="mform1_grade">
      <input type="hidden" name="sesskey" value="private-key">
      <input type="hidden" name="action" value="submitgrade">
      <input type="hidden" name="userid" value="84838">
      <input type="hidden" name="id" value="20">
      <input type="text" name="grade" value="">
      <textarea name="assignfeedbackcomments_editor[text]"></textarea>
      <input type="submit" name="savegrade" value="Сохранить">
    </form>
    '''
    submitted = {}

    def handle_submit(request):
        values = parse_qs(request.content.decode(), keep_blank_values=True)
        assert values["action"] == ["submitgrade"]
        assert values["sesskey"] == ["private-key"]
        assert values["userid"] == ["84838"]
        assert values["grade"] == ["5"]
        assert values["assignfeedbackcomments_editor[text]"] == ["Зачтено, отличная работа!"]
        submitted["done"] = True
        return httpx.Response(200, text="OK")

    with respx.mock() as router:
        router.get(BASE + "/my/").respond(200, text='"sesskey":"private-key"')
        router.get(BASE + "/course/view.php?id=1").respond(200, text=outline(module="assign", name="Практика"))
        router.get(BASE + "/mod/assign/view.php?id=20&action=grade&userid=84838").respond(200, text=form_html)
        router.post(BASE + "/mod/assign/view.php").mock(side_effect=handle_submit)
        api = CourseSDO({"K": "v"})
        try:
            res = await api.grade_submission(1, 20, 84838, "5", "Зачтено, отличная работа!")
            assert res["status"] == "graded"
            assert res["grade"] == "5"
            assert submitted.get("done") is True
        finally:
            await api.close()


async def test_read_submission_docx_file(tmp_path):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:t>Отчёт студента по практике</w:t></w:p></w:body></w:document>',
        )
    raw_docx = buf.getvalue()

    with respx.mock() as router:
        router.get(BASE + "/my/").respond(200, text='"sesskey":"private-key"')
        file_url = f"{BASE}/pluginfile.php/123/assignsubmission_file/submission_files/1/report.docx"
        router.get(file_url).respond(200, content=raw_docx)
        api = CourseSDO({"K": "v"})
        try:
            # Read text
            res_text = await api.read_submission_file(1, 20, file_url)
            assert res_text["name"] == "report.docx"
            assert "Отчёт студента по практике" in res_text["text"]

            # Download file
            save_dest = tmp_path / "downloaded.docx"
            res_dl = await api.download_submission_file(1, 20, file_url, save_path=str(save_dest))
            assert save_dest.exists()
            assert save_dest.read_bytes() == raw_docx
            assert res_dl["saved_to"] == str(save_dest)
        finally:
            await api.close()
