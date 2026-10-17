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
import json
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
                 params={"token": get_token(), "seller_id": SELLER_ID, "top": top,
                         "group": "false"}, timeout=20)  # без группировки по товару
    data = r.json()
    if data.get("retval") != 0:
        get_token(force=True)
        raise RuntimeError(f"seller-last-sales: {data}")
    return data.get("sales") or []


def purchase_info(invoice_id):
    """Подробности заказа. Если не получилось — не страшно, отправим то, что есть."""
    try:
        r = http.get(f"{API}/purchase/info/{invoice_id}", params={"token": get_token()}, timeout=20)
        data = r.json()
        if data.get("retval") == 0:
            return data.get("content") or {}
        log.warning("purchase/info %s: %s", invoice_id, data)
    except Exception as ex:
        log.warning("purchase/info %s: %s", invoice_id, ex)
    return {}


def send_tg(text):
    """Возвращает True, если сообщение дошло до Telegram."""
    try:
        r = http.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=20)
    except Exception as ex:
        log.error("Telegram: %s", ex)
        return False
    if not r.ok:
        log.error("Telegram: %s", r.text)
        return False
    log.info("Telegram: сообщение отправлено")
    return True


ID_FIELDS = ("invoice_id", "inv", "id_invoice", "invoiceId", "invoice", "id_i", "unique_code", "id")


def sale_inv(sale):
    """Номер заказа — Plati в разных режимах называет это поле по-разному."""
    for f in ID_FIELDS:
        v = sale.get(f)
        if v not in (None, "", 0):
            return v
    return None


def sale_key(sale):
    """Уникальный ключ продажи: номер заказа, а если его нет — вся запись целиком."""
    inv = sale_inv(sale)
    if inv is not None:
        return f"inv:{inv}"
    return "raw:" + json.dumps(sale, sort_keys=True, ensure_ascii=False)


def format_order(sale, info):
    e = lambda v: html.escape(str(v)) if v not in (None, "") else "—"
    product = sale.get("product") or {}
    name = info.get("name") or product.get("name")
    if info.get("amount"):
        amount, currency = info["amount"], info.get("currency_type") or ""
    else:
        amount, currency = product.get("price_rub") or product.get("price"), "RUB"
    market = MARKETS.get(info.get("owner"), "Plati.market")
    buyer = info.get("buyer_info") or {}

    lines = [
        f"🛒 <b>Новый заказ — {market}</b>",
        "",
        f"📦 <b>Товар:</b> {e(name)}",
        f"💰 <b>Сумма:</b> {e(amount)} {e(currency)}",
        f"🧾 <b>Заказ №:</b> <code>{e(sale_inv(sale))}</code>",
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
            start_sales = last_sales(50)
            seen = {sale_key(s) for s in start_sales}
            if start_sales:
                log.info("Пример продажи из API: %s", start_sales[0])
            break
        except Exception as ex:
            log.error("Старт: %s", ex)
            time.sleep(15)
    log.info("Запущен, уже известно продаж: %d", len(seen))
    send_tg(f"✅ Бот заказов Plati запущен. Уже известно продаж: {len(seen)}")

    # Проверка: если в Railway задать TEST_LAST_SALE=1, бот при старте
    # пришлёт последнюю существующую продажу, чтобы было видно, как выглядит заявка.
    if os.getenv("TEST_LAST_SALE") == "1":
        try:
            sales = last_sales(50)
            if sales:
                newest = max(sales, key=lambda s: str(s.get("date", "")))
                inv = sale_inv(newest)
                send_tg("🧪 ТЕСТ\n\n" + format_order(newest, purchase_info(inv) if inv else {}))
        except Exception as ex:
            log.error("Тест: %s", ex)

    while True:
        try:
            sales = last_sales()
            log.info("Проверка: продаж в ответе %d, новых %d",
                     len(sales), sum(1 for s in sales if sale_key(s) not in seen))
            for sale in sorted(sales, key=lambda s: str(s.get("date", ""))):  # от старых к новым
                key = sale_key(sale)
                if key in seen:
                    continue
                inv = sale_inv(sale)
                log.info("Новый заказ %s: %s", inv, sale)
                try:
                    text = format_order(sale, purchase_info(inv) if inv else {})
                except Exception as ex:
                    log.error("Формат заказа %s: %s", inv, ex)
                    text = f"🛒 <b>Новый заказ — Plati.market</b>\n🧾 Заказ №: <code>{inv}</code>"
                # Помечаем заказ обработанным, только если Telegram его принял,
                # иначе повторим на следующей проверке.
                if send_tg(text):
                    seen.add(key)
                    log.info("Отправлен заказ %s", inv)
            if len(seen) > 5000:
                seen = set(list(seen)[-1000:])
        except Exception as ex:
            log.error("Ошибка: %s", ex)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
