#!/usr/bin/env python3
"""

╔══════════════════════════════════════════════════════════════╗
║     AGENT TRADING IA v2 — Trading IA by fefe                ║
║     Prêt à lancer — toutes les clés configurées             ║
╚══════════════════════════════════════════════════════════════╝

INSTALLATION (Terminal Mac) :
  pip3 install requests anthropic

LANCEMENT :
  python3 trading_agent_fefe.py
"""

import requests
import threading
import time
import json
import logging
import sqlite3
import os
from datetime import datetime, time as dtime
from anthropic import Anthropic

# ════════════════════════════════════════════════════════════════
#  CONFIG — Tout est déjà configuré !
# ════════════════════════════════════════════════════════════════

import os

TWELVE_DATA_KEY  = os.environ.get("TWELVE_DATA_KEY",  "")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_KEY",    "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SCAN_INTERVAL_MIN  = 5
SCORE_THRESHOLD    = 68
CAPITAL_TOTAL      = 10000
RISK_PER_TRADE_PCT = 2
MARKET_OPEN_HOUR   = 8
MARKET_CLOSE_HOUR  = 22
ALERT_COOLDOWN_MIN = 60

# ════════════════════════════════════════════════════════════════
#  WATCHLIST
# ════════════════════════════════════════════════════════════════

WATCHLIST_FULL = [
    {"symbol": "SE",      "name": "Sea Limited",    "type": "US",      "currency": "$"},
    {"symbol": "SOUN",    "name": "SoundHound AI",  "type": "US",      "currency": "$"},
    {"symbol": "RIBER",   "name": "Riber",          "type": "EU",      "currency": "€"},
    {"symbol": "AL2SI",   "name": "2CRSi",          "type": "EU",      "currency": "€"},
    {"symbol": "SOI",     "name": "Soitec",         "type": "EU",      "currency": "€"},
    {"symbol": "GLD",     "name": "Or (ETF)",       "type": "Matière", "currency": "$"},
    {"symbol": "SLV",     "name": "Argent (ETF)",   "type": "Matière", "currency": "$"},
    {"symbol": "USO",     "name": "Pétrole (ETF)",  "type": "Énergie", "currency": "$"},
    {"symbol": "ZEC/USD", "name": "Zcash",          "type": "Crypto",  "currency": "$"},
    {"symbol": "SOL/USD", "name": "Solana",         "type": "Crypto",  "currency": "$"},
]

# Watchlist crypto uniquement pour le week-end
WATCHLIST_CRYPTO = [
    {"symbol": "ZEC/USD", "name": "Zcash",  "type": "Crypto", "currency": "$"},
    {"symbol": "SOL/USD", "name": "Solana", "type": "Crypto", "currency": "$"},
]

def is_weekend():
    """Retourne True si on est samedi (5) ou dimanche (6)."""
    return datetime.now().weekday() >= 5

def get_active_watchlist():
    """Retourne crypto seulement le week-end, tout le monde en semaine."""
    if is_weekend():
        log.info("📅 Week-end détecté → scan crypto uniquement (ZEC, SOL)")
        return WATCHLIST_CRYPTO
    return WATCHLIST

# ════════════════════════════════════════════════════════════════
#  SETUP
# ════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("trading_agent.log")
    ]
)
log = logging.getLogger(__name__)

anthropic_client = Anthropic(api_key=ANTHROPIC_KEY)
TD_BASE = "https://api.twelvedata.com"
TG_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
already_alerted = {}

