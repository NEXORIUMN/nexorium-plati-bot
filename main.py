"""
Plati.market (Digiseller) -> Telegram: уведомления о новых заказах.

Переменные окружения:
  DIGI_SELLER_ID   — ID продавца (my.digiseller.com -> Мои данные)
  DIGI_API_KEY     — API-ключ (my.digiseller.com -> Настройки -> API)
  TG_BOT_TOKEN     — токен Telegram-бота (можно тот же, что у бота заказов GGSel)
  TG_CHAT_ID       — ID группы, куда слать заявки (например -100xxxxxxxxxx)
  POLL_INTERVAL    — как часто проверять, в секундах (по умолчанию 30)
"""

import hashlib
import html
import logging
import os
import time

import requests

API = "https://api.digiseller.ru/api"
SELLER_ID = int(os.environ["DIGI_SELLER_ID"])
API_KEY = os.environ["DIGI_API_KEY"]
TG_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT = os.environ["TG_CHAT_ID"]
POLL = int(os.getenv("POLL_INTERVAL", "30"))

MARKETS = {1: "Plati.market", 1271: "GGSel", 9295: "WMCenter"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("plati-bot")
http = requests.Session()
http.headers["Accept"] = "application/json"

_token = None
_token_time = 0.0


def get_token(force=False):
    """Токен живёт ~2 часа, обновляем раз в 90 минут или при ошибке."""
    global _token, _token_time
    if _token and not force and time.time() - _token_time < 90 * 60:
        return _token
    ts = int(time.time())
    sign = hashlib.sha256(f"{API_KEY}{ts}".encode()).hexdigest()
    r = http.post(f"{API}/apilogin", json={"seller_id": SELLER_ID, "timestamp": ts, "sign": sign}, timeout=20)
    data = r.json()
    if data.get("retval") != 0:
        raise RuntimeError(f"apilogin: {data}")
    _token, _token_time = data["token"], time.time()
    log.info("Токен обновлён")
    return _token


def last_sales(top=20):
    r = http.get(f"{API}/seller-last-sales",
                 params={"token": get_token(), "seller_id": SELLER_ID, "top": top}, timeout=20)
    data = r.json()
    if data.get("retval") != 0:
        get_token(force=True)
        raise RuntimeError(f"seller-last-sales: {data}")
    return data.get("sales") or []


def purchase_info(invoice_id):
    r = http.get(f"{API}/purchase/info/{invoice_id}", params={"token": get_token()}, timeout=20)
    data = r.json()
    return data.get("content") or {} if data.get("retval") == 0 else {}


def send_tg(text):
    r = http.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                        "disable_web_page_preview": True}, timeout=20)
    if not r.ok:
        log.error("Telegram: %s", r.text)
    else:
        log.info("Telegram: сообщение отправлено")


def format_order(sale, info):
    e = lambda v: html.escape(str(v)) if v not in (None, "") else "—"
    product = sale.get("product") or {}
    name = info.get("name") or product.get("name")
    amount = info.get("amount") or product.get("price")
    currency = info.get("currency_type") or product.get("currency") or ""
    market = MARKETS.get(info.get("owner"), "Plati.market")
    buyer = info.get("buyer_info") or {}

    lines = [
        f"🛒 <b>Новый заказ — {market}</b>",
        "",
        f"📦 <b>Товар:</b> {e(name)}",
        f"💰 <b>Сумма:</b> {e(amount)} {e(currency)}",
        f"🧾 <b>Заказ №:</b> <code>{e(sale.get('invoice_id'))}</code>",
        f"🕒 <b>Дата:</b> {e(info.get('date_pay') or sale.get('date'))}",
    ]
    if info.get("cnt_goods") and float(info["cnt_goods"]) > 1:
        lines.append(f"🔢 <b>Кол-во:</b> {e(info['cnt_goods'])}")
    if buyer.get("email"):
        lines.append(f"✉️ <b>Email:</b> {e(buyer['email'])}")

    # Параметры, которые покупатель заполнил при оплате (логин, ID аккаунта, регион и т.п.)
    opts = info.get("options") or []
    if opts:
        lines += ["", "📝 <b>Данные покупателя:</b>"]
        for o in opts:
            lines.append(f"• {e(o.get('name'))}: <code>{e(o.get('value') or o.get('user_data'))}</code>")

    lines += ["", f'🔗 <a href="https://my.digiseller.com/inside/sales.asp">Открыть в кабинете</a>']
    return "\n".join(lines)


def main():
    seen = set()
    # При старте помечаем уже существующие продажи как просмотренные,
    # чтобы после перезапуска на Railway не прилетели старые заказы.
    while True:
        try:
            seen = {s["invoice_id"] for s in last_sales(50)}
            break
        except Exception as ex:
            log.error("Старт: %s", ex)
            time.sleep(15)
    log.info("Запущен, уже известно продаж: %d", len(seen))
    send_tg(f"✅ Бот заказов Plati запущен. Уже известно продаж: {len(seen)}")

    while True:
        try:
            sales = last_sales()
            log.info("Проверка: продаж в ответе %d, новых %d",
                     len(sales), sum(1 for s in sales if s.get("invoice_id") not in seen))
            for sale in reversed(sales):  # от старых к новым
                inv = sale.get("invoice_id")
                if inv in seen:
                    continue
                seen.add(inv)
                send_tg(format_order(sale, purchase_info(inv)))
                log.info("Отправлен заказ %s", inv)
            if len(seen) > 5000:
                seen = set(list(seen)[-1000:])
        except Exception as ex:
            log.error("Ошибка: %s", ex)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
