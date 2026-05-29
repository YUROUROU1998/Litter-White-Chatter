import os
from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest, TextMessage
)
import re
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.exceptions import InvalidSignatureError

from db import get_conn, get_cursor, init_db
from agent import agent_parse_todos, agent_parse_transaction, agent_chat
from datetime import date, datetime, timedelta, timezone

TZ_TW = timezone(timedelta(hours=8))

def today_tw():
    return datetime.now(TZ_TW).date()

app = Flask(__name__)

config = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])

# ── Chat mode: in-memory conversation history (cleared on mode switch) ──
chat_histories = {}
pending_confirmations = {}

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

PRIORITY_ORDER = "CASE priority WHEN '高' THEN 1 WHEN '中' THEN 2 WHEN '低' THEN 3 ELSE 4 END"

def fmt_todo_date(row):
    d = row["due_date"].strftime("%m/%d") if row.get("due_date") else ""
    t = row["due_time"].strftime("%H:%M") if row.get("due_time") else ""
    return f"{d} {t}".strip()

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

def push_message(user_id: str, text: str):
    with ApiClient(config) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=text)]
            )
        )

# ── Push time helpers ──

def set_push_time(user_id: str, time_str: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE user_state SET push_time = %s WHERE user_id = %s",
        (time_str, user_id)
    )
    conn.commit()
    cur.close()
    conn.close()


def clear_push_time(user_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE user_state SET push_time = NULL WHERE user_id = %s",
        (user_id,)
    )
    conn.commit()
    cur.close()
    conn.close()


def get_push_time(user_id: str):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute("SELECT push_time FROM user_state WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row["push_time"] if row and row["push_time"] else None


# ── Bulk delete confirmation ──

def month_range(month_str: str) -> dict:
    year, month = month_str.split("-")
    year, month = int(year), int(month)
    start = f"{year}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1}-01-01"
    else:
        end = f"{year}-{month + 1:02d}-01"
    return {"month_start": start, "month_end": end}


def count_records(mode: str, user_id: str, action: str, params: dict) -> int:
    conn = get_conn()
    cur = conn.cursor()
    table = "todos" if mode == "note" else "transactions"
    date_col = "due_date" if mode == "note" else "tx_date"

    if action == "clear_done":
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = %s AND done = TRUE", (user_id,))
    elif action == "delete_all":
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = %s", (user_id,))
    elif action == "delete_by_date":
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = %s AND {date_col} = %s",
                    (user_id, params["date"]))
    elif action == "delete_by_month":
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = %s AND {date_col} >= %s AND {date_col} < %s",
                    (user_id, params["month_start"], params["month_end"]))

    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return count


def request_confirm(event, user_id: str, mode: str, action: str, params: dict, description: str):
    count = count_records(mode, user_id, action, params)
    if count == 0:
        reply(event, "沒有符合條件的記錄")
        return
    pending_confirmations[user_id] = {"mode": mode, "action": action, "params": params}
    reply(event, f"即將{description}（共 {count} 筆）\n\n輸入「確認」執行，其他輸入取消")


def execute_pending(event, user_id: str):
    pending = pending_confirmations.pop(user_id)
    mode = pending["mode"]
    action = pending["action"]
    params = pending.get("params", {})

    conn = get_conn()
    cur = conn.cursor()
    table = "todos" if mode == "note" else "transactions"
    date_col = "due_date" if mode == "note" else "tx_date"

    if action == "clear_done":
        cur.execute(f"DELETE FROM {table} WHERE user_id = %s AND done = TRUE", (user_id,))
    elif action == "delete_all":
        cur.execute(f"DELETE FROM {table} WHERE user_id = %s", (user_id,))
    elif action == "delete_by_date":
        cur.execute(f"DELETE FROM {table} WHERE user_id = %s AND {date_col} = %s",
                    (user_id, params["date"]))
    elif action == "delete_by_month":
        cur.execute(f"DELETE FROM {table} WHERE user_id = %s AND {date_col} >= %s AND {date_col} < %s",
                    (user_id, params["month_start"], params["month_end"]))

    count = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    reply(event, f"已刪除 {count} 筆記錄")


