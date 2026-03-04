#!/usr/bin/env python3
import os
import json
import logging
import base64
from datetime import date
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from anthropic import Anthropic
import database as db

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))

anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)

PLAN = {
    "deficit": {"calories": 2300, "protein": 185, "fat": 68, "carbs": 210, "label": "Дефицит"},
    "maintenance": {"calories": 2900, "protein": 185, "fat": 95, "carbs": 340, "label": "Поддержка"}
}

def get_day_type(for_date=None):
    d = for_date or date.today()
    return "maintenance" if d.weekday() >= 5 else "deficit"

def get_day_plan(for_date=None):
    return PLAN[get_day_type(for_date)]

def is_allowed(update):
    if ALLOWED_USER_ID == 0:
        return True
    return update.effective_user.id == ALLOWED_USER_ID

def parse_json_response(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())

def parse_food_with_ai(food_text):
    prompt = f"""Ты нутрициолог-аналитик. Пользователь написал что съел: "{food_text}"
Отвечай ТОЛЬКО валидным JSON без markdown:
{{"items":[{{"name":"название","amount":"количество","calories":0,"protein":0,"fat":0,"carbs":0}}],"total":{{"calories":0,"protein":0,"fat":0,"carbs":0}},"confidence":"high/medium/low","notes":""}}"""
    r = anthropic.messages.create(model="claude-sonnet-4-20250514", max_tokens=1000, messages=[{"role":"user","content":prompt}])
    return parse_json_response(r.content[0].text)

def parse_food_from_photo(photo_b64, caption=""):
    prompt = f"""Ты нутрициолог. Определи еду на фото, посчитай КБЖУ. Контекст: "{caption}"
Отвечай ТОЛЬКО валидным JSON без markdown:
{{"items":[{{"name":"название","amount":"порция","calories":0,"protein":0,"fat":0,"carbs":0}}],"total":{{"calories":0,"protein":0,"fat":0,"carbs":0}},"confidence":"high/medium/low","notes":"что видишь"}}"""
    r = anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":photo_b64}},
            {"type":"text","text":prompt}
        ]}]
    )
    return parse_json_response(r.content[0].text)

def get_ai_analysis(daily_summary, plan, day_label, history):
    history_text = ""
    if history:
        history_text = "\n\nИстория:\n" + "\n".join([f"- {h['date']}: {h['calories']:.0f} ккал Б:{h['protein']:.0f}г ({h['day_type']})" for h in history[-7:]])
    prompt = f"""Персональный нутрициолог, атлет на сушке: 26л, 83кг, 189см, активный, схема 5/2.
Сегодня ({day_label}), план: {plan['calories']} ккал Б:{plan['protein']}г Ж:{plan['fat']}г У:{plan['carbs']}г
Факт: {daily_summary['calories']:.0f} ккал Б:{daily_summary['protein']:.0f}г Ж:{daily_summary['fat']:.0f}г У:{daily_summary['carbs']:.0f}г{history_text}
3-5 предложений: как день, динамика, совет на завтра. По-русски, конкретно."""
    r = anthropic.messages.create(model="claude-sonnet-4-20250514", max_tokens=500, messages=[{"role":"user","content":prompt}])
    return r.content[0].text.strip()

def get_weekly_analysis(data):
    if not data: return "Недостаточно данных."
    rows = "\n".join([f"- {d['date']} ({d['day_type']}): {d['calories']:.0f}ккал Б:{d['protein']:.0f}г Ж:{d['fat']:.0f}г У:{d['carbs']:.0f}г" for d in data])
    prompt = f"""Нутрициолог, разбери неделю (26л, 83кг, активный, 5/2 схема):\n{rows}\nЦели: дефицит 2300 (пн-пт), поддержка 2900 (сб-вс), белок 185г+. Соблюдение, среднее, БЖУ баланс, топ-2 совета. По-русски."""
    r = anthropic.messages.create(model="claude-sonnet-4-20250514", max_tokens=700, messages=[{"role":"user","content":prompt}])
    return r.content[0].text.strip()