# ════════════════════════════════════════════════════════════════
#  BASE DE DONNÉES
# ════════════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT,
            name            TEXT,
            signal          TEXT,
            entry           REAL,
            stop            REAL,
            target1         REAL,
            target2         REAL,
            rr              REAL,
            score           INTEGER,
            confiance       TEXT,
            analyse         TEXT,
            decision        TEXT DEFAULT 'pending',
            price_at_signal REAL,
            created_at      TEXT,
            decided_at      TEXT,
            message_id      INTEGER
        )
    """)
    conn.commit()
    conn.close()
    log.info("✅ Base de données OK")


def save_trade(symbol, name, signal_data, score, price, message_id):
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("""
        INSERT INTO trades
        (symbol, name, signal, entry, stop, target1, target2, rr,
         score, confiance, analyse, price_at_signal, created_at, message_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        symbol, name,
        signal_data.get("signal"),
        signal_data.get("entry"),
        signal_data.get("stop"),
        signal_data.get("target1"),
        signal_data.get("target2"),
        signal_data.get("rr"),
        score,
        signal_data.get("confiance"),
        signal_data.get("analyse"),
        price,
        datetime.now().isoformat(),
        message_id
    ))
    trade_id = c.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def update_trade_decision(message_id, decision):
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute(
        "UPDATE trades SET decision=?, decided_at=? WHERE message_id=?",
        (decision, datetime.now().isoformat(), message_id)
    )
    conn.commit()
    conn.close()


def get_trade_stats():
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN decision='pris'      THEN 1 ELSE 0 END),
            SUM(CASE WHEN decision='passe'     THEN 1 ELSE 0 END),
            SUM(CASE WHEN decision='surveille' THEN 1 ELSE 0 END),
            SUM(CASE WHEN decision='pending'   THEN 1 ELSE 0 END)
        FROM trades WHERE date(created_at) = date('now')
    """)
    row = c.fetchone()
    conn.close()
    return {"total": row[0] or 0, "pris": row[1] or 0,
            "passe": row[2] or 0, "surveille": row[3] or 0,
            "pending": row[4] or 0}


def get_recent_trades(limit=5):
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("""
        SELECT symbol, signal, entry, stop, target1, score, decision, created_at
        FROM trades ORDER BY created_at DESC LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return rows


# ════════════════════════════════════════════════════════════════
#  TWELVE DATA
# ════════════════════════════════════════════════════════════════