# ── Note mode handlers ──

def handle_note_natural(event, user_id: str, text: str):
    result = agent_parse_todos(text)
    if result is None:
        reply(event, "AI 解析失敗，請重試")
        return

    action = result.get("action", "unknown")

    if action == "add":
        conn = get_conn()
        cur = conn.cursor()
        lines = ["已新增待辦：\n"]
        for t in result.get("items", []):
            due_time = t.get("due_time") if t.get("due_time") else None
            cur.execute(
                "INSERT INTO todos (user_id, title, category, priority, due_date, due_time) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (user_id, t["title"], t["category"], t["priority"],
                 t.get("due_date", today_tw().isoformat()), due_time)
            )
            tid = cur.fetchone()[0]
            p = PRIORITY_EMOJI.get(t["priority"], "")
            c = CATEGORY_EMOJI_NOTE.get(t["category"], "")
            date_str = t.get("due_date", today_tw().isoformat())
            try:
                date_display = datetime.strptime(date_str, "%Y-%m-%d").strftime("%m/%d")
            except ValueError:
                date_display = date_str
            if due_time:
                date_display += f" {due_time}"
            lines.append(f"{c}{p} #{tid} {t['title']}")
            lines.append(f"   {date_display}｜{t['category']}｜{t['priority']}")
        conn.commit()
        cur.close()
        conn.close()
        reply(event, "\n".join(lines))

    elif action == "done":
        ids = result.get("ids", [])
        conn = get_conn()
        cur = conn.cursor()
        done_ids = []
        for i in ids:
            cur.execute("UPDATE todos SET done = TRUE WHERE id = %s AND user_id = %s", (i, user_id))
            if cur.rowcount:
                done_ids.append(str(i))
        conn.commit()
        cur.close()
        conn.close()
        if done_ids:
            reply(event, f"已完成 #{', #'.join(done_ids)}")
        else:
            reply(event, "找不到對應的待辦")

    elif action == "delete":
        ids = result.get("ids", [])
        conn = get_conn()
        cur = conn.cursor()
        deleted_ids = []
        for i in ids:
            cur.execute("DELETE FROM todos WHERE id = %s AND user_id = %s", (i, user_id))
            if cur.rowcount:
                deleted_ids.append(str(i))
        conn.commit()
        cur.close()
        conn.close()
        if deleted_ids:
            reply(event, f"已刪除 #{', #'.join(deleted_ids)}")
        else:
            reply(event, "找不到對應的待辦")

    elif action == "edit":
        todo_id = result.get("id")
        updates = result.get("updates", {})
        if todo_id:
            handle_note_edit(event, user_id, todo_id, updates)
        else:
            reply(event, "請指定要修改的待辦編號，例如：修改#3 標題改成買菜")

    elif action == "delete_all":
        request_confirm(event, user_id, "note", "delete_all", {}, "刪除全部待辦")

    elif action == "delete_by_date":
        d = result.get("date", today_tw().isoformat())
        request_confirm(event, user_id, "note", "delete_by_date", {"date": d}, f"刪除 {d} 的待辦")

    elif action == "delete_by_month":
        m = result.get("month", today_tw().strftime("%Y-%m"))
        request_confirm(event, user_id, "note", "delete_by_month", month_range(m), f"刪除 {m} 的待辦")

    else:
        reply(event, "小白要碎碎念～")