def pbar(current, target, length=8):
    pct = min(current / target, 1.3) if target > 0 else 0
    filled = int(pct * length)
    if pct > 1.1: bar = "🔴" * min(filled, length)
    elif pct > 0.85: bar = "🟢" * filled + "⬜" * (length - filled)
    else: bar = "🟡" * filled + "⬜" * (length - filled)
    return f"{bar} {int(pct*100)}%"

def format_food_result(result, totals, plan, source="text"):
    items_text = "".join([f"  • {i['name']} {i['amount']}: {i['calories']:.0f} ккал | Б:{i['protein']:.0f} Ж:{i['fat']:.0f} У:{i['carbs']:.0f}\n" for i in result['items']])
    cal_left = plan['calories'] - totals['calories']
    prot_left = plan['protein'] - totals['protein']
    conf = {"high":"✅","medium":"⚠️","low":"❓"}.get(result.get('confidence','medium'),"⚠️")
    header = "📸 *Распознано с фото:*" if source == "photo" else f"{conf} *Записано:*"
    text = f"""{header}

{items_text}
*Добавлено:* `{result['total']['calories']:.0f}` ккал | Б:`{result['total']['protein']:.0f}г` Ж:`{result['total']['fat']:.0f}г` У:`{result['total']['carbs']:.0f}г`

*За сегодня:* `{totals['calories']:.0f}` / `{plan['calories']}` ккал
Осталось: `{cal_left:.0f}` ккал | Белка: `{prot_left:.0f}г`"""
    if result.get('notes'):
        text += f"\n\n💬 _{result['notes']}_"
    return text

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    db.init_db()
    plan = get_day_plan()
    day_type = get_day_type()
    emoji = "🔥" if day_type == "deficit" else "⚖️"
    await update.message.reply_text(f"""👋 *Привет! Твой личный нутрициолог готов.*

Сегодня: *{emoji} {plan['label']}* — {plan['calories']} ккал
Б: {plan['protein']}г | Ж: {plan['fat']}г | У: {plan['carbs']}г

*Как добавить еду:*
✏️ Напиши текстом: _"курица 200г, рис 150г"_
📸 Отправь фото тарелки (можно с подписью)

*Команды:*
/itog — сводка дня + AI-анализ
/nedelya — аналитика за 7 дней
/plan — твой план на сегодня
/history — данные за 7 дней""", parse_mode="Markdown")

async def show_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    plan = get_day_plan()
    day_type = get_day_type()
    d, m = PLAN['deficit'], PLAN['maintenance']
    weekday_ru = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"][date.today().weekday()]
    emoji = "🔥" if day_type == "deficit" else "⚖️"
    await update.message.reply_text(f"""📋 *Твой план*

Сегодня *{weekday_ru}* → {emoji} {plan['label']}

🎯 Калории: `{plan['calories']}` ккал
🥩 Белок:   `{plan['protein']}г`
🥑 Жиры:   `{plan['fat']}г`
🍚 Углеводы: `{plan['carbs']}г`

*Схема недели:*
Пн-Пт → 🔥 Дефицит ({d['calories']} ккал)
Сб-Вс → ⚖️ Поддержка ({m['calories']} ккал)""", parse_mode="Markdown")

async def daily_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    today = date.today().isoformat()
    totals = db.get_daily_totals(today)
    entries = db.get_daily_entries(today)
    plan = get_day_plan()
    day_type = get_day_type()
    if totals['calories'] == 0:
        await update.message.reply_text("📭 Сегодня ещё нет записей. Напиши что съел или отправь фото!")
        return
    cal_left = plan['calories'] - totals['calories']
    signed = lambda x: f"+{x:.0f}" if x > 0 else f"{x:.0f}"
    emoji = "🔥" if day_type == "deficit" else "⚖️"
    await update.message.reply_text(f"""📊 *Итог — {date.today().strftime('%d.%m.%Y')}*
{emoji} {plan['label']} | {len(entries)} приёмов

*Калории:*
{pbar(totals['calories'], plan['calories'])}
`{totals['calories']:.0f}` / `{plan['calories']}` ккал ({signed(cal_left)} осталось)

*Макро:*
🥩 Белок:    `{totals['protein']:.0f}г` / `{plan['protein']}г`  {pbar(totals['protein'], plan['protein'], 6)}
🥑 Жиры:    `{totals['fat']:.0f}г` / `{plan['fat']}г`  {pbar(totals['fat'], plan['fat'], 6)}
🍚 Углеводы: `{totals['carbs']:.0f}г` / `{plan['carbs']}г`  {pbar(totals['carbs'], plan['carbs'], 6)}""", parse_mode="Markdown")
    thinking = await update.message.reply_text("🤖 Анализирую...")
    analysis = get_ai_analysis(totals, plan, plan['label'], db.get_history(days=7))
    await thinking.edit_text(f"💡 *AI-анализ:*\n\n{analysis}", parse_mode="Markdown")

