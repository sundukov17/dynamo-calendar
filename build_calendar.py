#!/usr/bin/env python3
"""Собирает dynamo.ics — домашние официальные матчи ФК и ХК «Динамо» Москва.

Футбол берётся из официального ICS-фида клуба, хоккей — из hockey.json,
который обновляет облачный агент (у ХК «Динамо» нет рабочего фида).

Сыгранные матчи выбрасываются: это подписной календарь, прошлое в нём не нужно.
SEQUENCE у изменившихся событий увеличивается, иначе клиенты не подтянут правку.
"""

import hashlib
import json
import re
import ssl
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
ROOT = Path(__file__).parent
FOOTBALL_FEED = "https://fcdynamo.ru/calendar/dynamo"
OUT = ROOT / "dynamo.ics"
STATE = ROOT / "state.json"
HOCKEY = ROOT / "hockey.json"
HOCKEY_PAGE = "https://dynamo.ru/games"

# Средняя длительность, если источник не сказал иначе.
FOOTBALL_LEN = timedelta(hours=1, minutes=45)
HOCKEY_LEN = timedelta(hours=2, minutes=30)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "dynamo-calendar/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        # У Python из python.org на macOS часто пустое хранилище корневых
        # сертификатов. Проверку не отключаем — берём системный набор.
        if not isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            raise
        if not Path("/etc/ssl/cert.pem").exists():
            raise
        ctx = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
            return r.read().decode("utf-8", "replace")


def unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text)


def ics_unescape(v):
    return v.replace("\\,", ",").replace("\\;", ";").replace("\\n", "\n").replace("\\\\", "\\")