def handle_note_today(event, user_id: str):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(
        f"SELECT * FROM todos WHERE user_id = %s AND due_date = %s "
        f"ORDER BY done, {PRIORITY_ORDER}, due_time NULLS LAST",
        (user_id, today_tw())
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        reply(event, "今天沒有待辦事項")
        return

    undone = [r for r in rows if not r["done"]]
    done = [r for r in rows if r["done"]]
    today_str = today_tw().strftime("%m/%d")

    lines = [f"今日待辦（{today_str}）\n"]
    if undone:
        lines.append("── 未完成 ──")
        for r in undone:
            p = PRIORITY_EMOJI.get(r["priority"], "")
            c = CATEGORY_EMOJI_NOTE.get(r["category"], "")
            time_str = f" {r['due_time'].strftime('%H:%M')}" if r.get("due_time") else ""
            lines.append(f"{c}{p} #{r['id']} {r['title']}{time_str}")
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
        f"SELECT * FROM todos WHERE user_id = %s AND done = FALSE AND due_date >= %s "
        f"ORDER BY due_date, {PRIORITY_ORDER}, due_time NULLS LAST",
        (user_id, today_tw())
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
        lines.append(f"   {fmt_todo_date(r)}")
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


def handle_note_edit(event, user_id: str, todo_id: int, updates: dict):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM todos WHERE id = %s AND user_id = %s", (todo_id, user_id))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        reply(event, f"找不到待辦 #{todo_id}")
        return

    allowed = {"title", "category", "priority", "due_date", "due_time"}
    fields = {k: v for k, v in updates.items() if k in allowed and v}
    if not fields:
        cur.close()
        conn.close()
        reply(event, "沒有需要修改的內容")
        return

    set_clause = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [todo_id, user_id]
    cur.execute(f"UPDATE todos SET {set_clause} WHERE id = %s AND user_id = %s", values)
    conn.commit()

    cur.execute("SELECT * FROM todos WHERE id = %s AND user_id = %s", (todo_id, user_id))
    updated = cur.fetchone()
    cur.close()
    conn.close()

    p = PRIORITY_EMOJI.get(updated["priority"], "")
    c = CATEGORY_EMOJI_NOTE.get(updated["category"], "")
    field_names = {"title": "標題", "category": "分類", "priority": "優先度", "due_date": "日期", "due_time": "時間"}
    changed = "、".join(field_names.get(k, k) for k in fields)
    reply(event, (
        f"已修改 #{todo_id}（{changed}）\n\n"
        f"{c}{p} {updated['title']}\n"
        f"{fmt_todo_date(updated)}｜{updated['category']}｜{updated['priority']}"
    ))


def handle_note_clear_done(event, user_id: str):
    request_confirm(event, user_id, "note", "clear_done", {}, "清除已完成的待辦")

# ── Record mode handlers ──

def handle_record_natural(event, user_id: str, text: str):
    result = agent_parse_transaction(text)
    if result is None:
        reply(event, "AI 解析失敗，請重試")
        return

    action = result.get("action", "unknown")

    if action == "add":
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO transactions (user_id, type, category, amount, description, tx_date) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (user_id, result["type"], result["category"], result["amount"], result["description"],
             result.get("tx_date", today_tw().isoformat()))
        )
        tid = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        emoji = CATEGORY_EMOJI_RECORD.get(result["category"], "")
        tx_date = result.get("tx_date", today_tw().isoformat())
        reply(event, (
            f"已記錄 #{tid}\n\n"
            f"{emoji} {result['category']}｜{result['type']}\n"
            f"${result['amount']:,}\n"
            f"{tx_date}\n"
            f"{result['description']}"
        ))

    elif action == "delete":
        ids = result.get("ids", [])
        conn = get_conn()
        cur = conn.cursor()
        deleted_ids = []
        for i in ids:
            cur.execute("DELETE FROM transactions WHERE id = %s AND user_id = %s", (i, user_id))
            if cur.rowcount:
                deleted_ids.append(str(i))
        conn.commit()
        cur.close()
        conn.close()
        if deleted_ids:
            reply(event, f"已刪除 #{', #'.join(deleted_ids)}")
        else:
            reply(event, "找不到對應的記錄")

    elif action == "edit":
        tx_id = result.get("id")
        updates = result.get("updates", {})
        if tx_id:
            handle_record_edit(event, user_id, tx_id, updates)
        else:
            reply(event, "請指定要修改的記錄編號，例如：修改#5 金額改成300")

    elif action == "delete_all":
        request_confirm(event, user_id, "record", "delete_all", {}, "刪除全部記帳記錄")

    elif action == "delete_by_date":
        d = result.get("date", today_tw().isoformat())
        request_confirm(event, user_id, "record", "delete_by_date", {"date": d}, f"刪除 {d} 的記錄")

    elif action == "delete_by_month":
        m = result.get("month", today_tw().strftime("%Y-%m"))
        request_confirm(event, user_id, "record", "delete_by_month", month_range(m), f"刪除 {m} 的記錄")

    else:
        reply(event, "無法辨識，請輸入消費或收入內容，或輸入「說明」查看指令")


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
    today = today_tw()
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