async def weekly_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    data = db.get_history(days=7)
    if not data:
        await update.message.reply_text("📭 Недостаточно данных. Веди трекинг несколько дней!")
        return
    lines = ["📈 *Последние 7 дней:*\n"]
    for d in data:
        plan_cal = PLAN['maintenance']['calories'] if d['day_type'] == 'maintenance' else PLAN['deficit']['calories']
        diff = d['calories'] - plan_cal
        emoji = "⚖️" if d['day_type'] == 'maintenance' else "🔥"
        lines.append(f"{emoji} `{d['date']}` {d['calories']:.0f}ккал {'▲' if diff>0 else '▼'}{abs(diff):.0f} | Б:{d['protein']:.0f}г")
    avg_cal = sum(d['calories'] for d in data) / len(data)
    avg_prot = sum(d['protein'] for d in data) / len(data)
    lines.append(f"\n📊 Среднее: `{avg_cal:.0f} ккал` | `{avg_prot:.0f}г белка`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    thinking = await update.message.reply_text("🤖 Готовлю недельный разбор...")
    await thinking.edit_text(f"🗓 *Недельный AI-анализ:*\n\n{get_weekly_analysis(data)}", parse_mode="Markdown")

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    history = db.get_history(days=7)
    if not history:
        await update.message.reply_text("📭 История пуста. Начни вести трекинг!")
        return
    lines = ["📜 *История (7 дней):*\n"]
    for d in history:
        icon = "⚖️" if d['day_type'] == 'maintenance' else "🔥"
        diff = d['calories'] - PLAN[d['day_type']]['calories']
        lines.append(f"{icon} `{d['date']}` — {d['calories']:.0f} ккал ({'+' if diff>=0 else ''}{diff:.0f})\n   Б:{d['protein']:.0f} Ж:{d['fat']:.0f} У:{d['carbs']:.0f}\n")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def handle_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    msg = await update.message.reply_text("⏳ Считаю КБЖУ...")
    try:
        result = parse_food_with_ai(update.message.text.strip())
        today = date.today().isoformat()
        db.add_food_entry(date=today, description=update.message.text.strip(),
            items=json.dumps(result['items'], ensure_ascii=False),
            calories=result['total']['calories'], protein=result['total']['protein'],
            fat=result['total']['fat'], carbs=result['total']['carbs'], day_type=get_day_type())
        await msg.edit_text(format_food_result(result, db.get_daily_totals(today), get_day_plan()), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Food error: {e}")
        await msg.edit_text("❌ Не смог распознать. Попробуй: _«курица 200г, рис 150г»_", parse_mode="Markdown")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    msg = await update.message.reply_text("📸 Анализирую фото...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()
        photo_b64 = base64.b64encode(photo_bytes).decode()
        caption = update.message.caption or ""
        result = parse_food_from_photo(photo_b64, caption)
        today = date.today().isoformat()
        db.add_food_entry(date=today, description=caption or "фото еды",
            items=json.dumps(result['items'], ensure_ascii=False),
            calories=result['total']['calories'], protein=result['total']['protein'],
            fat=result['total']['fat'], carbs=result['total']['carbs'], day_type=get_day_type())
        await msg.edit_text(format_food_result(result, db.get_daily_totals(today), get_day_plan(), source="photo"), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await msg.edit_text("❌ Не смог распознать фото. Попробуй написать текстом.", parse_mode="Markdown")

def main():
    db.init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("itog", daily_summary))
    app.add_handler(CommandHandler("nedelya", weekly_report))
    app.add_handler(CommandHandler("plan", show_plan))
    app.add_handler(CommandHandler("history", show_history))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_food))
    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
