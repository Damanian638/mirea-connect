#!/usr/bin/env python3
"""
Поиск и вывод точного сырого item из payload API МИРЭА по заданным критериям:
аудитория, номер пары, номер недели (и опционально день недели).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

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
    print("Ошибка: pymirea не найдена.", file=sys.stderr)
    sys.exit(1)


LESSON_BELLS = {
    1: ("09:00", "10:30"),
    2: ("10:40", "12:10"),
    3: ("12:40", "14:10"),
    4: ("14:20", "15:50"),
    5: ("16:20", "17:50"),
    6: ("18:00", "19:30"),
    7: ("19:40", "21:10"),
}

WEEKDAYS_MAP = {
    0: ("ПН", "ПОНЕДЕЛЬНИК"),
    1: ("ВТ", "ВТОРНИК"),
    2: ("СР", "СРЕДА"),
    3: ("ЧТ", "ЧЕТВЕРГ"),
    4: ("ПТ", "ПЯТНИЦА"),
    5: ("СБ", "СУББОТА"),
}


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


def get_semester_week(dt: datetime) -> int:
    year = dt.year
    sem_start = datetime(year, 9, 1) if dt.month >= 8 else datetime(year, 2, 9)
    sem_monday = sem_start - timedelta(days=sem_start.weekday())
    curr_monday = dt - timedelta(days=dt.weekday())
    delta_days = (curr_monday.date() - sem_monday.date()).days
    return max(1, (delta_days // 7) + 1)


def is_classroom_match(classrooms: list[str], room_name: str, campus: str = "В-78") -> bool:
    m = re.match(r"^([А-ЯA-Z])[-_ ]?(\d+)$", room_name.strip(), re.IGNORECASE)
    norm_room = f"{m.group(1).upper()}-{m.group(2)}" if m else room_name.strip().upper()
    norm_room = norm_room.replace("B", "В").replace(" ", "")
    campus_norm = campus.replace("B", "В").replace(" ", "").upper()

    KNOWN_OTHER_CAMPUSES = ["С-20", "C-20", "МП-1", "В-86", "СГ-22", "КОЛЛЕДЖ"]

    for c in classrooms:
        c_norm = c.replace("B", "В").replace(" ", "").upper()
        if any(other in c_norm for other in KNOWN_OTHER_CAMPUSES) and campus_norm not in c_norm:
            continue
        if norm_room in c_norm:
            return True
    return False


async def main():
    parser = argparse.ArgumentParser(description="Поиск конкретного item в расписании МИРЭА")
    parser.add_argument("-r", "--room", default="Б-215", help="Номер аудитории (например: Б-215 или Б215)")
    parser.add_argument("-p", "--pair", type=int, default=3, help="Номер пары (1-7, по умолчанию 3)")
    parser.add_argument("-w", "--week", type=int, default=9, help="Номер недели семестра (по умолчанию 9)")
    parser.add_argument("-d", "--day", default=None, help="День недели опционально (Пн, Вт, Ср, Чт, Пт, Сб)")
    parser.add_argument("-c", "--campus", default="В-78", help="Кампус (по умолчанию В-78)")

    args = parser.parse_args()

    m_r = re.match(r"^([А-ЯA-Z])[-_ ]?(\d+)$", args.room.strip(), re.IGNORECASE)
    room_clean = f"{m_r.group(1).upper()}-{m_r.group(2)}" if m_r else args.room.strip().upper()

    sched = MireaSchedule()
    try:
        print(f"-> Поиск аудитории {room_clean} ({args.campus})...")
        candidates = await sched.search_classrooms(room_clean)

        picked = None
        for cand in candidates:
            full_name = getattr(cand, "name", "")
            if (args.campus in full_name or "B-78" in full_name) and (room_clean in full_name):
                picked = cand
                break

        if not picked:
            print(f"[!] Аудитория {room_clean} ({args.campus}) не найдена в поиске API.")
            return

        print(f"-> Найдена: {picked.name} (uid={picked.uid}). Запрос к базе...")
        payload, err = await sched._rpc(
            "get_schedule_for_entity",
            {
                "p_entity_type": "classroom",
                "p_entity": str(picked.uid),
                "p_organization_id": SCHEDULE_ORGANIZATION_ID,
            },
        )

        if err or not payload:
            print(f"[!] Ошибка API: {err}")
            return

        raw_items = payload if isinstance(payload, list) else []

        matches = []
        for item in raw_items:
            # 1. Проверка аудитории
            raw_classrooms = item.get("classrooms") or []
            classrooms_list = [c.get("name") if isinstance(c, dict) else str(c) for c in raw_classrooms]
            if not is_classroom_match(classrooms_list, room_clean, args.campus):
                continue

            # 2. Проверка пары
            bells = item.get("lesson_bells") or {}
            pair_num = get_pair_number_from_str(bells.get("start_time") or "")
            if pair_num != args.pair:
                continue

            # 3. Проверка недели и дня
            dates = item.get("dates") or []
            matched_dates = []
            for d_str in dates:
                dt = parse_item_date(d_str)
                if not dt or dt.weekday() > 5:
                    continue

                if get_semester_week(dt) == args.week:
                    if args.day:
                        day_query = args.day.strip().upper()
                        day_names = WEEKDAYS_MAP[dt.weekday()]
                        if not any(day_query in n for n in day_names):
                            continue
                    matched_dates.append(dt)

            if matched_dates:
                matches.append((item, matched_dates))

        print("\n" + "=" * 60)
        target_day_str = f", день: {args.day}" if args.day else ""
        print(f"КРИТЕРИИ: {room_clean} ({args.campus}) | {args.pair}-я пара | Неделя {args.week}{target_day_str}")
        print(f"Найдено занятий: {len(matches)}")
        print("=" * 60)

        if not matches:
            print("[!] В этот слот занятий не найдено (аудитория свободна или не совпали критерии).")
            return

        for idx, (item, dates_list) in enumerate(matches, 1):
            sample_dt = dates_list[0]
            day_name = WEEKDAYS_MAP[sample_dt.weekday()][1].capitalize()
            dates_formatted = ", ".join(d.strftime("%d.%m.%Y") for d in dates_list)

            print(f"\n--- [РЕЗУЛЬТАТ #{idx}] День: {day_name} ({dates_formatted}) ---")
            print(json.dumps(item, ensure_ascii=False, indent=2))

    finally:
        await sched.close()


if __name__ == "__main__":
    asyncio.run(main())