def handle_record_edit(event, user_id: str, tx_id: int, updates: dict):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM transactions WHERE id = %s AND user_id = %s", (tx_id, user_id))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        reply(event, f"找不到記錄 #{tx_id}")
        return

    allowed = {"type", "category", "amount", "description", "tx_date"}
    fields = {k: v for k, v in updates.items() if k in allowed and v is not None}
    if not fields:
        cur.close()
        conn.close()
        reply(event, "沒有需要修改的內容")
        return

    set_clause = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [tx_id, user_id]
    cur.execute(f"UPDATE transactions SET {set_clause} WHERE id = %s AND user_id = %s", values)
    conn.commit()

    cur.execute("SELECT * FROM transactions WHERE id = %s AND user_id = %s", (tx_id, user_id))
    updated = cur.fetchone()
    cur.close()
    conn.close()

    emoji = CATEGORY_EMOJI_RECORD.get(updated["category"], "")
    field_names = {"type": "類型", "category": "分類", "amount": "金額", "description": "描述", "tx_date": "日期"}
    changed = "、".join(field_names.get(k, k) for k in fields)
    reply(event, (
        f"已修改 #{tx_id}（{changed}）\n\n"
        f"{emoji} {updated['category']}｜{updated['type']}\n"
        f"${updated['amount']:,}\n"
        f"{updated['tx_date']}\n"
        f"{updated['description']}"
    ))

# ── Help messages ──

HELP_NOTE = (
    "Note 模式指令：\n\n"
    "直接輸入 AI 自動建立待辦\n"
    "今天 → 查看今日待辦\n"
    "本週 → 查看近期未完成\n"
    "完成 [id] → 標記完成\n"
    "修改#[id] 內容 → 修改待辦\n"
    "刪 [id] → 刪除待辦\n"
    "清除完成 → 清空已完成項目\n"
    "刪除全部/今天/本月 → 批次刪除"
)

HELP_RECORD = (
    "Record 模式指令：\n\n"
    "直接輸入 AI 自動記帳\n"
    "帳戶 → 查看收支總覽\n"
    "本月 → 查看本月報表\n"
    "明細 → 查看最近 10 筆\n"
    "修改#[id] 內容 → 修改記錄\n"
    "刪 [id] → 刪除記錄\n"
    "刪除全部/今天/本月 → 批次刪除"
)

