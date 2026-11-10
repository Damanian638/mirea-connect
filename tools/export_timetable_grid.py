#!/usr/bin/env python3
"""
Экспорт семестрового расписания аудиторий Б-201, 202, 207, 208, 215, 221 (В-78)
в Excel в формате официальной сетки (дни недели, пары 1-7, недели I / II).
Извлекает группы напрямую из сырого payload Supabase RPC.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import sys

# Корректное добавление пути к src/ как при запуске из корня проекта, так и из tools/
for base_dir in (Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent):
    candidate_src = base_dir / "src"
    if candidate_src.exists() and str(candidate_src) not in sys.path:
        sys.path.insert(0, str(candidate_src))
        break

try:
    from pymirea.schedule import MireaSchedule
    try:
        from pymirea.schedule import SCHEDULE_ORGANIZATION_ID
    except ImportError:
        SCHEDULE_ORGANIZATION_ID = "mirea"
except ImportError:
    print("Ошибка: не найдена библиотека pymirea. Установите: pip install ..", file=sys.stderr)
    sys.exit(1)

try:
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    print("Ошибка: требуется библиотека openpyxl (pip install openpyxl)", file=sys.stderr)
    sys.exit(1)


TARGET_ROOMS = [
    "Б-201",
    "Б-202",
    "Б-207",
    "Б-208",
    "Б-215",
    "Б-221",
]
CAMPUS = "В-78"

LESSON_BELLS = {
    1: ("09:00", "10:30"),
    2: ("10:40", "12:10"),
    3: ("12:40", "14:10"),
    4: ("14:20", "15:50"),
    5: ("16:20", "17:50"),
    6: ("18:00", "19:30"),
    7: ("19:40", "21:10"),
}

WEEKDAYS = [
    (0, "Понедельник"),
    (1, "Вторник"),
    (2, "Среда"),
    (3, "Четверг"),
    (4, "Пятница"),
    (5, "Суббота"),
]


def get_pair_number_from_str(start_str: str) -> int | None:
    if not start_str:
        return None
    try:
        sh, sm = map(int, start_str.split(":")[:2])
        curr_minutes = sh * 60 + sm
        for pair_num, (b_start, _) in LESSON_BELLS.items():
            bh, bm = map(int, b_start.split(":"))
            if abs(curr_minutes - (bh * 60 + bm)) <= 20:
                return pair_num
    except Exception:
        pass
    return None


def parse_item_date(d_str: str) -> datetime | None:
    if not d_str:
        return None
    d_str = str(d_str).strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(d_str, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(d_str)
    except Exception:
        pass
    return None


def get_semester_week(dt: datetime) -> tuple[int, int]:
    year = dt.year
    if dt.month >= 8:
        sem_start = datetime(year, 9, 1)
    else:
        sem_start = datetime(year, 2, 9)

    sem_monday = sem_start - timedelta(days=sem_start.weekday())
    curr_monday = dt - timedelta(days=dt.weekday())

    delta_days = (curr_monday.date() - sem_monday.date()).days
    week_num = max(1, (delta_days // 7) + 1)
    parity = 1 if (week_num % 2 == 1) else 2
    return week_num, parity


def format_weeks(week_set: set[int], parity: int) -> str:
    if not week_set:
        return ""

    weeks = sorted(week_set)
    min_w, max_w = weeks[0], weeks[-1]
    expected_step2 = list(range(min_w, max_w + 1, 2))

    if weeks == expected_step2:
        if len(weeks) == 1:
            return f"{min_w} н."
        if min_w <= 2 and max_w >= 16:
            return ""
        return f"{min_w}-{max_w} н."

    all_possible = list(range(1 if parity == 1 else 2, 19, 2))
    missing = set(all_possible) - week_set
    if len(missing) == 1 and max_w >= 16:
        return f"кр. {missing.pop()} н."

    if len(weeks) <= 4:
        return f"{','.join(map(str, weeks))} н."

    return f"{min_w}-{max_w} н."


def normalize_lesson_type(type_raw: str | None) -> str:
    t = (type_raw or "").lower()
    if any(k in t for k in ("lecture", "лек", "лк")):
        return "ЛК"
    if any(k in t for k in ("practice", "прак", "пр")):
        return "ПР"
    if any(k in t for k in ("lab", "лаб")):
        return "ЛАБ"
    if any(k in t for k in ("unknown", "мер", "мр")):
        return "МР"
    return type_raw.upper() if type_raw else ""


def shorten_fio(fio: str) -> str:
    if not fio:
        return ""
    parts = fio.strip().split()
    if len(parts) >= 3:
        return f"{parts[0]} {parts[1][0]}. {parts[2][0]}."
    if len(parts) == 2:
        return f"{parts[0]} {parts[1][0]}."
    return fio


def is_classroom_match(classrooms: list[str], room_name: str, campus: str = "В-78") -> bool:
    room_norm = room_name.replace("B", "В").replace(" ", "").upper()
    campus_norm = campus.replace("B", "В").replace(" ", "").upper()
    KNOWN_OTHER_CAMPUSES = ["С-20", "C-20", "МП-1", "В-86", "СГ-22", "КОЛЛЕДЖ"]

    for c in classrooms:
        c_norm = c.replace("B", "В").replace(" ", "").upper()
        if any(other in c_norm for other in KNOWN_OTHER_CAMPUSES) and campus_norm not in c_norm:
            continue
        if room_norm in c_norm:
            return True

    return False


async def load_grid_for_room(sched: MireaSchedule, room_name: str) -> dict:
    print(f"-> Поиск аудитории {room_name} ({CAMPUS})...")
    candidates = await sched.search_classrooms(room_name)

    picked = None
    for cand in candidates:
        full_name = getattr(cand, "name", "")
        if ("В-78" in full_name or "B-78" in full_name) and (room_name in full_name):
            picked = cand
            break

    if not picked:
        print(f"   [!] Аудитория {room_name} ({CAMPUS}) не найдена в поиске!")
        return {}

    print(f"   Найдена: {picked.name} (uid={picked.uid}). Загрузка сырого payload...")

    payload, err = await sched._rpc(
        "get_schedule_for_entity",
        {
            "p_entity_type": "classroom",
            "p_entity": str(picked.uid),
            "p_organization_id": SCHEDULE_ORGANIZATION_ID,
        },
    )

    if err or not payload:
        print(f"   [!] Ошибка API: {err}")
        return {}

    raw_items = payload if isinstance(payload, list) else []
    print(f"   Всего получено элементов базы: {len(raw_items)}")

    grid_accumulator = defaultdict(lambda: defaultdict(set))
    matched_items = 0

    for item in raw_items:
        # 1. Проверяем аудиторию
        raw_classrooms = item.get("classrooms") or item.get("classroom") or []
        if isinstance(raw_classrooms, dict):
            raw_classrooms = [raw_classrooms]
        classrooms_list = [
            c.get("name") if isinstance(c, dict) else str(c)
            for c in raw_classrooms
        ]
        if not is_classroom_match(classrooms_list, room_name, CAMPUS):
            continue

        matched_items += 1

        # 2. Номер пары по звонкам
        bells = item.get("lesson_bells") or {}
        start_time = bells.get("start_time") or ""
        pair_num = get_pair_number_from_str(start_time)
        if not pair_num:
            continue

        # 3. Предмет и вид пары
        subject = item.get("subject") or ""
        lesson_type = normalize_lesson_type(item.get("lesson_type"))

        # 4. Группы
        raw_groups = item.get("groups") or item.get("group") or []
        if isinstance(raw_groups, (str, dict)):
            raw_groups = [raw_groups]
        groups_list = []
        for g in raw_groups:
            if isinstance(g, dict):
                g_name = g.get("name") or g.get("target_title") or g.get("title") or ""
            else:
                g_name = str(g)
            if g_name:
                groups_list.append(g_name)
        groups_str = ", ".join(dict.fromkeys(groups_list))

        # 5. Преподаватели
        raw_teachers = item.get("teachers") or item.get("teacher") or []
        if isinstance(raw_teachers, (str, dict)):
            raw_teachers = [raw_teachers]
        teachers_list = []
        for t in raw_teachers:
            if isinstance(t, dict):
                t_name = t.get("name") or t.get("target_title") or t.get("title") or ""
            else:
                t_name = str(t)
            if t_name:
                teachers_list.append(shorten_fio(t_name))
        teachers_str = ", ".join(dict.fromkeys(teachers_list))

        lesson_key = (subject, lesson_type, groups_str, teachers_str)

        # 6. Разбор дат
        dates = item.get("dates") or []
        for d_str in dates:
            dt = parse_item_date(d_str)
            if not dt or dt.weekday() > 5:
                continue
            weekday = dt.weekday()
            week_num, parity = get_semester_week(dt)
            grid_accumulator[(weekday, pair_num, parity)][lesson_key].add(week_num)

    print(f"   Отфильтровано для {room_name} ({CAMPUS}): {matched_items} записей")

    final_grid = defaultdict(list)
    for slot_key, lessons_dict in grid_accumulator.items():
        _, _, parity = slot_key
        for lesson_key, weeks in lessons_dict.items():
            subj, l_type, grp, teach = lesson_key
            weeks_label = format_weeks(weeks, parity)
            final_grid[slot_key].append({
                "weeks": weeks_label,
                "subject": subj,
                "type": l_type,
                "groups": grp,
                "teacher": teach,
            })

    return final_grid


def render_room_sheet(ws, room_name: str, grid: dict) -> None:
    day_header_fill = PatternFill(start_color="899EC6", end_color="899EC6", fill_type="solid")
    table_header_fill = PatternFill(start_color="3E5A8A", end_color="3E5A8A", fill_type="solid")
    even_row_fill = PatternFill(start_color="F7F9FC", end_color="F7F9FC", fill_type="solid")

    font_title = Font(name="Calibri", size=16, bold=True, color="1F2A44")
    font_day = Font(name="Calibri", size=12, bold=True, color="FFFFFF")
    font_th = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    font_bold = Font(name="Calibri", size=10, bold=True)
    font_main = Font(name="Calibri", size=10)

    thin_border = Border(
        left=Side(style="thin", color="CBD2E0"),
        right=Side(style="thin", color="CBD2E0"),
        top=Side(style="thin", color="CBD2E0"),
        bottom=Side(style="thin", color="CBD2E0"),
    )
    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_align = Alignment(horizontal="left", vertical="center", wrap_text=True)

    # 1. Первая строчка с названием
    ws.merge_cells("A1:G1")
    cell_title = ws["A1"]
    cell_title.value = f"Расписание аудитории {room_name} ({CAMPUS})"
    cell_title.font = font_title
    cell_title.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 30

    current_row = 2
    headers = ["Пара", "Время", "Неделя", "Дисциплина (недели)", "Вид", "Группы", "Преподаватель"]

    for w_idx, w_name in WEEKDAYS:
        ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=7)
        day_cell = ws.cell(row=current_row, column=1)
        day_cell.value = w_name.upper()
        day_cell.font = font_day
        day_cell.fill = day_header_fill
        day_cell.alignment = center_align
        ws.row_dimensions[current_row].height = 24
        current_row += 1

        for col_idx, h_text in enumerate(headers, 1):
            th_cell = ws.cell(row=current_row, column=col_idx, value=h_text)
            th_cell.font = font_th
            th_cell.fill = table_header_fill
            th_cell.alignment = center_align
            th_cell.border = thin_border
        ws.row_dimensions[current_row].height = 20
        current_row += 1

        for pair_num in range(1, 8):
            time_start, time_end = LESSON_BELLS[pair_num]
            time_str = f"{time_start}–{time_end}"

            for parity, parity_label in [(1, "I"), (2, "II")]:
                lessons = grid.get((w_idx, pair_num, parity), [])

                if lessons:
                    subj_lines = []
                    type_lines = []
                    grp_lines = []
                    teach_lines = []
                    for l in lessons:
                        w_prefix = f"{l['weeks']} " if l['weeks'] else ""
                        subj_lines.append(f"{w_prefix}{l['subject']}".strip())
                        type_lines.append(l['type'])
                        grp_lines.append(l['groups'])
                        teach_lines.append(l['teacher'])

                    subj_val = "\n".join(subj_lines)
                    type_val = "\n".join(type_lines)
                    grp_val = "\n".join(grp_lines)
                    teach_val = "\n".join(teach_lines)
                else:
                    subj_val, type_val, grp_val, teach_val = "", "", "", ""

                ws.append([pair_num, time_str, parity_label, subj_val, type_val, grp_val, teach_val])
                r_num = ws.max_row
                ws.row_dimensions[r_num].height = 22 if not lessons else max(26, len(lessons) * 18)

                for c_idx in range(1, 8):
                    c = ws.cell(row=r_num, column=c_idx)
                    c.border = thin_border
                    c.font = font_bold if c_idx in (1, 3) else font_main
                    c.alignment = left_align if c_idx in (4, 6, 7) else center_align
                    if parity == 2:
                        c.fill = even_row_fill

            current_row += 2
        current_row += 1

    col_widths = {1: 7, 2: 14, 3: 9, 4: 48, 5: 8, 6: 24, 7: 24}
    for col_i, width in col_widths.items():
        col_letter = get_column_letter(col_i)
        ws.column_dimensions[col_letter].width = width


async def main() -> int:
    output_file = "расписание_аудиторий_РПТ.xlsx"
    sched = MireaSchedule()
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    try:
        for room in TARGET_ROOMS:
            grid = await load_grid_for_room(sched, room)
            ws = wb.create_sheet(title=room)
            render_room_sheet(ws, room, grid)
            ws.freeze_panes = "A1"

        wb.save(output_file)
        print(f"\n[✓] Готово! Итоговая сетка сохранена в: {output_file}")
    finally:
        await sched.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))