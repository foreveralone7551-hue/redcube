"""
Бот-учёт для игры в дартс: ставки, лимиты, статистика.
Не прогнозирует результаты (это невозможно), а помогает контролировать игру.

Запуск:
    pip install python-telegram-bot==21.6
    BOT_TOKEN=токен_от_BotFather python bot.py
"""
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

SECTORS = {
    "red":    ("🔴 Красный", 3.0),
    "white":  ("⚪ Белый", 3.0),
    "wbounce": ("⚪ Белый / отскок", 2.0),
    "rcenter": ("🔴 Красный / центр", 2.0),
    "any":    ("🌀 Любой", 1.5),
    "center": ("🎯 Центр", 5.0),
    "bounce": ("↩️ Отскок", 5.0),
}

db = sqlite3.connect("bets.db", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS bets(id INTEGER PRIMARY KEY, uid INT, sector TEXT,
    stake REAL, won INT, profit REAL, session INT);
CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY, loss_limit REAL,
    win_goal REAL, session INT DEFAULT 1);
""")


def user(uid):
    db.execute("INSERT OR IGNORE INTO users(uid) VALUES(?)", (uid,))
    return db.execute("SELECT loss_limit, win_goal, session FROM users WHERE uid=?",
                      (uid,)).fetchone()


def session_net(uid, session):
    r = db.execute("SELECT COALESCE(SUM(profit),0) FROM bets WHERE uid=? AND session=?",
                   (uid, session)).fetchone()
    return r[0]


def fmt(x):
    return f"{x:+.0f}".replace("+0", "0")


MENU = Kb([
    [Btn("➕ Новая ставка", callback_data="new")],
    [Btn("📊 Статистика", callback_data="stats"), Btn("⚙️ Лимиты", callback_data="limits")],
    [Btn("🔄 Новая сессия", callback_data="reset")],
])


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    _, _, s = user(uid)
    net = session_net(uid, s)
    await update.effective_message.reply_text(
        "🎯 *Дартс-трекер*\n"
        "━━━━━━━━━━━━━━\n"
        f"Баланс сессии: *{fmt(net)}*\n\n"
        "Записывай ставки, следи за лимитами и смотри реальную статистику.",
        parse_mode="Markdown", reply_markup=MENU)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    loss, goal, s = user(uid)
    d = q.data

    if d == "menu":
        net = session_net(uid, s)
        await q.edit_message_text(
            f"🎯 *Дартс-трекер*\n━━━━━━━━━━━━━━\nБаланс сессии: *{fmt(net)}*",
            parse_mode="Markdown", reply_markup=MENU)

    elif d == "new":
        rows = [[Btn(f"{n}  ×{m:g}", callback_data=f"sec:{k}")] for k, (n, m) in SECTORS.items()]
        rows.append([Btn("⬅️ Назад", callback_data="menu")])
        await q.edit_message_text("*Куда поставил?*", parse_mode="Markdown", reply_markup=Kb(rows))

    elif d.startswith("sec:"):
        ctx.user_data["sector"] = d[4:]
        ctx.user_data["wait"] = "stake"
        name, mult = SECTORS[d[4:]]
        await q.edit_message_text(f"{name}  ×{mult:g}\n\n💰 Напиши сумму ставки числом:")

    elif d.startswith("res:"):
        won = d == "res:win"
        sector, stake = ctx.user_data.get("sector"), ctx.user_data.get("stake")
        if sector is None or stake is None:
            await q.edit_message_text("Ставка не найдена. Начни заново.", reply_markup=MENU)
            return
        mult = SECTORS[sector][1]
        profit = stake * (mult - 1) if won else -stake
        db.execute("INSERT INTO bets(uid,sector,stake,won,profit,session) VALUES(?,?,?,?,?,?)",
                   (uid, sector, stake, int(won), profit, s))
        db.commit()
        ctx.user_data.clear()
        net = session_net(uid, s)
        text = (f"{'✅ Выигрыш' if won else '❌ Проигрыш'}: *{fmt(profit)}*\n"
                f"Баланс сессии: *{fmt(net)}*")
        if loss and net <= -loss:
            text += "\n\n⛔ *Лимит проигрыша достигнут. Остановись на сегодня.*"
        elif goal and net >= goal:
            text += "\n\n🏁 *Цель по выигрышу достигнута. Забирай и заканчивай.*"
        await q.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=Kb([[Btn("➕ Ещё", callback_data="new"),
                                                    Btn("🏠 Меню", callback_data="menu")]]))

    elif d == "stats":
        tot = db.execute("SELECT COUNT(*), COALESCE(SUM(won),0), COALESCE(SUM(profit),0), "
                         "COALESCE(SUM(stake),0) FROM bets WHERE uid=?", (uid,)).fetchone()
        n, wins, profit, staked = tot
        if n == 0:
            await q.edit_message_text("Пока нет ставок.", reply_markup=Kb([[Btn("⬅️ Назад", callback_data="menu")]]))
            return
        lines = [f"📊 *Статистика*\n━━━━━━━━━━━━━━",
                 f"Ставок: *{n}*   Побед: *{wins}* ({wins / n:.0%})",
                 f"Поставлено: *{staked:.0f}*",
                 f"Итого: *{fmt(profit)}*",
                 "\n*По секторам:*"]
        for k, (name, mult) in SECTORS.items():
            r = db.execute("SELECT COUNT(*), COALESCE(SUM(won),0), COALESCE(SUM(profit),0) "
                           "FROM bets WHERE uid=? AND sector=?", (uid, k)).fetchone()
            if r[0]:
                lines.append(f"{name}: {r[0]} шт, {r[1] / r[0]:.0%}, {fmt(r[2])}")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                  reply_markup=Kb([[Btn("⬅️ Назад", callback_data="menu")]]))

    elif d == "limits":
        await q.edit_message_text(
            "⚙️ *Лимиты на сессию*\n━━━━━━━━━━━━━━\n"
            f"Стоп при проигрыше: *{loss or 'нет'}*\n"
            f"Стоп при выигрыше: *{goal or 'нет'}*\n\n"
            "Изменить:\n`/loss 500`\n`/win 1000`\n(`0` — отключить)",
            parse_mode="Markdown", reply_markup=Kb([[Btn("⬅️ Назад", callback_data="menu")]]))

    elif d == "reset":
        db.execute("UPDATE users SET session=session+1 WHERE uid=?", (uid,))
        db.commit()
        await q.edit_message_text("🔄 Новая сессия начата. Баланс: *0*", parse_mode="Markdown",
                                  reply_markup=MENU)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("wait") != "stake":
        return
    try:
        stake = float(update.message.text.replace(",", "."))
        assert stake > 0
    except Exception:
        await update.message.reply_text("Нужно число больше нуля, например 100")
        return
    ctx.user_data["stake"] = stake
    ctx.user_data["wait"] = None
    name, mult = SECTORS[ctx.user_data["sector"]]
    await update.message.reply_text(
        f"{name} ×{mult:g}, ставка *{stake:g}*\n\nЧто выпало?", parse_mode="Markdown",
        reply_markup=Kb([[Btn("✅ Выигрыш", callback_data="res:win"),
                          Btn("❌ Проигрыш", callback_data="res:lose")]]))


async def set_limit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    col = "loss_limit" if update.message.text.startswith("/loss") else "win_goal"
    try:
        val = float(ctx.args[0])
    except Exception:
        await update.message.reply_text("Пример: /loss 500")
        return
    user(update.effective_user.id)
    db.execute(f"UPDATE users SET {col}=? WHERE uid=?", (val or None, update.effective_user.id))
    db.commit()
    await update.message.reply_text("✅ Сохранено", reply_markup=MENU)


async def backup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Присылает файл базы. Чтобы восстановить после сброса хостинга, просто отправь этот файл боту."""
    db.commit()
    with open("bets.db", "rb") as f:
        await update.message.reply_document(
            f, filename="bets.db",
            caption="💾 Резервная копия. После перезапуска хостинга отправь этот файл боту, и данные вернутся.")


async def restore(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.endswith(".db"):
        return
    tg_file = await doc.get_file()
    await tg_file.download_to_drive("restore.db")
    src = sqlite3.connect("restore.db")
    try:
        src.backup(db)
        await update.message.reply_text("✅ Данные восстановлены.", reply_markup=MENU)
    except Exception:
        await update.message.reply_text("❌ Не получилось восстановить, файл повреждён.")
    finally:
        src.close()


class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def run_ping_server():
    """Нужен для Render/Koyeb: хостингу нужен открытый порт, а UptimeRobot будет пинговать его."""
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()


def main():
    threading.Thread(target=run_ping_server, daemon=True).start()
    app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("backup", backup))
    app.add_handler(MessageHandler(filters.Document.ALL, restore))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["loss", "win"], set_limit))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()
