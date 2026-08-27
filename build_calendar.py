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
from html import unescape
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
ROOT = Path(__file__).parent
FOOTBALL_FEED = "https://fcdynamo.ru/calendar/dynamo"
OUT = ROOT / "dynamo.ics"
STATE = ROOT / "state.json"
HOCKEY = ROOT / "hockey.json"
HOCKEY_PAGE = "https://www.sports.ru/hockey/club/dinamo/calendar/"
# Ни клуб, ни sports.ru арену не публикуют — держим известное значение.
HOCKEY_VENUE = "ВТБ Ледовый Дворец, Москва"

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


def ics_unescape_entities(v):
    """HTML-сущности в тексте страницы: &nbsp;, &ndash; и прочее."""
    return unescape(v)


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
    """Домашние матчи КХЛ из календаря команды на sports.ru.

    Сайт клуба не годится: filter-games отвечает 500, а /games отдаёт только
    текущий месяц — время сентябрьских матчей в августе там не увидеть.
    На sports.ru лежит весь сезон сразу, с точным временем там, где оно назначено.
    """
    html_text = fetch(HOCKEY_PAGE)
    rows = re.findall(
        r"<tr[^>]*>((?:(?!</tr>).)*?/hockey/match/(?:(?!</tr>).)*?)</tr>",
        html_text, re.S)

    games, seen = [], {}
    for row in rows:
        cells = [x.strip() for x in re.split(r"<[^>]+>", ics_unescape_entities(row)) if x.strip()]
        if "Дома" not in cells:
            continue
        date = next((c for c in cells if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", c)), None)
        if not date:
            continue
        time = next((c for c in cells if re.fullmatch(r"\d{2}:\d{2}", c)), None)
        # Необъявленное время sports.ru показывает как полночь, сдвигая её на
        # 01:00 или 02:00. Домашние матчи в Москве раньше полудня не начинаются,
        # так что любое утреннее время здесь — заглушка, а не расписание.
        if time and time < "10:00":
            time = None

        # Соперник — из ссылки на его клуб; slug даёт стабильный id, не зависящий
        # от того, как сайт пишет название, и переживающий перенос матча.
        rival = re.search(r'/hockey/club/([\w-]+)/"[^>]*title="([^"]+)"', row)
        if not rival:
            continue
        slug, name = rival.group(1), rival.group(2)
        seen[slug] = seen.get(slug, 0) + 1

        games.append({
            "id": f"{slug}-{seen[slug]}",
            "date": f"{date[6:]}-{date[3:5]}-{date[:2]}",
            "time": time,
            "opponent": name,
            "tournament": "КХЛ",
            "location": HOCKEY_VENUE,
        })

    games.sort(key=lambda g: (g["date"], g["time"] or "99:99"))
    return games


def refresh_hockey_file():
    """Переписывает hockey.json тем, что говорит источник.

    Источник покрывает весь сезон, поэтому сверять помесячно больше не нужно.
    Но если он отвалился или отдал подозрительно мало матчей, оставляем прошлый
    снимок: пустой календарь хуже слегка устаревшего.
    """
    known = json.loads(HOCKEY.read_text(encoding="utf-8")) if HOCKEY.exists() else []
    try:
        games = scrape_hockey()
    except Exception as e:
        print(f"ВНИМАНИЕ: источник по хоккею недоступен ({e}) — взят прошлый снимок",
              file=sys.stderr)
        return known
    if len(games) < 10 and known:
        print(f"ВНИМАНИЕ: источник отдал всего {len(games)} домашних матчей — "
              "похоже на сбой, взят прошлый снимок", file=sys.stderr)
        return known

    if games != known:
        HOCKEY.write_text(json.dumps(games, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
        timed = sum(1 for g in games if g["time"])
        print(f"хоккей обновлён: {len(games)} домашних матчей, "
              f"время известно у {timed}")
    return games


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
            "description": "Источник: календарь команды на sports.ru.",
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
