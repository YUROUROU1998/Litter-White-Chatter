import os
from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.exceptions import InvalidSignatureError

from db import get_conn, get_cursor, init_db
from agent import agent_parse_todos, agent_parse_transaction
from datetime import date, timedelta

app = Flask(__name__)

config = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])

# ── Emoji maps ──

PRIORITY_EMOJI = {"高": "🔴", "中": "🟡", "低": "🟢"}
CATEGORY_EMOJI_NOTE = {
    "生活": "🏠", "工作": "💼", "健康": "💪",
    "購物": "🛒", "娛樂": "🎮", "其他": "📌"
}
CATEGORY_EMOJI_RECORD = {
    "餐飲": "🍜", "交通": "🚇", "娛樂": "🎮", "購物": "🛍️",
    "醫療": "💊", "薪資": "💼", "獎金": "🎉", "其他": "📌"
}

# ── DB helpers ──

def get_user_mode(user_id: str) -> str:
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute("SELECT mode FROM user_state WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO user_state (user_id) VALUES (%s)", (user_id,))
        conn.commit()
        mode = "note"
    else:
        mode = row["mode"]
    cur.close()
    conn.close()
    return mode


def set_user_mode(user_id: str, mode: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO user_state (user_id, mode, updated_at) VALUES (%s, %s, NOW()) "
        "ON CONFLICT (user_id) DO UPDATE SET mode = %s, updated_at = NOW()",
        (user_id, mode, mode)
    )
    conn.commit()
    cur.close()
    conn.close()

# ── Reply helper ──

def reply(event, text: str):
    with ApiClient(config) as api_client:
        MessagingApi(api_client).reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=text)]
            )
        )

# ── Note mode handlers ──

def handle_note_natural(event, user_id: str, text: str):
    todos = agent_parse_todos(text)
    if todos is None:
        reply(event, "AI 解析失敗，請重試")
        return

    conn = get_conn()
    cur = conn.cursor()
    lines = ["已新增待辦：\n"]
    for t in todos:
        cur.execute(
            "INSERT INTO todos (user_id, title, category, priority, due_date) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (user_id, t["title"], t["category"], t["priority"], t.get("due_date", date.today().isoformat()))
        )
        tid = cur.fetchone()[0]
        p = PRIORITY_EMOJI.get(t["priority"], "")
        c = CATEGORY_EMOJI_NOTE.get(t["category"], "")
        lines.append(f"{c}{p} #{tid} {t['title']}")
        lines.append(f"   {t.get('due_date', '今天')}｜{t['category']}｜{t['priority']}")
    conn.commit()
    cur.close()
    conn.close()
    reply(event, "\n".join(lines))


