"""
Чтение публичной ленты t.me/s/rc_alerts без ключей и без входа в аккаунт.

Из пары постов «X ставит N$ / Дартс - <сектор>» и «X проигрывает/выигрывает N$»
собирается запись: на что ставили и выиграла ли ставка.
Сам выпавший сектор в ленте текстом не пишется, поэтому он известен
точно только если выиграла ставка на один конкретный сектор.
"""
import html
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

URL = "https://t.me/s/rc_alerts"
DB_PATH = "bets.db"
POLL_SECONDS = 60

# что покрывает ставка (красный/белый/центр/отскок)
COVER = {
    "красный сектор": {"red"},
    "белый сектор": {"white"},
    "белый сектор или отскок": {"white", "bounce"},
    "красный сектор или центр": {"red", "center"},
    "центр": {"center"},
    "отскок": {"bounce"},
}
NAMES = {"red": "🔴 красный", "white": "⚪ белый", "center": "🎯 центр", "bounce": "↩️ отскок"}

BET_RE = re.compile(r"^(?P<player>.+?)\s+ставит\s+[\d.,]+.*?Дартс\s*[-–—]\s*(?P<bet>[^\n]+)",
                    re.S | re.I)
RES_RE = re.compile(r"^(?P<player>.+?),.*?(?P<res>Проигрывает|Выигрывает)\s+[\d.,]+", re.S | re.I)
POST_RE = re.compile(r'data-post="[^"/]+/(?P<id>\d+)"(?P<body>.*?)(?=data-post="|\Z)', re.S)
TEXT_RE = re.compile(r'class="tgme_widget_message_text[^"]*"[^>]*>(?P<t>.*?)</div>', re.S)
TIME_RE = re.compile(r'<time[^>]*datetime="(?P<d>[^"]+)"')


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("CREATE TABLE IF NOT EXISTS feed(post INTEGER PRIMARY KEY, ts TEXT, "
                 "player TEXT, bet TEXT, won INT)")
    conn.execute("CREATE TABLE IF NOT EXISTS subs(chat INTEGER PRIMARY KEY)")
    return conn


def _clean(raw):
    raw = re.sub(r"<br\s*/?>", "\n", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    lines = [re.sub(r"\s+", " ", l).strip() for l in html.unescape(raw).split("\n")]
    return "\n".join(l for l in lines if l)


def parse(page):
    """Возвращает список (id, iso-время UTC, текст) по возрастанию id."""
    out = []
    for m in POST_RE.finditer(page):
        t = TEXT_RE.search(m.group("body"))
        if not t:
            continue
        d = TIME_RE.search(m.group("body"))
        try:
            ts = datetime.fromisoformat(d.group("d")).astimezone(timezone.utc).isoformat()
        except Exception:
            ts = datetime.now(timezone.utc).isoformat()
        out.append((int(m.group("id")), ts, _clean(t.group("t"))))
    return sorted(out)


def process(posts, open_bets, conn):
    """Склеивает ставки и результаты. open_bets: игрок -> тип ставки.
    Возвращает список только что добавленных событий (ts, ставка, выиграла)."""
    new = []
    for pid, ts, text in posts:
        b = BET_RE.search(text)
        if b:
            bet = b.group("bet").strip().lower().rstrip(".!")
            open_bets[b.group("player").strip()] = (ts, bet)
            continue
        r = RES_RE.search(text)
        if r:
            player = r.group("player").strip()
            opened = open_bets.pop(player, None)
            if opened:
                bet_ts, bet = opened  # время ставки, а не время результата
                won = 1 if r.group("res").lower().startswith("выигр") else 0
                cur = conn.execute("INSERT OR IGNORE INTO feed VALUES(?,?,?,?,?)",
                                   (pid, bet_ts, player, bet, won))
                if cur.rowcount:
                    new.append((bet_ts, bet, won))
    conn.commit()
    return new


def fetch():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", "replace")


MSK = timezone(timedelta(hours=3))


def msk_time(ts):
    return datetime.fromisoformat(ts).astimezone(MSK).strftime("%H:%M")


def describe(bet, won):
    cover = COVER.get(bet)
    if won and cover and len(cover) == 1:
        return f"✅ Выиграла. Выпал: {NAMES[next(iter(cover))]}"
    if won:
        return "✅ Выиграла"
    if cover:
        return "❌ Проиграла. Выпало не: " + " / ".join(NAMES[c] for c in sorted(cover))
    return "❌ Проиграла"


def subscribe(chat_id, on=True):
    conn = _connect()
    if on:
        conn.execute("INSERT OR IGNORE INTO subs VALUES(?)", (chat_id,))
    else:
        conn.execute("DELETE FROM subs WHERE chat=?", (chat_id,))
    conn.commit()
    conn.close()


def notify(conn, new):
    token = os.environ.get("BOT_TOKEN")
    chats = [r[0] for r in conn.execute("SELECT chat FROM subs")]
    if not token or not chats:
        return
    for ts, bet, won in new:
        text = f"🎯 *Дартс · {msk_time(ts)} МСК*\nСтавка: {bet}\n{describe(bet, won)}"
        for chat in chats:
            data = json.dumps({"chat_id": chat, "text": text, "parse_mode": "Markdown"}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=15).read()
            except Exception as e:
                print("notify error:", e)


def _loop():
    open_bets = {}
    conn = _connect()
    first = True  # первый проход только запоминает историю, без уведомлений
    while True:
        try:
            new = process(parse(fetch()), open_bets, conn)
            if not first:
                notify(conn, new)
            first = False
        except Exception as e:  # сеть может моргать, цикл не должен падать
            print("feed error:", e)
        time.sleep(POLL_SECONDS)


def start_feed():
    threading.Thread(target=_loop, daemon=True).start()


def summary_text(minutes=30):
    conn = _connect()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    rows = conn.execute("SELECT bet, won FROM feed WHERE ts>=?", (cutoff,)).fetchall()
    recent = conn.execute("SELECT ts, bet, won FROM feed WHERE ts>=? ORDER BY ts DESC LIMIT 15",
                          (cutoff,)).fetchall()
    conn.close()
    if not rows:
        return (f"📡 За {minutes} мин в ленте нет ставок на дартс.\n"
                "Лента обновляется раз в минуту, подожди немного.")
    per_bet, exact = {}, {}
    for bet, won in rows:
        w, n = per_bet.get(bet, (0, 0))
        per_bet[bet] = (w + won, n + 1)
        cover = COVER.get(bet)
        if won and cover and len(cover) == 1:
            k = next(iter(cover))
            exact[k] = exact.get(k, 0) + 1
    lines = [f"📡 *Лента за {minutes} мин*", f"Ставок на дартс: *{len(rows)}*", "",
             "*Ставки (время МСК · на что · итог):*"]
    lines += [f"• {msk_time(ts)} · {bet} · {'✅' if won else '❌'}" for ts, bet, won in recent]
    lines += ["", "*Выиграли / всего:*"]
    for bet, (w, n) in sorted(per_bet.items(), key=lambda x: -x[1][1]):
        lines.append(f"• {bet}: {w} / {n} ({w / n:.0%})")
    if exact:
        total = sum(exact.values())
        lines += ["", "*Точно известные выпадения:*"]
        lines += [f"• {NAMES[k]}: {v} ({v / total:.0%})"
                  for k, v in sorted(exact.items(), key=lambda x: -x[1])]
    lines += ["", "_Это лишь часть раундов, а проценты считаются только по выигравшим ставкам. "
                  "Каждый следующий раунд не зависит от прошлых._"]
    return "\n".join(lines)
