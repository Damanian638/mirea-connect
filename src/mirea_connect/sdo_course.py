"""Course authoring through authenticated Moodle forms (no browser automation).

Only explicit operations are exposed. Never accept arbitrary form fields or URLs
for writes, and never replay a POST after a network error.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pymirea.sdo import SDO_URL
from pypdf import PdfReader

from .sdo import MireaSDO

ActivityType = Literal["label", "page", "url", "assign", "quiz", "folder"]
MODULES = {"label", "page", "url", "assign", "quiz", "folder"}


class CourseError(RuntimeError):
    """Safe, actionable error without credentials or response bodies."""


class QuizQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=150)
    text: str = Field(min_length=1, max_length=30000)
    answers: list[str] = Field(min_length=2, max_length=5)
    correct: int = Field(ge=0, description="Zero-based index of the single correct answer")
    feedback: str = Field(default="", max_length=10000)

    @model_validator(mode="after")
    def valid_answers(self):
        if self.correct >= len(self.answers) or any(not a.strip() for a in self.answers):
            raise ValueError("Нужны непустые ответы и корректный индекс correct (с нуля).")
        if len({a.strip().casefold() for a in self.answers}) != len(self.answers):
            raise ValueError("Варианты ответов не должны повторяться.")
        return self


def positive(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CourseError(f"{label}: нужен положительный целый ID.")
    return value


def trusted_url(url: str, base: str = SDO_URL) -> str:
    result = urljoin(base, url)
    p = urlsplit(result)
    if (p.scheme, p.netloc) != ("https", "online-edu.mirea.ru"):
        raise CourseError("СДО вернула адрес вне online-edu.mirea.ru; операция остановлена.")
    return result


def form_values(form: Tag) -> dict[str, str | list[str]]:
    """Serialize successful controls, preserving defaults and hidden CSRF fields."""
    values: dict[str, str | list[str]] = {}
    for field in form.select("input[name],textarea[name],select[name]"):
        if field.has_attr("disabled") or field.find_parent("fieldset", disabled=True):
            continue
        name = field["name"]
        kind = field.get("type", "text").lower()
        if kind in {"submit", "button", "reset", "file", "image"}:
            continue
        if kind in {"checkbox", "radio"} and not field.has_attr("checked"):
            continue
        if field.name == "textarea":
            value = field.get_text()
        elif field.name == "select":
            options = field.select("option[selected]")
            if not options and not field.has_attr("multiple"):
                options = field.select("option")[:1]
            selected = [str(o.get("value", o.get_text())) for o in options if not o.has_attr("disabled")]
            if field.has_attr("multiple"):
                if selected:
                    values[name] = selected
                continue
            if not selected:
                continue
            value = selected[0]
        else:
            value = field.get("value", "on" if kind == "checkbox" else "")
        values[name] = str(value)
    return values


def plain_html(text: str) -> str:
    return "<p>" + html.escape(text).replace("\n", "<br>\n") + "</p>"


def extract_docx_text(raw: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            if "word/document.xml" not in z.namelist():
                return ""
            tree = ET.fromstring(z.read("word/document.xml"))
            ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            paras = []
            for p in tree.iter(f"{{{ns}}}p"):
                texts = [node.text for node in p.iter(f"{{{ns}}}t") if node.text]
                if texts:
                    paras.append("".join(texts))
            return "\n".join(paras)
    except Exception:
        return ""


def content_marker(course_id: int, section: int, module: str, name: str, content: str, external_url: str | None) -> str:
    payload = json.dumps([course_id, section, module, name.strip(), content, external_url], ensure_ascii=False)
    return "mcp-" + hashlib.sha256(payload.encode()).hexdigest()[:32]


class CourseSDO(MireaSDO):
    async def ready(self):
        # httpx INFO logs include Moodle's sesskey in AJAX query strings.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        if not await self._get_sesskey():
            raise CourseError("СДО не подтвердила вход. Войдите через login. Сессия Пульса сохранена.")

    async def page(self, path: str, *, data: dict | None = None) -> tuple[httpx.Response, BeautifulSoup]:
        url = trusted_url(path)
        # No automatic POST replay; redirects are followed as GET only on this origin.
        if data is None:
            response = await self.client.get(url, follow_redirects=False)
        else:
            response = await self.client.post(url, data=data, follow_redirects=False)
        for _ in range(8):
            if response.status_code not in {301, 302, 303}:
                break
            target = trusted_url(response.headers.get("location", ""), str(response.url))
            response = await self.client.get(target, follow_redirects=False)
        if response.status_code >= 400 or response.status_code in {307, 308}:
            raise CourseError(f"СДО отклонила запрос (HTTP {response.status_code}). Проверьте права и состояние курса.")
        if urlsplit(str(response.url)).path.startswith(("/login/", "/auth/")):
            raise CourseError("Сессия СДО истекла. Войдите через login. Сессия Пульса сохранена.")
        soup = BeautifulSoup(response.text, "html.parser")
        if soup.select_one(".errorbox, .errormessage, #region-main .alert-danger[role=alert]"):
            raise CourseError("СДО вернула ошибку доступа или проверки формы. Изменение не подтверждено.")
        if data is not None:
            errors = [
                n.get("id", "field") for n in soup.select(".invalid-feedback, .ferror .error") if n.get_text(strip=True)
            ]
            if errors:
                raise CourseError("СДО не приняла поля формы: " + ", ".join(errors[:12]))
        return response, soup

    def form(self, soup: BeautifulSoup, marker: str, action: str) -> Tag:
        field = soup.find(attrs={"name": marker})
        form = field.find_parent("form") if field else None
        if not form or form.get("method", "get").lower() != "post":
            raise CourseError("Нужная форма недоступна: недостаточно прав или изменилась версия СДО.")
        if urlsplit(trusted_url(form.get("action", ""), SDO_URL + action)).path != action:
            raise CourseError("Неожиданный обработчик формы; операция остановлена.")
        if not form.find("input", attrs={"name": "sesskey"}):
            raise CourseError("В форме отсутствует защита сессии; операция остановлена.")
        return form

    async def outline(self, course_id: int) -> dict:
        positive(course_id, "course_id")
        await self.ready()
        _, soup = await self.page(f"/course/view.php?id={course_id}")
        sections = []
        for section in soup.select("li.section[data-id]"):
            number = section.get("data-number", section.get("data-sectionid", ""))
            if not str(number).isdigit():
                continue
            heading = section.select_one(".sectionname")
            row = {
                "id": int(section["data-id"]),
                "number": int(number),
                "name": section.get("data-sectionname") or (heading.get_text(" ", strip=True) if heading else ""),
                "hidden": "hidden" in section.get("class", []),
                "activities": [],
            }
            for item in section.select("li.activity[id^=module-]"):
                card = item.select_one("[data-activityname]")
                name = card.get("data-activityname", "") if card else ""
                link = item.select_one(".activityname a[href], .activity-instance a[href]")
                module = next((c[8:] for c in item.get("class", []) if c.startswith("modtype_")), "")
                row["activities"].append(
                    {
                        "cmid": int(item["id"].split("-")[-1]),
                        "type": module,
                        "name": name or (link.get_text(" ", strip=True) if link else ""),
                        "hidden": bool(item.select_one(".hiddenactivity")),
                        "url": trusted_url(link["href"]) if link else None,
                    }
                )
            sections.append(row)
        if not sections:
            raise CourseError("Структура курса недоступна или формат курса не поддерживается.")
        title = soup.select_one("h1") or soup.title
        return {"course_id": course_id, "name": title.get_text(" ", strip=True) if title else "", "sections": sections}

    async def activity_in_course(self, course_id: int, cmid: int) -> dict:
        positive(cmid, "cmid")
        outline = await self.outline(course_id)
        for section in outline["sections"]:
            for activity in section["activities"]:
                if activity["cmid"] == cmid:
                    return {**activity, "section": section["number"]}
        raise CourseError("Элемент с этим cmid не найден в указанном курсе.")

    async def read_activity(self, course_id: int, cmid: int) -> dict:
        activity = await self.activity_in_course(course_id, cmid)
        if not activity["url"]:
            _, soup = await self.page(f"/course/view.php?id={course_id}")
            main = soup.select_one(f"#module-{cmid}")
            if main is None:
                raise CourseError("Элемент не найден на странице курса.")
        else:
            _, soup = await self.page(activity["url"])
            main = soup.select_one("#region-main") or soup
        files = []
        for a in main.select('a[href*="/pluginfile.php/"]'):
            url = trusted_url(a["href"])
            if url not in [f["url"] for f in files]:
                files.append({"name": a.get_text(" ", strip=True), "url": url})
        for node in main.select("script,style,form,nav"):
            node.decompose()
        text = main.get_text("\n", strip=True)
        return {**activity, "text": text[:60000], "truncated": len(text) > 60000, "files": files}

    async def read_file(self, course_id: int, cmid: int, file_url: str) -> dict:
        material = await self.read_activity(course_id, cmid)
        file = next((f for f in material["files"] if f["url"] == file_url), None)
        if file is None:
            raise CourseError("Файл не найден среди вложений указанного элемента курса.")
        url = trusted_url(file_url)
        async with self.client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code != 200:
                raise CourseError("СДО не разрешила скачивание вложения.")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > 25 * 1024 * 1024:
                    raise CourseError("Вложение превышает ограничение 25 МБ.")
        raw = bytes(chunks)
        if raw.startswith(b"%PDF-"):
            try:
                reader = PdfReader(io.BytesIO(raw))
                texts = []
                size = 0
                for page in reader.pages[:200]:
                    part = page.extract_text() or ""
                    texts.append(part)
                    size += len(part)
                    if size > 120000:
                        break
                text = "\n\n".join(texts)
                truncated = len(reader.pages) > len(texts) or len(text) > 120000
            except Exception:
                raise CourseError(
                    "Не удалось извлечь текст PDF. Возможно, файл защищён или содержит только сканы."
                ) from None
        elif file["name"].lower().endswith(".docx"):
            text = extract_docx_text(raw)
            if not text:
                raise CourseError("Не удалось извлечь текст из документа DOCX.")
            truncated = len(text) > 120000
            text = text[:120000]
        elif file["name"].lower().endswith((".txt", ".csv", ".md")):
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("cp1251", errors="replace")
            truncated = len(text) > 120000
        else:
            raise CourseError("Извлечение текста поддерживается для PDF, TXT, CSV и MD; OCR не выполняется.")
        return {"name": file["name"], "text": text[:120000], "truncated": truncated}

    async def edit_form(self, course_id: int, cmid: int):
        activity = await self.activity_in_course(course_id, cmid)
        _, soup = await self.page(f"/course/modedit.php?update={cmid}&return=0")
        form = self.form(soup, "modulename", "/course/modedit.php")
        values = form_values(form)
        if str(values.get("course")) != str(course_id) or str(values.get("coursemodule")) != str(cmid):
            raise CourseError("Форма относится к другому курсу или элементу.")
        if "visible" not in values:
            # Moodle may render modvisible as frozen text instead of a control.
            frozen = form.select_one("#fitem_id_visible [data-fieldtype=modvisible]")
            text = frozen.get_text(" ", strip=True) if frozen else ""
            if text in {"Скрыть на странице курса", "Hide on course page"}:
                values["visible"] = "0"
            elif text in {"Отображать на странице курса", "Show on course page"}:
                values["visible"] = "1"
        return activity, values

    @staticmethod
    def content_fields(module: str, values: dict, name: str, content: str, external_url: str | None):
        if module not in MODULES or not name.strip() or len(name) > 255 or len(content) > 200000:
            raise CourseError("Неподдерживаемый тип, пустое/длинное название или слишком большой текст.")
        values["name"] = name.strip()
        if module == "page":
            if not content.strip():
                raise CourseError("Для страницы нужен непустой текст.")
            values["page[text]"] = plain_html(content)
            values["page[format]"] = "1"
        else:
            values["introeditor[text]"] = plain_html(content)
            values["introeditor[format]"] = "1"
        if module == "url":
            p = urlsplit(external_url or "")
            if p.scheme not in {"https", "http"} or not p.netloc or p.username or p.password:
                raise CourseError("Для ссылки нужен http(s) URL без логина и пароля.")
            values["externalurl"] = external_url
        elif external_url is not None:
            raise CourseError("external_url используется только для элемента url.")

    async def create_activity(
        self, course_id: int, section: int, module: str, name: str, content: str = "", external_url: str | None = None
    ) -> dict:
        # Validate all user content before any write.
        self.content_fields(module, {}, name, content, external_url)
        outline = await self.outline(course_id)
        target = next((s for s in outline["sections"] if s["number"] == section), None)
        if target is None:
            raise CourseError("Раздел с таким номером не найден; сначала прочитайте структуру курса.")
        marker = content_marker(course_id, section, module, name, content, external_url)
        matches = [a for a in target["activities"] if a["name"] == name.strip()]
        for match in matches:
            _, existing = await self.edit_form(course_id, match["cmid"])
            if existing.get("cmidnumber") == marker:
                return {**match, "created": False, "reused": True, "course_id": course_id, "section": section}
        if matches:
            raise CourseError(
                "В разделе уже есть элемент с этим названием и другим содержимым. Используйте обновление по cmid."
            )
        _, soup = await self.page(
            "/course/modedit.php?" + urlencode({"add": module, "course": course_id, "section": section, "return": 0})
        )
        form = self.form(soup, "modulename", "/course/modedit.php")
        values = form_values(form)
        if (
            str(values.get("course")) != str(course_id)
            or values.get("modulename") != module
            or str(values.get("section")) != str(section)
        ):
            raise CourseError("СДО вернула форму другого курса, раздела или типа.")
        self.content_fields(module, values, name, content, external_url)
        values.update(visible="0", cmidnumber=marker, coursecontentnotification="0", completion="0", submitbutton="1")
        if module == "assign":
            for key in list(values):
                if key.endswith("[enabled]") and any(
                    key.startswith(x) for x in ("duedate", "allowsubmissionsfromdate", "cutoffdate", "gradingduedate")
                ):
                    values.pop(key)
            values.update(
                assignsubmission_onlinetext_enabled="1",
                assignsubmission_file_enabled="1",
                sendnotifications="0",
                sendlatenotifications="0",
                sendstudentnotifications="0",
            )
        await self.page("/course/modedit.php", data=values)
        new_outline = await self.outline(course_id)
        found = [
            a
            for s in new_outline["sections"]
            if s["number"] == section
            for a in s["activities"]
            if a["name"] == name.strip()
        ]
        for activity in found:
            _, saved = await self.edit_form(course_id, activity["cmid"])
            if saved.get("cmidnumber") == marker and str(saved.get("visible")) == "0":
                return {**activity, "hidden": True, "created": True, "course_id": course_id, "section": section}
        raise CourseError(
            "Создание не подтверждено. Перед повтором проверьте структуру курса; запрос автоматически не повторялся."
        )

    async def update_activity(
        self, course_id: int, cmid: int, name: str, content: str, external_url: str | None = None
    ) -> dict:
        activity, values = await self.edit_form(course_id, cmid)
        self.content_fields(activity["type"], values, name, content, external_url)
        if str(values.get("cmidnumber", "")).startswith("mcp-"):
            values["cmidnumber"] = content_marker(
                course_id, activity["section"], activity["type"], name, content, external_url
            )
        # Content editing preserves visibility and all unrelated module settings.
        values.update(submitbutton="1", coursecontentnotification="0")
        await self.page("/course/modedit.php", data=values)
        updated, saved = await self.edit_form(course_id, cmid)
        key = "page[text]" if activity["type"] == "page" else "introeditor[text]"
        if saved.get("name") != name.strip() or saved.get(key) != values[key]:
            raise CourseError(f"Сохранение содержимого cmid={cmid} не подтверждено.")
        return {**updated, "updated": True}

    async def set_visibility(self, course_id: int, cmid: int, hidden: bool) -> dict:
        activity, current = await self.edit_form(course_id, cmid)
        if str(current.get("visible")) == ("0" if hidden else "1"):
            return {**activity, "hidden": hidden, "updated": False}
        await self.ajax_action(course_id, "cm_hide" if hidden else "cm_show", [cmid])
        activity, saved = await self.edit_form(course_id, cmid)
        if str(saved.get("visible")) != ("0" if hidden else "1"):
            raise CourseError("Изменение видимости не подтверждено.")
        return {**activity, "hidden": hidden, "updated": True}

    async def ajax_action(self, course_id: int, action: str, ids: list[int]) -> None:
        response = await self.client.post(
            SDO_URL + "/lib/ajax/service.php",
            params={"sesskey": self._sesskey, "info": "core_courseformat_update_course"},
            json=[
                {
                    "index": 0,
                    "methodname": "core_courseformat_update_course",
                    "args": {"courseid": course_id, "action": action, "ids": ids},
                }
            ],
            follow_redirects=False,
        )
        try:
            entry = response.json()[0]
        except (ValueError, KeyError, IndexError, TypeError):
            raise CourseError("Операция с разделом не подтверждена; проверьте структуру курса.") from None
        if response.status_code != 200 or entry.get("error"):
            code = str((entry.get("exception") or {}).get("errorcode", "unknown"))
            raise CourseError(f"СДО отклонила операцию с курсом ({code}): недостаточно прав или метод API недоступен.")

    async def create_section(self, course_id: int, name: str, summary: str = "") -> dict:
        if not name.strip() or len(name) > 255 or len(summary) > 200000:
            raise CourseError("Укажите непустое название до 255 символов и описание до 200000 символов.")
        before = await self.outline(course_id)
        existing = [s for s in before["sections"] if s["name"] == name.strip()]
        if existing:
            raise CourseError("Раздел с этим названием уже есть. Повторное создание остановлено.")
        await self.ajax_action(course_id, "section_add", [])
        after = await self.outline(course_id)
        created = [s for s in after["sections"] if s["id"] not in {x["id"] for x in before["sections"]}]
        if len(created) != 1:
            raise CourseError("Новый раздел однозначно не определён. Проверьте структуру перед повтором.")
        section = created[0]
        # Empty section is hidden before any user content is submitted.
        try:
            await self.ajax_action(course_id, "section_hide", [section["id"]])
            _, soup = await self.page(f"/course/editsection.php?id={section['id']}")
            form = self.form(soup, "summary_editor[text]", "/course/editsection.php")
            values = form_values(form)
            if str(values.get("id")) != str(section["id"]):
                raise CourseError("Неверный ID раздела в форме.")
            values.pop("name[usedefault]", None)
            values.update(
                {
                    "name": name.strip(),
                    "summary_editor[text]": plain_html(summary),
                    "summary_editor[format]": "1",
                    "submitbutton": "1",
                }
            )
            await self.page("/course/editsection.php", data=values)
            saved = next(s for s in (await self.outline(course_id))["sections"] if s["id"] == section["id"])
            if saved["name"] != name.strip() or not saved["hidden"]:
                raise CourseError("Имя или скрытие раздела не подтверждено.")
            return {**saved, "created": True, "course_id": course_id}
        except (CourseError, httpx.HTTPError):
            raise CourseError(
                f"Создан пустой раздел id={section['id']}, но настройка не завершена. Проверьте его перед повтором."
            ) from None

    async def update_section(self, course_id: int, section_id: int, name: str, summary: str, hidden: bool) -> dict:
        positive(section_id, "section_id")
        if not name.strip() or len(name) > 255 or len(summary) > 200000:
            raise CourseError("Неверная длина названия или описания раздела.")
        outline = await self.outline(course_id)
        if not any(s["id"] == section_id for s in outline["sections"]):
            raise CourseError("Раздел не принадлежит указанному курсу.")
        # Hide first when requested; show only after all content has been saved.
        if hidden:
            await self.ajax_action(course_id, "section_hide", [section_id])
        _, soup = await self.page(f"/course/editsection.php?id={section_id}")
        form = self.form(soup, "summary_editor[text]", "/course/editsection.php")
        values = form_values(form)
        if str(values.get("id")) != str(section_id) or str(values.get("course")) != str(course_id):
            raise CourseError("Не совпал курс или раздел в форме.")
        values.pop("name[usedefault]", None)
        values.update(
            {
                "name": name.strip(),
                "summary_editor[text]": plain_html(summary),
                "summary_editor[format]": "1",
                "submitbutton": "1",
            }
        )
        await self.page("/course/editsection.php", data=values)
        await self.ajax_action(course_id, "section_hide" if hidden else "section_show", [section_id])
        final = await self.outline(course_id)
        section = next(s for s in final["sections"] if s["id"] == section_id)
        if section["name"] != name.strip() or section["hidden"] != hidden:
            raise CourseError("Название или видимость раздела не подтверждены.")
        return {**section, "updated": True}

    async def quiz_questions(self, course_id: int, cmid: int) -> dict:
        activity = await self.activity_in_course(course_id, cmid)
        if activity["type"] != "quiz":
            raise CourseError("Указанный элемент не является тестом.")
        _, soup = await self.page(f"/mod/quiz/edit.php?cmid={cmid}")
        questions = []
        for slot in soup.select("li.slot"):
            name = slot.select_one(".questionname")
            questionid = None
            for a in slot.select("a[href]"):
                p = urlsplit(a["href"])
                if p.path.endswith("/question.php"):
                    raw = parse_qs(p.query).get("id", [""])[0]
                    if raw.isdigit():
                        questionid = int(raw)
            questions.append(
                {
                    "slot": slot.get("data-slot", slot.get("id")),
                    "name": name.get_text(" ", strip=True) if name else "",
                    "question_id": questionid,
                }
            )
        if not soup.find("input", attrs={"name": "cmid", "value": str(cmid)}):
            raise CourseError("Редактирование вопросов недоступно.")
        return {"cmid": cmid, "questions": questions, "count": len(questions)}

    async def add_questions(self, course_id: int, cmid: int, questions: list[QuizQuestion]) -> dict:
        if not 1 <= len(questions) <= 50 or len({q.name for q in questions}) != len(questions):
            raise CourseError("Нужно 1–50 вопросов с уникальными названиями.")
        activity, settings = await self.edit_form(course_id, cmid)
        if activity["type"] != "quiz" or str(settings.get("visible")) != "0":
            raise CourseError("Добавление вопросов поддерживается только в скрытый тест. Сначала скройте тест.")
        state = await self.quiz_questions(course_id, cmid)
        existing = {q["name"] for q in state["questions"]}
        if existing.intersection(q.name for q in questions):
            raise CourseError("В тесте уже есть вопросы с такими названиями. Проверьте состав; повтор остановлен.")
        _, soup = await self.page(f"/mod/quiz/edit.php?cmid={cmid}")
        category = soup.find("input", attrs={"name": "category"})
        if not category:
            _, bank = await self.page(f"/question/edit.php?courseid={course_id}")
            category = bank.find("input", attrs={"name": "category"})
        if not category or not str(category.get("value", "")).isdigit():
            raise CourseError("Не найдена доступная категория банка вопросов.")
        added = []
        for question in questions:
            try:
                path = "/question/bank/editquestion/question.php"
                _, soup = await self.page(
                    path
                    + "?"
                    + urlencode(
                        {
                            "courseid": course_id,
                            "cmid": cmid,
                            "category": category["value"],
                            "qtype": "multichoice",
                            "returnurl": f"/mod/quiz/edit.php?cmid={cmid}",
                        }
                    )
                )
                form = self.form(soup, "_qf__qtype_multichoice_edit_form", path)
                values = form_values(form)
                if str(values.get("cmid")) != str(cmid) or values.get("qtype") != "multichoice":
                    raise CourseError("Несовпадение контекста вопроса.")
                values.update(
                    name=question.name,
                    defaultmark="1",
                    single="1",
                    status="ready",
                    updatebutton="1",
                    appendqnumstring="",
                )
                values["questiontext[text]"] = plain_html(question.text)
                values["generalfeedback[text]"] = plain_html(question.feedback)
                for i in range(5):
                    values[f"answer[{i}][text]"] = plain_html(question.answers[i]) if i < len(question.answers) else ""
                    values[f"fraction[{i}]"] = "1.0" if i == question.correct else "0.0"
                _, saved_soup = await self.page(path, data=values)
                saved_id = saved_soup.find("input", attrs={"name": "id"})
                if not saved_id or not str(saved_id.get("value", "")).isdigit():
                    raise CourseError("Сохранение вопроса не подтверждено.")
                question_id = int(saved_id["value"])
                saved_form = self.form(saved_soup, "_qf__qtype_multichoice_edit_form", path)
                saved = form_values(saved_form)
                if (
                    saved.get("name") != question.name
                    or saved.get("questiontext[text]") != values["questiontext[text]"]
                ):
                    raise CourseError("Текст сохранённого вопроса не совпал с отправленным.")
                for i in range(len(question.answers)):
                    if saved.get(f"answer[{i}][text]") != values[f"answer[{i}][text]"] or float(
                        saved.get(f"fraction[{i}]", "-999")
                    ) != (1.0 if i == question.correct else 0.0):
                        raise CourseError("Ответы сохранённого вопроса не совпали с отправленными.")
                # Explicit POST attaches the saved question; GET remains read-only.
                await self.page(
                    "/mod/quiz/edit.php",
                    data={"cmid": str(cmid), "addquestion": str(question_id), "sesskey": self._sesskey},
                )
                added.append({"name": question.name, "question_id": question_id})
            except (CourseError, httpx.HTTPError):
                raise CourseError(
                    f"Добавлено {len(added)} из {len(questions)} вопросов в cmid={cmid}; остановка на «{question.name}». В банке мог сохраниться отдельный вопрос. Проверьте состав перед повтором."
                ) from None
        final = await self.quiz_questions(course_id, cmid)
        if final["count"] != state["count"] + len(questions):
            raise CourseError(f"Число вопросов в cmid={cmid} не совпало с ожидаемым. Проверьте тест перед повтором.")
        return {**final, "added": added}

    async def assign_submissions(
        self, course_id: int, cmid: int, query: str | None = None, group: str | None = None, limit: int = 50
    ) -> dict:
        positive(course_id, "course_id")
        positive(cmid, "cmid")
        activity = await self.activity_in_course(course_id, cmid)
        if activity["type"] != "assign":
            raise CourseError(f"Элемент cmid={cmid} имеет тип '{activity['type']}', а не 'assign'.")

        await self.ready()
        _, soup = await self.page(f"/mod/assign/view.php?id={cmid}&action=grading")
        table = soup.select_one("table.flexible, table.generaltable")
        if not table:
            raise CourseError("Таблица ответов недоступна: возможно, у вас нет прав преподавателя на оценивание.")

        rows = []
        q_low = query.strip().lower() if query else None
        grp_low = group.strip().lower() if group else None

        for tr in table.select("tbody tr"):
            cells = tr.select("td")
            if not cells or len(cells) < 7:
                continue

            user_link = tr.select_one("a[href*='/user/view.php']")
            if not user_link:
                continue

            user_href = user_link["href"]
            user_id_m = re.search(r"id=(\d+)", user_href)
            user_id = int(user_id_m.group(1)) if user_id_m else None

            user_link_copy = BeautifulSoup(str(user_link), "html.parser").a
            if user_link_copy:
                for init_node in user_link_copy.select(".userinitials"):
                    init_node.decompose()
                user_name = user_link_copy.get_text(" ", strip=True)
            else:
                user_name = user_link.get_text(" ", strip=True)

            idnumber_td = tr.select_one("td.idnumber, td.c2")
            student_id = idnumber_td.get_text(strip=True) if idnumber_td else ""

            email_td = tr.select_one("td.email, td.c3")
            email = email_td.get_text(strip=True) if email_td else ""

            dept_td = tr.select_one("td.department, td.c4")
            dept = dept_td.get_text(strip=True) if dept_td else ""

            status_td = tr.select_one("td.status, td.c5")
            status = status_td.get_text(" ", strip=True) if status_td else ""

            grade_td = tr.select_one("td.grade, td.c6")
            grade_text = grade_td.get_text(" ", strip=True) if grade_td else ""
            current_grade = None
            max_grade = None
            if "/" in grade_text:
                parts = grade_text.split("/")
                first = parts[0].replace("Оценка пользователя", "").replace("Оценка", "").strip()
                if first and any(c.isdigit() for c in first):
                    current_grade = first
                second = parts[1].split()[0].strip() if len(parts) > 1 else ""
                if second and any(c.isdigit() for c in second):
                    max_grade = second
            elif grade_text:
                clean_g = grade_text.replace("Оценка пользователя", "").replace("Оценка", "").strip()
                if clean_g and any(c.isdigit() for c in clean_g):
                    current_grade = clean_g

            date_td = tr.select_one("td.timemodified, td.c7")
            submitted_at = date_td.get_text(" ", strip=True) if date_td else None
            if submitted_at in {"-", ""}:
                submitted_at = None

            files = []
            files_td = tr.select_one("td.c9, td[class*='assignsubmission_file']")
            if files_td:
                for a in files_td.select("a[href*='/pluginfile.php/']"):
                    f_url = trusted_url(a["href"])
                    f_name = a.get_text(" ", strip=True)
                    if f_url and f_url not in [f["url"] for f in files]:
                        files.append({"name": f_name, "url": f_url})

            text_td = tr.select_one("td.c8, td[class*='assignsubmission_onlinetext']")
            online_text = text_td.get_text(" ", strip=True) if text_td else None
            if online_text in {"-", ""}:
                online_text = None

            feedback_td = tr.select_one("td.c12, td[class*='assignfeedbackcomments']")
            feedback = feedback_td.get_text(" ", strip=True) if feedback_td else None
            if feedback in {"-", "", "Отзыв в виде комментария"}:
                feedback = None

            if not status or status == "-":
                status = "Сдано" if (files or online_text) else "Нет ответа"

            if q_low:
                search_space = f"{user_name} {student_id} {email} {dept}".lower()
                if q_low not in search_space:
                    continue

            if grp_low:
                if grp_low not in dept.lower():
                    continue

            rows.append({
                "user_id": user_id,
                "name": user_name,
                "student_id": student_id,
                "email": email,
                "group": dept,
                "status": status,
                "grade": current_grade,
                "grade_max": max_grade,
                "submitted_at": submitted_at,
                "files": files,
                "online_text": online_text,
                "feedback": feedback,
            })

        total = len(rows)
        paged = rows[:limit] if (limit and limit > 0) else rows
        return {
            "course_id": course_id,
            "cmid": cmid,
            "activity_name": activity["name"],
            "total_matches": total,
            "submissions": paged,
        }

    async def read_submission_file(self, course_id: int, cmid: int, file_url: str) -> dict:
        positive(course_id, "course_id")
        positive(cmid, "cmid")
        await self.ready()
        url = trusted_url(file_url)
        if "/pluginfile.php/" not in url:
            raise CourseError("Ссылка на файл должна вести на вложение в СДО (/pluginfile.php/).")

        async with self.client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code != 200:
                raise CourseError("СДО не разрешила скачивание файла ответа.")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > 25 * 1024 * 1024:
                    raise CourseError("Файл ответа превышает ограничение 25 МБ.")
        raw = bytes(chunks)
        filename = unquote(urlsplit(url).path.split("/")[-1]) or f"submission_{cmid}.bin"
        if raw.startswith(b"%PDF-"):
            try:
                reader = PdfReader(io.BytesIO(raw))
                texts = []
                size = 0
                for page in reader.pages[:200]:
                    part = page.extract_text() or ""
                    texts.append(part)
                    size += len(part)
                    if size > 120000:
                        break
                text = "\n\n".join(texts)
                truncated = len(reader.pages) > len(texts) or len(text) > 120000
            except Exception:
                raise CourseError("Не удалось извлечь текст PDF. Возможно, файл защищён или содержит только сканы.")
        elif filename.lower().endswith(".docx"):
            text = extract_docx_text(raw)
            if not text:
                raise CourseError("Не удалось извлечь текст из документа DOCX.")
            truncated = len(text) > 120000
            text = text[:120000]
        elif filename.lower().endswith(
            (".txt", ".csv", ".md", ".c", ".h", ".cpp", ".asm", ".s", ".py", ".json", ".xml", ".ino")
        ):
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("cp1251", errors="replace")
            truncated = len(text) > 120000
            text = text[:120000]
        else:
            raise CourseError(
                f"Формат файла '{filename}' не поддерживается для извлечения текста. "
                "Используйте sdo_download_submission_file для сохранения файла на диск."
            )

        return {"name": filename, "text": text, "truncated": truncated, "size_bytes": len(raw)}

    async def download_submission_file(
        self, course_id: int, cmid: int, file_url: str, save_path: str | None = None
    ) -> dict:
        positive(course_id, "course_id")
        positive(cmid, "cmid")
        await self.ready()
        url = trusted_url(file_url)
        if "/pluginfile.php/" not in url:
            raise CourseError("Ссылка на файл должна вести на вложение в СДО (/pluginfile.php/).")

        filename = unquote(urlsplit(url).path.split("/")[-1]) or f"submission_{cmid}.bin"

        async with self.client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code != 200:
                raise CourseError("СДО не разрешила скачивание файла ответа.")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > 50 * 1024 * 1024:
                    raise CourseError("Файл ответа превышает ограничение 50 МБ.")
        raw = bytes(chunks)

        if save_path:
            target = Path(save_path).expanduser().resolve()
            if target.is_dir():
                target = target / filename
        else:
            downloads = Path.home() / "Downloads"
            target_dir = downloads if downloads.exists() else Path.cwd()
            target = target_dir / filename

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)

        return {"name": filename, "saved_to": str(target), "size_bytes": len(raw)}

    async def grade_submission(
        self, course_id: int, cmid: int, user_id: int, grade: str | float, feedback: str = ""
    ) -> dict:
        positive(course_id, "course_id")
        positive(cmid, "cmid")
        positive(user_id, "user_id")
        activity = await self.activity_in_course(course_id, cmid)
        if activity["type"] != "assign":
            raise CourseError(f"Элемент cmid={cmid} имеет тип '{activity['type']}', а не 'assign'.")

        await self.ready()
        path = f"/mod/assign/view.php?id={cmid}&action=grade&userid={user_id}"
        _, soup = await self.page(path)
        form = soup.find("input", attrs={"name": "action", "value": "submitgrade"})
        form = form.find_parent("form") if form else None
        if not form:
            raise CourseError("Форма оценивания задания не найдена: возможно, нет прав преподавателя.")

        values = form_values(form)
        if not values.get("sesskey"):
            raise CourseError("В форме отсутствует защита сессии; операция остановлена.")

        values["grade"] = str(grade).replace(",", ".")
        if feedback:
            for k in list(values.keys()):
                if "assignfeedbackcomments" in k and "[text]" in k:
                    values[k] = feedback

        values["savegrade"] = "Сохранить"
        await self.page("/mod/assign/view.php", data=values)

        return {
            "course_id": course_id,
            "cmid": cmid,
            "user_id": user_id,
            "grade": str(grade),
            "feedback": feedback,
            "status": "graded",
        }