def handle_note_today(event, user_id: str):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(
        "SELECT * FROM todos WHERE user_id = %s AND due_date = %s ORDER BY done, priority",
        (user_id, date.today())
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        reply(event, "今天沒有待辦事項")
        return

    undone = [r for r in rows if not r["done"]]
    done = [r for r in rows if r["done"]]

    lines = [f"今日待辦（{date.today()}）\n"]
    if undone:
        lines.append("── 未完成 ──")
        for r in undone:
            p = PRIORITY_EMOJI.get(r["priority"], "")
            c = CATEGORY_EMOJI_NOTE.get(r["category"], "")
            lines.append(f"{c}{p} #{r['id']} {r['title']}")
    if done:
        lines.append("\n── 已完成 ──")
        for r in done:
            c = CATEGORY_EMOJI_NOTE.get(r["category"], "")
            lines.append(f"{c} #{r['id']} {r['title']}")

    reply(event, "\n".join(lines))


def handle_note_week(event, user_id: str):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(
        "SELECT * FROM todos WHERE user_id = %s AND done = FALSE AND due_date >= %s "
        "ORDER BY due_date, priority",
        (user_id, date.today())
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        reply(event, "近期沒有未完成的待辦")
        return

    lines = ["近期未完成待辦\n"]
    for r in rows:
        p = PRIORITY_EMOJI.get(r["priority"], "")
        c = CATEGORY_EMOJI_NOTE.get(r["category"], "")
        lines.append(f"{c}{p} #{r['id']} {r['title']}")
        lines.append(f"   {r['due_date']}")
    reply(event, "\n".join(lines))


def handle_note_done(event, user_id: str, todo_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE todos SET done = TRUE WHERE id = %s AND user_id = %s", (todo_id, user_id)
    )
    affected = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()

    if affected:
        reply(event, f"待辦 #{todo_id} 已完成")
    else:
        reply(event, f"找不到待辦 #{todo_id}")


def handle_note_delete(event, user_id: str, todo_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM todos WHERE id = %s AND user_id = %s", (todo_id, user_id))
    affected = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()

    if affected:
        reply(event, f"已刪除待辦 #{todo_id}")
    else:
        reply(event, f"找不到待辦 #{todo_id}")


def handle_note_clear_done(event, user_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM todos WHERE user_id = %s AND done = TRUE", (user_id,))
    count = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    reply(event, f"已清除 {count} 筆已完成待辦")

# ── Record mode handlers ──

def handle_record_natural(event, user_id: str, text: str):
    tx = agent_parse_transaction(text)
    if tx is None:
        reply(event, "無法辨識為一筆交易，請輸入包含金額的消費或收入\n\n範例：午餐吃拉麵250元\n輸入「說明」查看所有指令")
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO transactions (user_id, type, category, amount, description, tx_date) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (user_id, tx["type"], tx["category"], tx["amount"], tx["description"],
         tx.get("tx_date", date.today().isoformat()))
    )
    tid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    emoji = CATEGORY_EMOJI_RECORD.get(tx["category"], "")
    tx_date = tx.get("tx_date", date.today().isoformat())
    reply(event, (
        f"已記錄 #{tid}\n\n"
        f"{emoji} {tx['category']}｜{tx['type']}\n"
        f"${tx['amount']:,}\n"
        f"{tx_date}\n"
        f"{tx['description']}"
    ))


def handle_record_balance(event, user_id: str):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(
        "SELECT type, COALESCE(SUM(amount), 0) AS total "
        "FROM transactions WHERE user_id = %s GROUP BY type",
        (user_id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    income = 0
    expense = 0
    for r in rows:
        if r["type"] == "收入":
            income = r["total"]
        else:
            expense = r["total"]
    balance = income - expense
    b_sign = "+" if balance >= 0 else ""

    reply(event, (
        f"帳戶總覽\n\n"
        f"總收入：${income:,}\n"
        f"總支出：${expense:,}\n"
        f"結餘：{b_sign}${balance:,}"
    ))


def handle_record_monthly(event, user_id: str):
    conn = get_conn()
    cur = get_cursor(conn)
    today = date.today()
    month_start = today.replace(day=1)

    cur.execute(
        "SELECT type, category, COALESCE(SUM(amount), 0) AS total "
        "FROM transactions WHERE user_id = %s AND tx_date >= %s "
        "GROUP BY type, category ORDER BY type, total DESC",
        (user_id, month_start)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        reply(event, "本月還沒有任何記錄")
        return

    income_total = 0
    expense_total = 0
    expense_lines = []
    income_lines = []

    for r in rows:
        emoji = CATEGORY_EMOJI_RECORD.get(r["category"], "")
        if r["type"] == "支出":
            expense_total += r["total"]
            expense_lines.append(f"{emoji} {r['category']}：${r['total']:,}")
        else:
            income_total += r["total"]
            income_lines.append(f"{emoji} {r['category']}：${r['total']:,}")

    balance = income_total - expense_total
    b_sign = "+" if balance >= 0 else ""

    lines = [f"{today.year}/{today.month} 月報\n"]
    if expense_lines:
        lines.append("── 支出 ──")
        lines.extend(expense_lines)
        lines.append(f"小計：${expense_total:,}\n")
    if income_lines:
        lines.append("── 收入 ──")
        lines.extend(income_lines)
        lines.append(f"小計：${income_total:,}\n")
    lines.append(f"本月結餘：{b_sign}${balance:,}")

    reply(event, "\n".join(lines))


def handle_record_recent(event, user_id: str):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(
        "SELECT * FROM transactions WHERE user_id = %s ORDER BY created_at DESC LIMIT 10",
        (user_id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        reply(event, "目前還沒有任何記錄")
        return

    lines = ["最近 10 筆記錄\n"]
    for r in rows:
        emoji = CATEGORY_EMOJI_RECORD.get(r["category"], "")
        t = "+" if r["type"] == "收入" else "-"
        d = r["tx_date"].strftime("%m/%d") if r["tx_date"] else r["created_at"].strftime("%m/%d")
        lines.append(f"#{r['id']} {d} {emoji} {t}${r['amount']:,} {r['description']}")
    lines.append("\n輸入「刪 編號」可刪除記錄")

    reply(event, "\n".join(lines))


def handle_record_delete(event, user_id: str, tx_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM transactions WHERE id = %s AND user_id = %s", (tx_id, user_id))
    affected = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()

    if affected:
        reply(event, f"已刪除記錄 #{tx_id}")
    else:
        reply(event, f"找不到記錄 #{tx_id}")

# ── Help messages ──

HELP_NOTE = (
    "Note 模式指令：\n\n"
    "直接輸入 AI 自動建立待辦\n"
    "今天 → 查看今日待辦\n"
    "本週 → 查看近期未完成\n"
    "完成 [id] → 標記完成\n"
    "刪 [id] → 刪除待辦\n"
    "清除完成 → 清空已完成項目"
)

HELP_RECORD = (
    "Record 模式指令：\n\n"
    "直接輸入 AI 自動記帳\n"
    "帳戶 → 查看收支總覽\n"
    "本月 → 查看本月報表\n"
    "明細 → 查看最近 10 筆\n"
    "刪 [id] → 刪除記錄"
)

# ── Main routing ──

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    mode = get_user_mode(user_id)

    # ── Global commands ──
    if text == "#note":
        set_user_mode(user_id, "note")
        reply(event, f"已切換至 Note 模式\n\n{HELP_NOTE}")
        return
    if text == "#record":
        set_user_mode(user_id, "record")
        reply(event, f"已切換至 Record 模式\n\n{HELP_RECORD}")
        return
    if text == "說明":
        reply(event, HELP_NOTE if mode == "note" else HELP_RECORD)
        return
    if text == "模式":
        mode_name = "Note（待辦清單）" if mode == "note" else "Record（記帳）"
        reply(event, f"目前模式：{mode_name}\n\n輸入 #note 或 #record 切換模式")
        return

    # ── Note mode ──
    if mode == "note":
        if text in ("今天", "今日"):
            handle_note_today(event, user_id)
        elif text in ("本週", "這週"):
            handle_note_week(event, user_id)
        elif text == "清除完成":
            handle_note_clear_done(event, user_id)
        elif text.startswith("完成"):
            try:
                todo_id = int(text.replace("完成", "").strip())
                handle_note_done(event, user_id, todo_id)
            except ValueError:
                reply(event, "格式錯誤，請輸入：完成 [編號]")
        elif text.startswith("刪"):
            try:
                todo_id = int(text.replace("刪", "").strip())
                handle_note_delete(event, user_id, todo_id)
            except ValueError:
                reply(event, "格式錯誤，請輸入：刪 [編號]")
        else:
            handle_note_natural(event, user_id, text)
        return

    # ── Record mode ──
    if mode == "record":
        if text in ("帳戶", "餘額", "總覽"):
            handle_record_balance(event, user_id)
        elif text in ("本月", "月報", "本月報表"):
            handle_record_monthly(event, user_id)
        elif text in ("明細", "紀錄", "最近") or "最近" in text and "筆" in text:
            handle_record_recent(event, user_id)
        elif text.startswith("刪"):
            try:
                tx_id = int(text.replace("刪", "").strip())
                handle_record_delete(event, user_id, tx_id)
            except ValueError:
                reply(event, "格式錯誤，請輸入：刪 [編號]")
        else:
            handle_record_natural(event, user_id, text)
        return


# ── App startup ──

init_db()

if __name__ == "__main__":
    app.run(port=5000, debug=True)