def td_get(endpoint, params):
    params["apikey"] = TWELVE_DATA_KEY
    try:
        r = requests.get(f"{TD_BASE}/{endpoint}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"TD erreur [{endpoint}] : {e}")
        return None


def get_quote(symbol):
    return td_get("quote", {"symbol": symbol})

def get_rsi(symbol):
    d = td_get("rsi", {"symbol": symbol, "interval": "1day",
                        "time_period": 14, "outputsize": 1})
    if d and "values" in d and d["values"]:
        return float(d["values"][0]["rsi"])
    return None

def get_macd(symbol):
    d = td_get("macd", {"symbol": symbol, "interval": "1day", "outputsize": 1})
    if d and "values" in d and d["values"]:
        v = d["values"][0]
        return float(v["macd"]), float(v["macd_signal"])
    return None, None

def get_ema(symbol, period):
    d = td_get("ema", {"symbol": symbol, "interval": "1day",
                        "time_period": period, "outputsize": 1})
    if d and "values" in d and d["values"]:
        return float(d["values"][0]["ema"])
    return None


# ════════════════════════════════════════════════════════════════
#  SCORING
# ════════════════════════════════════════════════════════════════

def compute_score(rsi, macd, macd_sig, ema20, ema50):
    pts = 0
    details = {}

    if rsi is not None:
        if rsi < 30:   p = 35; sig = "Oversold 🟢"
        elif rsi < 40: p = 28; sig = "Zone basse 🟡"
        elif rsi < 50: p = 18; sig = "Neutre bas 🟡"
        elif rsi < 60: p = 12; sig = "Neutre 🔘"
        elif rsi > 70: p = 5;  sig = "Overbought 🔴"
        else:          p = 10; sig = "Neutre haut 🔘"
        pts += p
        details["rsi"] = {"value": round(rsi, 1), "signal": sig}

    if macd is not None and macd_sig is not None:
        p = 35 if macd > macd_sig else 5
        sig = "Haussier 🟢" if macd > macd_sig else "Baissier 🔴"
        pts += p
        details["macd"] = {"value": round(macd, 3), "signal": sig}

    if ema20 is not None and ema50 is not None:
        p = 30 if ema20 > ema50 else 5
        sig = "Tendance haussière 🟢" if ema20 > ema50 else "Tendance baissière 🔴"
        pts += p
        details["ema"] = {"ema20": round(ema20, 2),
                          "ema50": round(ema50, 2), "signal": sig}

    return min(pts, 100), details


# ════════════════════════════════════════════════════════════════
#  CLAUDE
# ════════════════════════════════════════════════════════════════

def claude_analyze(asset, price, chg, score, details):
    rsi_txt  = f"{details.get('rsi',{}).get('value','—')} ({details.get('rsi',{}).get('signal','—')})"
    macd_txt = f"{details.get('macd',{}).get('value','—')} ({details.get('macd',{}).get('signal','—')})"
    ema_txt  = (f"EMA20={details.get('ema',{}).get('ema20','—')} / "
                f"EMA50={details.get('ema',{}).get('ema50','—')} "
                f"({details.get('ema',{}).get('signal','—')})")

    prompt = f"""Tu es un expert en swing trading. Analyse cet actif et génère un signal précis.

Actif : {asset['name']} ({asset['symbol']}) · {asset['type']}
Prix : {asset['currency']}{price:.2f} · Variation 24h : {chg:+.2f}%
Score technique : {score}/100
RSI(14) : {rsi_txt}
MACD : {macd_txt}
{ema_txt}
Capital : {CAPITAL_TOTAL}{asset['currency']} · Risque/trade : {RISK_PER_TRADE_PCT}%

Réponds UNIQUEMENT avec ce JSON (rien d'autre) :
{{
  "signal": "BUY" ou "SELL" ou "WAIT",
  "entry": <prix>,
  "stop": <stop-loss>,
  "target1": <target 1>,
  "target2": <target 2>,
  "rr": <ratio>,
  "confiance": "faible" ou "moyen" ou "fort",
  "analyse": "<2 phrases max>"
}}"""

    try:
        resp = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        text = resp.content[0].text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        log.error(f"Claude erreur : {e}")
        return None


# ════════════════════════════════════════════════════════════════
#  TELEGRAM
# ════════════════════════════════════════════════════════════════

def tg_post(endpoint, payload):
    try:
        r = requests.post(f"{TG_BASE}/{endpoint}", json=payload, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Telegram erreur : {e}")
        return None


def send_signal_with_buttons(asset, price, chg, score, signal_data):
    cur    = asset["currency"]
    sig    = signal_data["signal"]
    emoji  = {"BUY": "🟢", "SELL": "🔴", "WAIT": "⏳"}.get(sig, "⏳")
    action = {"BUY": "ACHAT", "SELL": "VENTE", "WAIT": "ATTENDRE"}.get(sig, "SIGNAL")
    c_emo  = {"fort": "💪", "moyen": "👍", "faible": "⚠️"}.get(
               signal_data.get("confiance", "moyen"), "👍")

    entry = float(signal_data.get("entry", price))
    stop  = float(signal_data.get("stop",  price))
    t1    = float(signal_data.get("target1", price))
    t2    = float(signal_data.get("target2", price))

    risk_amt   = CAPITAL_TOTAL * (RISK_PER_TRADE_PCT / 100)
    risk_share = abs(entry - stop)
    nb_shares  = int(risk_amt / risk_share) if risk_share > 0 else 0
    stop_pct   = (stop - entry) / entry * 100
    t1_pct     = (t1   - entry) / entry * 100
    t2_pct     = (t2   - entry) / entry * 100

    text = (
        f"{emoji} <b>{asset['name']} ({asset['symbol']})</b> — {action}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Prix : <b>{cur}{price:.2f}</b> ({chg:+.2f}%)\n"
        f"📊 Score : <b>{score}/100</b>\n\n"
        f"📌 <b>PLAN DE TRADE</b>\n"
        f"  • Entry    : <b>{cur}{entry:.2f}</b>\n"
        f"  • Stop     : <b>{cur}{stop:.2f}</b> ({stop_pct:.1f}%)\n"
        f"  • Target 1 : <b>{cur}{t1:.2f}</b> (+{t1_pct:.1f}%)\n"
        f"  • Target 2 : <b>{cur}{t2:.2f}</b> (+{t2_pct:.1f}%)\n"
        f"  • R/R      : <b>{signal_data.get('rr','—')}:1</b>\n\n"
        f"👛 Position : <b>~{nb_shares} titres</b> "
        f"(risque {RISK_PER_TRADE_PCT}% = {cur}{risk_amt:.0f})\n\n"
        f"{c_emo} Confiance : <b>{signal_data.get('confiance','—')}</b>\n"
        f"💬 {signal_data.get('analyse','')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )

    keyboard = {"inline_keyboard": [[
        {"text": "✅ Je prends",    "callback_data": f"pris|{asset['symbol']}"},
        {"text": "❌ Je passe",     "callback_data": f"passe|{asset['symbol']}"},
        {"text": "⏳ Je surveille", "callback_data": f"surveille|{asset['symbol']}"},
    ]]}

    resp = tg_post("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": keyboard
    })

    if resp and resp.get("ok"):
        msg_id = resp["result"]["message_id"]
        log.info(f"✅ Signal envoyé (msg_id={msg_id})")
        return msg_id
    return None


def answer_callback(cb_id, text):
    tg_post("answerCallbackQuery", {
        "callback_query_id": cb_id,
        "text": text,
        "show_alert": False
    })


def edit_after_decision(message_id, original, decision):
    emoji = {"pris": "✅", "passe": "❌", "surveille": "⏳"}.get(decision, "—")
    label = {"pris": "PRIS", "passe": "PASSÉ", "surveille": "SURVEILLÉ"}.get(decision, decision)
    new_text = f"{original}\n\n{emoji} <b>Décision : {label}</b> — {datetime.now().strftime('%H:%M')}"
    tg_post("editMessageText", {
        "chat_id": TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text": new_text,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": []}
    })


def send_simple(text):
    tg_post("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    })


def send_startup():
    symbols = ", ".join([a["symbol"] for a in WATCHLIST_FULL])
    mode = "🌙 Mode week-end — Crypto uniquement" if is_weekend() else "📈 Mode semaine — Tous les actifs"
    send_simple(
        f"🤖 <b>Trading IA by fefe — Démarré !</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 Watchlist complète : {symbols}\n"
        f"⏱ Scan toutes les {SCAN_INTERVAL_MIN} min\n"
        f"🎯 Seuil d'alerte : {SCORE_THRESHOLD}/100\n"
        f"💰 Capital : {CAPITAL_TOTAL}€ · Risque : {RISK_PER_TRADE_PCT}%\n"
        f"📅 {mode}\n"
        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"En attente de signaux... 👀\n\n"
        f"Commandes : /stats /watchlist /status /aide"
    )


def send_daily_report():
    stats  = get_trade_stats()
    trades = get_recent_trades(5)
    recent = ""
    for t in trades:
        sym, sig, entry, stop, t1, score, dec, created = t
        de = {"pris":"✅","passe":"❌","surveille":"⏳","pending":"❓"}.get(dec,"❓")
        se = {"BUY":"🟢","SELL":"🔴","WAIT":"⏳"}.get(sig,"—")
        recent += f"  {de} {se} {sym} | Entry:{entry} | Score:{score}\n"
    send_simple(
        f"📊 <b>Rapport — {datetime.now().strftime('%d/%m/%Y')}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Signaux envoyés : <b>{stats['total']}</b>\n"
        f"✅ Pris       : <b>{stats['pris']}</b>\n"
        f"❌ Passés     : <b>{stats['passe']}</b>\n"
        f"⏳ Surveillés : <b>{stats['surveille']}</b>\n"
        f"❓ En attente : <b>{stats['pending']}</b>\n\n"
        f"📋 <b>Derniers signaux :</b>\n"
        f"{recent if recent else '  Aucun signal aujourd\'hui'}"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


# ════════════════════════════════════════════════════════════════
#  POLLING TELEGRAM — Écoute les boutons et commandes
# ════════════════════════════════════════════════════════════════

def telegram_polling():
    offset = 0
    log.info("📡 Polling Telegram démarré...")

    while True:
        try:
            r = requests.get(
                f"{TG_BASE}/getUpdates",
                params={"timeout": 30, "offset": offset},
                timeout=35
            )
            updates = r.json().get("result", [])

            for upd in updates:
                offset = upd["update_id"] + 1

                # ── Bouton pressé ──────────────────────────────
                if "callback_query" in upd:
                    cb         = upd["callback_query"]
                    cb_id      = cb["id"]
                    cb_data    = cb["data"]
                    message    = cb["message"]
                    message_id = message["message_id"]
                    original   = message.get("text", "")

                    parts    = cb_data.split("|")
                    decision = parts[0]
                    symbol   = parts[1] if len(parts) > 1 else "?"

                    labels = {"pris": "✅ Trade pris !",
                              "passe": "❌ Ignoré",
                              "surveille": "⏳ En surveillance"}
                    answer_callback(cb_id, labels.get(decision, decision))
                    edit_after_decision(message_id, original, decision)
                    update_trade_decision(message_id, decision)
                    log.info(f"Décision : {decision} sur {symbol}")

                    if decision == "pris":
                        send_simple(
                            f"✅ <b>Trade {symbol} enregistré !</b>\n\n"
                            f"👉 <b>À faire sur XTB maintenant :</b>\n"
                            f"  1. Ouvre l'app XTB\n"
                            f"  2. Cherche <b>{symbol}</b>\n"
                            f"  3. Place l'ordre avec le stop et le target\n\n"
                            f"Bonne chance ! 💪📈"
                        )
                    elif decision == "surveille":
                        send_simple(
                            f"⏳ <b>{symbol} en surveillance</b>\n"
                            f"Je continue à monitorer cet actif.\n"
                            f"Tu seras alerté si le setup évolue."
                        )

                # ── Commande texte ─────────────────────────────
                elif "message" in upd:
                    msg  = upd["message"]
                    text = msg.get("text", "")

                    if text == "/stats":
                        send_daily_report()

                    elif text == "/watchlist":
                        active = get_active_watchlist()
                        mode = "🌙 Week-end — Crypto only" if is_weekend() else "📈 Semaine — Tous les actifs"
                        syms = "\n".join(
                            [f"  • {a['symbol']} — {a['name']}" for a in active])
                        send_simple(f"📋 <b>Watchlist active</b> ({mode})\n{syms}")

                    elif text == "/status":
                        stats = get_trade_stats()
                        send_simple(
                            f"🤖 <b>Agent actif</b>\n"
                            f"Signaux aujourd'hui : {stats['total']}\n"
                            f"Pris : {stats['pris']} | Passés : {stats['passe']}\n"
                            f"Prochain scan dans quelques minutes..."
                        )

                    elif text in ("/aide", "/help", "/start"):
                        send_simple(
                            f"🤖 <b>Trading IA by fefe — Commandes</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"/stats      → Rapport du jour\n"
                            f"/watchlist  → Actifs surveillés\n"
                            f"/status     → État de l'agent\n"
                            f"/aide       → Ce message\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"Les signaux arrivent automatiquement\n"
                            f"avec des boutons pour répondre 👇"
                        )

        except Exception as e:
            log.error(f"Polling erreur : {e}")
            time.sleep(5)


# ════════════════════════════════════════════════════════════════
#  RAPPORT JOURNALIER AUTO — 18h00
# ════════════════════════════════════════════════════════════════

def daily_report_scheduler():
    while True:
        now = datetime.now()
        if now.hour == 18 and now.minute == 0:
            log.info("📊 Rapport journalier...")
            send_daily_report()
            time.sleep(61)
        time.sleep(30)


# ════════════════════════════════════════════════════════════════
#  SCAN PRINCIPAL
# ════════════════════════════════════════════════════════════════

def can_alert(symbol):
    if symbol not in already_alerted:
        return True
    elapsed = (datetime.now() - already_alerted[symbol]).total_seconds() / 60
    return elapsed >= ALERT_COOLDOWN_MIN


def is_trading_hours():
    now = datetime.now().time()
    return dtime(MARKET_OPEN_HOUR, 0) <= now <= dtime(MARKET_CLOSE_HOUR, 0)


def scan_asset(asset):
    symbol = asset["symbol"]
    log.info(f"Scan {symbol}...")

    quote = get_quote(symbol)
    if not quote or "close" not in quote:
        log.warning(f"{symbol} : pas de données (marché fermé ?)")
        return

    price = float(quote["close"])
    chg   = float(quote.get("percent_change", 0))

    rsi            = get_rsi(symbol)
    macd, macd_sig = get_macd(symbol)
    ema20          = get_ema(symbol, 20)
    ema50          = get_ema(symbol, 50)

    score, details = compute_score(rsi, macd, macd_sig, ema20, ema50)

    log.info(f"{symbol} | {asset['currency']}{price:.2f} ({chg:+.2f}%) | Score: {score}/100")

    if score >= SCORE_THRESHOLD and can_alert(symbol):
        log.info(f"🎯 {symbol} score {score} ≥ {SCORE_THRESHOLD} → Claude analyse...")
        signal = claude_analyze(asset, price, chg, score, details)

        if signal and signal.get("signal") in ("BUY", "SELL"):
            msg_id = send_signal_with_buttons(asset, price, chg, score, signal)
            if msg_id:
                save_trade(symbol, asset["name"], signal, score, price, msg_id)
                already_alerted[symbol] = datetime.now()
                log.info(f"✅ {signal['signal']} envoyé pour {symbol}")
        else:
            log.info(f"{symbol} : Claude dit WAIT")


def run_scan_cycle():
    log.info(f"{'═'*50}")
    log.info(f"Scan — {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    log.info(f"{'═'*50}")
    active = get_active_watchlist()
    log.info(f"📋 {len(active)} actifs à scanner {'(mode week-end 🌙)' if is_weekend() else '(mode semaine 📈)'}")
    for asset in active:
        scan_asset(asset)
        time.sleep(1.5)


# ════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════

def main():
    log.info("🚀 Trading IA by fefe — Démarrage")
    log.info(f"📋 {len(WATCHLIST_FULL)} actifs | Seuil: {SCORE_THRESHOLD} | Scan: {SCAN_INTERVAL_MIN}min")

    init_db()

    # Thread polling Telegram
    threading.Thread(target=telegram_polling, daemon=True).start()

    # Thread rapport journalier
    threading.Thread(target=daily_report_scheduler, daemon=True).start()

    # Message de démarrage
    send_startup()

    # Boucle principale
    while True:
        try:
            if is_trading_hours():
                run_scan_cycle()
            else:
                log.info(f"⏸ Hors heures ({MARKET_OPEN_HOUR}h-{MARKET_CLOSE_HOUR}h) — pause")

            log.info(f"💤 Prochain scan dans {SCAN_INTERVAL_MIN} min...")
            time.sleep(SCAN_INTERVAL_MIN * 60)

        except KeyboardInterrupt:
            log.info("⛔ Agent arrêté")
            send_simple("⛔ <b>Trading IA by fefe — Arrêté</b>")
            break
        except Exception as e:
            log.error(f"Erreur : {e}")
            send_simple(f"⚠️ <b>Erreur agent</b> : {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