def ics_escape(v):
    return v.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    """RFC 5545: не длиннее 75 октетов, перенос — CRLF + пробел."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        # первая строка 75 октетов, продолжения — 74 (один занимает ведущий пробел)
        limit = 75 if not parts else 74
        if len(cur) + len(b) > limit:
            parts.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


def parse_football():
    """Домашние официальные матчи из фида клуба."""
    text = unfold(fetch(FOOTBALL_FEED))
    events = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        def get(key):
            m = re.search(rf"^{key}([^:]*):(.*)$", block, re.M)
            return (m.group(1), m.group(2).strip()) if m else ("", "")

        _, desc = get("DESCRIPTION")
        desc = ics_unescape(desc)
        if "Товарищеские" in desc:
            continue

        _, uid = get("UID")
        _, summary = get("SUMMARY")
        _, location = get("LOCATION")
        summary = ics_unescape(summary)

        # Хозяин — тот, кто назван первым. По стадиону судить нельзя: домашний
        # матч могут перенести на чужую арену, и он всё равно останется домашним.
        sides = re.split(r"\s+[-–—]\s+", summary)
        if len(sides) != 2 or sides[0].strip() != "Динамо":
            continue
        _, dtstart = get("DTSTART")
        _, dtend = get("DTEND")

        # «Альфа-Банк Российская Премьер-Лига, Тур 7» -> ('РПЛ', '7 тур')
        tour_raw, _, stage_raw = desc.partition(",")
        tour_raw, stage_raw = tour_raw.strip(), stage_raw.strip()
        if "Премьер-Лига" in tour_raw:
            tour, emoji = "РПЛ", "⚽"
        elif "Кубок" in tour_raw:
            tour, emoji = "Кубок России", "🏆"
        else:
            tour, emoji = tour_raw, "⚽"
        m = re.match(r"Тур\s+(\d+)", stage_raw)
        stage = f"{m.group(1)} тур" if m else stage_raw
        label = f"{tour}, {stage}" if stage else tour

        # Фид отдаёт локальное московское время без TZID.
        if "T" in dtstart:
            start = datetime.strptime(dtstart[:15], "%Y%m%dT%H%M%S").replace(tzinfo=MSK)
            end = (
                datetime.strptime(dtend[:15], "%Y%m%dT%H%M%S").replace(tzinfo=MSK)
                if "T" in dtend
                else start + FOOTBALL_LEN
            )
        else:
            start = datetime.strptime(dtstart[:8], "%Y%m%d").replace(tzinfo=MSK)
            end = None  # время ещё не назначено -> событие на весь день

        # В фиде встречаются и дефис, и тире — приводим к одному виду.
        summary = " — ".join(x.strip() for x in sides)
        events.append({
            "uid": f"fb-{uid}@dynamo-calendar",
            "summary": f"{emoji} {summary} ({label})",
            "start": start,
            "end": end,
            # Адрес берём из фида: домашние матчи бывают и не на «ВТБ Арене».
            "location": f"{ics_unescape(location).strip(chr(171)+chr(187))}, Москва",
            "description": "Источник: официальный ICS-фид ФК «Динамо» (fcdynamo.ru).",
        })
    return events


def scrape_hockey():
    """Домашние матчи с сайта ХК «Динамо».

    Сайт отдаёт только текущий месяц: рабочего фида на весь сезон у клуба нет
    (filter-games отвечает 500, khl.ru закрыт ботозащитой). Поэтому точное время
    проставляется матчам по мере того, как их месяц становится текущим.

    Возвращает (домашние официальные матчи, авторитетный месяц) — или (…, None),
    если месяц определить не удалось.
    """
    html = fetch(HOCKEY_PAGE)
    # В сетке месяца видны и соседние дни, поэтому «охваченным» считаем только
    # тот месяц, который страница реально показывает, — иначе сверка снесёт
    # матчи соседнего месяца, о которых сайт сейчас ничего не говорит.
    active = re.search(r'data-month="(\d+)"[^>]*_active', html)
    active_month = int(active.group(1)) if active else None
    blocks = re.findall(
        r'<div class="calendarframe"([^>]*)>(.*?)(?=<div class="calendarframe|<div class="line-regular)',
        html, re.S)
    games, years = [], set()
    for attrs, body in blocks:
        if 'data-content="fill"' not in attrs:
            continue
        get = lambda n: (re.search(rf'{n}="([^"]*)"', attrs) or ["", ""])[1]
        start = get("data-calendar-date-start")
        if not start:
            continue
        dt = datetime.strptime(start, "%m/%d/%Y %H:%M")
        if dt.month == active_month:
            years.add(dt.year)
        if get("data-location") != "home":
            continue

        # «Динамо (Москва) - Барыс (Астана)» -> «Барыс».
        # У минского «Динамо» город оставляем, иначе не отличить от своих.
        title = get("data-calendar-title")
        sides = [x.strip() for x in re.split(r"\s+[-–—]\s+", title)]
        rival = next((x for x in sides if not x.startswith("Динамо (Москва)")), "")
        city = (re.search(r"\(([^)]*)\)", rival) or ["", ""])[1].strip()
        name = re.sub(r"\s*\([^)]*\)", "", rival).strip()
        opponent = f"{name} {city}".strip() if name == "Динамо" and city else name

        tour = re.search(r'calendarthumb__title-detail.*?calendarthumb__text-item">\s*([^<]+?)\s*<', body, re.S)
        tournament = tour.group(1).strip() if tour else "КХЛ"
        # Календарь только про официальные матчи — предсезонку не берём.
        if re.search(r"Контрольн|Товарищ", tournament):
            continue
        gid = re.search(r"dynamo\.ru/game/(\d+)", body)
        games.append({
            "id": f"g{gid.group(1)}" if gid else f"d{dt:%Y%m%d}",
            "date": dt.strftime("%Y-%m-%d"),
            "time": dt.strftime("%H:%M"),
            "opponent": opponent,
            "tournament": tournament,
            "location": "ВТБ Арена, Москва",
        })
    # Год берём из матчей активного месяца: сезон переходит через Новый год.
    month = (years.pop(), active_month) if len(years) == 1 and active_month else None
    return games, month


def refresh_hockey_file():
    """Сверяет hockey.json с сайтом за те месяцы, которые сайт показал.

    Внутри охваченного месяца сайт считается истиной: так подхватываются переносы
    и отмены. Остальные месяцы не трогаем — про них сайт сейчас ничего не говорит.
    id сохраняется по дате, иначе у подписчиков разъедутся UID.
    """
    known = json.loads(HOCKEY.read_text(encoding="utf-8")) if HOCKEY.exists() else []
    try:
        scraped, month = scrape_hockey()
    except Exception as e:
        print(f"ВНИМАНИЕ: сайт ХК недоступен ({e}) — хоккей оставлен как есть", file=sys.stderr)
        return known
    if not month:
        print("ВНИМАНИЕ: не удалось понять, какой месяц показывает сайт ХК — "
              "хоккей оставлен как есть", file=sys.stderr)
        return known

    scraped = [g for g in scraped
               if (int(g["date"][:4]), int(g["date"][5:7])) == month]
    by_date = {g["date"]: g for g in known}
    kept = [g for g in known
            if (int(g["date"][:4]), int(g["date"][5:7])) != month]
    for g in scraped:
        prev = by_date.get(g["date"])
        if prev:
            g["id"] = prev["id"]                       # UID менять нельзя
            g["location"] = prev.get("location", g["location"])
        kept.append(g)

    kept.sort(key=lambda x: (x["date"], x["time"] or "99:99"))
    if kept != known:
        HOCKEY.write_text(json.dumps(kept, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"хоккей сверен с сайтом за {month[0]}-{month[1]:02d}: "
              f"было {len(known)}, стало {len(kept)}")
    return kept


def parse_hockey():
    events = []
    for g in refresh_hockey_file():
        d = datetime.strptime(g["date"], "%Y-%m-%d")
        if g.get("time"):
            hh, mm = (int(x) for x in g["time"].split(":"))
            start = d.replace(hour=hh, minute=mm, tzinfo=MSK)
            end = start + HOCKEY_LEN
        else:
            start, end = d.replace(tzinfo=MSK), None
        events.append({
            "uid": f"hk-{g['id']}@dynamo-calendar",
            "summary": f"🏒 Динамо — {g['opponent']} ({g.get('tournament', 'КХЛ')})",
            "start": start,
            "end": end,
            "location": g.get("location", "ВТБ Арена, Москва"),
            "description": "Источник: официальный сайт ХК «Динамо» (dynamo.ru).",
        })
    return events


def is_past(ev, now):
    """Матч сыгран: закончился по времени, либо его день уже прошёл."""
    if ev["end"]:
        return ev["end"] < now
    return ev["start"].date() < now.date()


def signature(ev):
    """Что именно видит подписчик. Меняется — значит нужен новый SEQUENCE."""
    end = ev["end"].isoformat() if ev["end"] else ""
    payload = f"{ev['summary']}|{ev['start'].isoformat()}|{end}|{ev['location']}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def render(events, now):
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    new_state, changed = {}, []
    stamp = now.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//sundukov17//dynamo-calendar//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Динамо Москва — домашние матчи",
        "X-WR-TIMEZONE:Europe/Moscow",
        # Подсказка клиентам, как часто перечитывать. Файл собирается раз в сутки.
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        "X-PUBLISHED-TTL:PT12H",
        "BEGIN:VTIMEZONE",
        "TZID:Europe/Moscow",
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        "TZOFFSETFROM:+0300",
        "TZOFFSETTO:+0300",
        "TZNAME:MSK",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]

    for ev in sorted(events, key=lambda e: e["start"]):
        uid, sig = ev["uid"], signature(ev)
        prev = state.get(uid)
        if prev is None:
            seq = 0
        elif prev["sig"] != sig:
            seq = prev["seq"] + 1
            changed.append(ev["summary"])
        else:
            seq = prev["seq"]
        new_state[uid] = {"sig": sig, "seq": seq}

        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}", f"SEQUENCE:{seq}"]
        if ev["end"]:
            lines += [
                f"DTSTART;TZID=Europe/Moscow:{ev['start'].strftime('%Y%m%dT%H%M%S')}",
                f"DTEND;TZID=Europe/Moscow:{ev['end'].strftime('%Y%m%dT%H%M%S')}",
            ]
        else:
            # Время ещё не объявлено — держим как событие на весь день.
            lines += [
                f"DTSTART;VALUE=DATE:{ev['start'].strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{(ev['start'] + timedelta(days=1)).strftime('%Y%m%d')}",
            ]
        lines += [
            fold(f"SUMMARY:{ics_escape(ev['summary'])}"),
            fold(f"LOCATION:{ics_escape(ev['location'])}"),
            fold(f"DESCRIPTION:{ics_escape(ev['description'])}"),
            f"LAST-MODIFIED:{stamp}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n", new_state, changed


def main():
    now = datetime.now(MSK)
    try:
        football = parse_football()
    except Exception as e:
        print(f"ОШИБКА: не удалось прочитать футбольный фид: {e}", file=sys.stderr)
        return 1
    if not football:
        print("ОШИБКА: в футбольном фиде нет домашних матчей — похоже, поменялся формат", file=sys.stderr)
        return 1

    hockey = parse_hockey()
    everything = football + hockey
    upcoming = [e for e in everything if not is_past(e, now)]
    dropped = len(everything) - len(upcoming)

    text, state, changed = render(upcoming, now)
    # newline="" — иначе Python схлопнет CRLF в LF и файл всегда будет «изменён».
    old = open(OUT, encoding="utf-8", newline="").read() if OUT.exists() else ""

    # DTSTAMP меняется всегда, поэтому сравниваем всё остальное.
    strip = lambda s: re.sub(r"^(DTSTAMP|LAST-MODIFIED):.*$", "", s, flags=re.M)
    if strip(old) == strip(text):
        print(f"без изменений: {len(upcoming)} матчей (футбол {len(football)}, хоккей {len(hockey)})")
        return 0

    open(OUT, "w", encoding="utf-8", newline="").write(text)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"обновлено: {len(upcoming)} матчей (футбол {len(football)}, хоккей {len(hockey)})")
    print(f"выброшено сыгранных: {dropped}")
    for c in changed:
        print(f"  изменилось: {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