HELP_CHAT = (
    "Chat 模式：\n\n"
    "直接輸入任何問題，AI 為你解答\n"
    "支援多輪對話，記得上下文\n"
    "切換模式時自動清空對話紀錄"
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

    # ── Confirmation handling ──
    if user_id in pending_confirmations:
        if text == "確認":
            execute_pending(event, user_id)
            return
        if text in ("取消", "算了"):
            pending_confirmations.pop(user_id)
            reply(event, "已取消")
            return
        pending_confirmations.pop(user_id)

    # ── Global commands: mode switching ──
    if text in ("#note", "待辦", "待辦模式"):
        set_user_mode(user_id, "note")
        chat_histories.pop(user_id, None)
        reply(event, f"已切換至 Note 模式\n\n{HELP_NOTE}")
        return
    if text in ("#record", "記帳", "記帳模式"):
        set_user_mode(user_id, "record")
        chat_histories.pop(user_id, None)
        reply(event, f"已切換至 Record 模式\n\n{HELP_RECORD}")
        return
    if text in ("#chat", "碎碎念", "碎碎念模式"):
        set_user_mode(user_id, "chat")
        chat_histories.pop(user_id, None)
        reply(event, f"已切換至 Chat 模式\n\n{HELP_CHAT}")
        return
    if text in ("#mode", "查看指令", "說明", "模式"):
        mark = {mode: " ← 目前"}
        reply(event, (
            f"── 待辦（#note）{mark.get('note', '')} ──\n"
            f"{HELP_NOTE}\n\n"
            f"── 記帳（#record）{mark.get('record', '')} ──\n"
            f"{HELP_RECORD}\n\n"
            f"── 碎碎念（#chat）{mark.get('chat', '')} ──\n"
            f"{HELP_CHAT}\n\n"
            "── 每日推送 ──\n"
            "設定推送 HH:MM → 開啟每日總結\n"
            "取消推送 → 關閉推送\n"
            "推送狀態 → 查看目前設定\n\n"
            "切換模式：待辦 / 記帳 / 碎碎念"
        ))
        return

    # ── Push time commands ──
    push_match = re.match(r"(?:設定推送|推送時間)\s*(\d{1,2})[：:](\d{2})", text)
    if push_match:
        h, m = int(push_match.group(1)), int(push_match.group(2))
        if 0 <= h <= 23 and 0 <= m <= 59:
            time_str = f"{h:02d}:{m:02d}"
            set_push_time(user_id, time_str)
            reply(event, f"已設定每日推送時間：{time_str}\n每天 {time_str} 會收到待辦與記帳總結\n\n輸入「取消推送」可關閉")
        else:
            reply(event, "時間格式不正確，請輸入 00:00~23:59\n例如：設定推送 21:00")
        return
    if text == "取消推送":
        clear_push_time(user_id)
        reply(event, "已取消每日推送")
        return
    if text == "推送狀態":
        pt = get_push_time(user_id)
        if pt:
            reply(event, f"目前推送時間：{pt.strftime('%H:%M')}\n\n輸入「取消推送」可關閉")
        else:
            reply(event, "目前未設定推送\n\n輸入「設定推送 21:00」開啟每日總結")
        return

    # ── Exact keyword shortcuts ──
    if mode == "note":
        if text in ("今天", "今日"):
            handle_note_today(event, user_id)
            return
        if text in ("本週", "這週"):
            handle_note_week(event, user_id)
            return
        if text == "清除完成":
            handle_note_clear_done(event, user_id)
            return
        if text in ("刪除全部", "清空全部"):
            request_confirm(event, user_id, "note", "delete_all", {}, "刪除全部待辦")
            return
        if text in ("刪除今天", "刪除今日"):
            request_confirm(event, user_id, "note", "delete_by_date",
                          {"date": today_tw().isoformat()}, f"刪除 {today_tw()} 的待辦")
            return
        if text == "刪除本月":
            request_confirm(event, user_id, "note", "delete_by_month",
                          month_range(today_tw().strftime("%Y-%m")),
                          f"刪除 {today_tw().strftime('%Y/%m')} 的待辦")
            return

    if mode == "record":
        if text in ("帳戶", "餘額", "總覽"):
            handle_record_balance(event, user_id)
            return
        if text in ("本月", "月報", "本月報表"):
            handle_record_monthly(event, user_id)
            return
        if text in ("明細", "紀錄", "最近") or ("最近" in text and "筆" in text):
            handle_record_recent(event, user_id)
            return
        if text in ("刪除全部", "清空全部"):
            request_confirm(event, user_id, "record", "delete_all", {}, "刪除全部記帳記錄")
            return
        if text in ("刪除今天", "刪除今日"):
            request_confirm(event, user_id, "record", "delete_by_date",
                          {"date": today_tw().isoformat()}, f"刪除 {today_tw()} 的記錄")
            return
        if text == "刪除本月":
            request_confirm(event, user_id, "record", "delete_by_month",
                          month_range(today_tw().strftime("%Y-%m")),
                          f"刪除 {today_tw().strftime('%Y/%m')} 的記錄")
            return

    # ── Everything else → AI agent ──
    if mode == "note":
        handle_note_natural(event, user_id, text)
    elif mode == "record":
        handle_record_natural(event, user_id, text)
    else:
        handle_chat_message(event, user_id, text)


# ── Chat mode handler ──

MAX_CHAT_HISTORY = 10

def handle_chat_message(event, user_id: str, text: str):
    if user_id not in chat_histories:
        chat_histories[user_id] = []

    history = chat_histories[user_id]
    response = agent_chat(text, history)

    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": response})

    if len(history) > MAX_CHAT_HISTORY:
        chat_histories[user_id] = history[-MAX_CHAT_HISTORY:]

    reply(event, response)

# ── Daily summary push ──

def generate_daily_summary(user_id: str) -> str | None:
    today = today_tw()
    today_str = today.strftime("%m/%d")
    conn = get_conn()
    cur = get_cursor(conn)

    # Todos for today
    cur.execute(
        f"SELECT * FROM todos WHERE user_id = %s AND due_date = %s "
        f"ORDER BY done, {PRIORITY_ORDER}, due_time NULLS LAST",
        (user_id, today)
    )
    todos = cur.fetchall()

    # Transactions for today
    cur.execute(
        "SELECT * FROM transactions WHERE user_id = %s AND tx_date = %s "
        "ORDER BY created_at",
        (user_id, today)
    )
    txs = cur.fetchall()
    cur.close()
    conn.close()

    if not todos and not txs:
        return None

    lines = [f"今日總結（{today_str}）\n"]

    # Todo section
    if todos:
        undone = [r for r in todos if not r["done"]]
        done = [r for r in todos if r["done"]]
        lines.append("── 待辦事項 ──")
        if undone:
            lines.append(f"未完成：{len(undone)} 項")
            for r in undone:
                p = PRIORITY_EMOJI.get(r["priority"], "")
                c = CATEGORY_EMOJI_NOTE.get(r["category"], "")
                time_str = f" {r['due_time'].strftime('%H:%M')}" if r.get("due_time") else ""
                lines.append(f"  {c}{p} #{r['id']} {r['title']}{time_str}")
        if done:
            lines.append(f"已完成：{len(done)} 項")

    # Transaction section
    if txs:
        if todos:
            lines.append("")
        lines.append("── 今日收支 ──")
        expense_total = 0
        income_total = 0
        expense_lines = []
        income_lines = []
        for r in txs:
            emoji = CATEGORY_EMOJI_RECORD.get(r["category"], "")
            if r["type"] == "支出":
                expense_total += r["amount"]
                expense_lines.append(f"  {emoji} {r['description']} ${r['amount']:,}")
            else:
                income_total += r["amount"]
                income_lines.append(f"  {emoji} {r['description']} ${r['amount']:,}")
        if expense_lines:
            lines.append(f"支出 {len(expense_lines)} 筆：${expense_total:,}")
            lines.extend(expense_lines)
        if income_lines:
            lines.append(f"收入 {len(income_lines)} 筆：${income_total:,}")
            lines.extend(income_lines)

    return "\n".join(lines)


@app.route("/cron/daily-summary", methods=["POST", "GET"])
def cron_daily_summary():
    # Verify cron secret
    secret = os.environ.get("CRON_SECRET", "")
    provided = request.headers.get("X-Cron-Secret") or request.args.get("secret", "")
    if not secret or provided != secret:
        abort(403)

    now_tw = datetime.now(TZ_TW)
    current_hour = now_tw.hour
    current_minute = now_tw.minute

    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(
        "SELECT user_id, push_time FROM user_state WHERE push_time IS NOT NULL"
    )
    users = cur.fetchall()
    cur.close()
    conn.close()

    sent = 0
    for u in users:
        pt = u["push_time"]
        # Match within 30-minute window (for hourly cron)
        if pt.hour == current_hour and abs(pt.minute - current_minute) <= 30:
            summary = generate_daily_summary(u["user_id"])
            if summary:
                try:
                    push_message(u["user_id"], summary)
                    sent += 1
                except Exception as e:
                    print(f"[push error] user={u['user_id']}, error={e}")

    print(f"[cron] daily summary sent to {sent} users at {now_tw.strftime('%H:%M')}")
    return f"OK sent={sent}", 200


# ── App startup ──

print("[startup] Initializing database...")
init_db()
print("[startup] App ready")

if __name__ == "__main__":
    app.run(port=5000, debug=True)
