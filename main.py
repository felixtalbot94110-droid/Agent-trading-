#!/usr/bin/env python3
# coding: utf-8
"""
AGENT TRADING IA - Multi-Timeframe (MTF)
Version de test en parallele du v1
Installation: pip3 install requests anthropic
Lancement: python3 trading_agent_mtf.py
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

# ================================================================
# CONFIG
# ================================================================

TWELVE_DATA_KEY  = "84321bec610b44f3a1d4c06905707ff6"
ANTHROPIC_KEY    = "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"
TELEGRAM_TOKEN   = "8667670061:AAFGSHGYzES8E5n3_K4Ip6UIaRBsEHTLsgo"
TELEGRAM_CHAT_ID = "7499134607"

SCAN_INTERVAL_MIN  = 15
SCORE_THRESHOLD    = 68
CAPITAL_TOTAL      = 10000
RISK_PER_TRADE_PCT = 2
MARKET_OPEN_HOUR   = 8
MARKET_CLOSE_HOUR  = 22
ALERT_COOLDOWN_MIN = 60

# Poids des timeframes
WEIGHT_1H  = 0.20
WEIGHT_4H  = 0.30
WEIGHT_1D  = 0.50

# ================================================================
# WATCHLIST
# ================================================================

WATCHLIST_FULL = [
    {"symbol": "SE",      "name": "Sea Limited",   "type": "US",      "currency": "$"},
    {"symbol": "SOUN",    "name": "SoundHound AI", "type": "US",      "currency": "$"},
    {"symbol": "RIBER",   "name": "Riber",         "type": "EU",      "currency": "e"},
    {"symbol": "AL2SI",   "name": "2CRSi",         "type": "EU",      "currency": "e"},
    {"symbol": "SOI",     "name": "Soitec",        "type": "EU",      "currency": "e"},
    {"symbol": "GLD",     "name": "Or ETF",        "type": "Matiere", "currency": "$"},
    {"symbol": "SLV",     "name": "Argent ETF",    "type": "Matiere", "currency": "$"},
    {"symbol": "USO",     "name": "Petrole ETF",   "type": "Energie", "currency": "$"},
    {"symbol": "ZEC/USD", "name": "Zcash",         "type": "Crypto",  "currency": "$"},
    {"symbol": "SOL/USD", "name": "Solana",        "type": "Crypto",  "currency": "$"},
]

WATCHLIST_CRYPTO = [
    {"symbol": "ZEC/USD", "name": "Zcash",  "type": "Crypto", "currency": "$"},
    {"symbol": "SOL/USD", "name": "Solana", "type": "Crypto", "currency": "$"},
]

def is_weekend():
    return datetime.now().weekday() >= 5

def get_active_watchlist():
    if is_weekend():
        log.info("[WEEKEND] Mode week-end -> Crypto uniquement")
        return WATCHLIST_CRYPTO
    return WATCHLIST_FULL

# ================================================================
# SETUP
# ================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("trading_agent_mtf.log")
    ]
)
log = logging.getLogger(__name__)

anthropic_client = Anthropic(api_key=ANTHROPIC_KEY)
TD_BASE = "https://api.twelvedata.com"
TG_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
already_alerted = {}

# ================================================================
# BASE DE DONNEES
# ================================================================

def init_db():
    conn = sqlite3.connect("trades_mtf.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT,
            name        TEXT,
            signal      TEXT,
            entry       REAL,
            stop        REAL,
            target1     REAL,
            target2     REAL,
            rr          REAL,
            score_1h    INTEGER,
            score_4h    INTEGER,
            score_1d    INTEGER,
            score_mtf   INTEGER,
            confiance   TEXT,
            analyse     TEXT,
            decision    TEXT DEFAULT 'pending',
            created_at  TEXT,
            message_id  INTEGER
        )
    """)
    conn.commit()
    conn.close()
    log.info("[OK] Base de donnees MTF OK")


def save_trade(symbol, name, signal_data, s1h, s4h, s1d, smtf, message_id):
    conn = sqlite3.connect("trades_mtf.db")
    c = conn.cursor()
    c.execute("""
        INSERT INTO trades
        (symbol, name, signal, entry, stop, target1, target2, rr,
         score_1h, score_4h, score_1d, score_mtf, confiance, analyse, created_at, message_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        symbol, name,
        signal_data.get("signal"),
        signal_data.get("entry"),
        signal_data.get("stop"),
        signal_data.get("target1"),
        signal_data.get("target2"),
        signal_data.get("rr"),
        s1h, s4h, s1d, smtf,
        signal_data.get("confiance"),
        signal_data.get("analyse"),
        datetime.now().isoformat(),
        message_id
    ))
    conn.commit()
    conn.close()


def update_decision(message_id, decision):
    conn = sqlite3.connect("trades_mtf.db")
    c = conn.cursor()
    c.execute("UPDATE trades SET decision=? WHERE message_id=?", (decision, message_id))
    conn.commit()
    conn.close()


def get_stats():
    conn = sqlite3.connect("trades_mtf.db")
    c = conn.cursor()
    c.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN decision='pris'      THEN 1 ELSE 0 END),
               SUM(CASE WHEN decision='passe'     THEN 1 ELSE 0 END),
               SUM(CASE WHEN decision='surveille' THEN 1 ELSE 0 END),
               SUM(CASE WHEN decision='pending'   THEN 1 ELSE 0 END)
        FROM trades WHERE date(created_at) = date('now')
    """)
    row = c.fetchone()
    conn.close()
    return {"total": row[0] or 0, "pris": row[1] or 0,
            "passe": row[2] or 0, "surveille": row[3] or 0, "pending": row[4] or 0}


# ================================================================
# TWELVE DATA
# ================================================================

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


def get_rsi(symbol, interval, period=14):
    d = td_get("rsi", {"symbol": symbol, "interval": interval,
                        "time_period": period, "outputsize": 1})
    if d and "values" in d and d["values"]:
        return float(d["values"][0]["rsi"])
    return None


def get_macd(symbol, interval):
    d = td_get("macd", {"symbol": symbol, "interval": interval, "outputsize": 1})
    if d and "values" in d and d["values"]:
        v = d["values"][0]
        return float(v["macd"]), float(v["macd_signal"])
    return None, None


def get_ema(symbol, interval, period):
    d = td_get("ema", {"symbol": symbol, "interval": interval,
                        "time_period": period, "outputsize": 1})
    if d and "values" in d and d["values"]:
        return float(d["values"][0]["ema"])
    return None


# ================================================================
# SCORING PAR TIMEFRAME
# ================================================================

def score_timeframe(symbol, interval):
    """
    Calcule le score technique sur un timeframe donne.
    Retourne (score 0-100, details dict)
    """
    rsi            = get_rsi(symbol, interval)
    macd, macd_sig = get_macd(symbol, interval)
    ema20          = get_ema(symbol, interval, 20)
    ema50          = get_ema(symbol, interval, 50)

    pts = 0
    details = {}

    # RSI (35 pts)
    if rsi is not None:
        if rsi < 30:   p = 35; sig = "Oversold"
        elif rsi < 40: p = 28; sig = "Zone basse"
        elif rsi < 50: p = 18; sig = "Neutre bas"
        elif rsi < 60: p = 12; sig = "Neutre"
        elif rsi > 70: p = 5;  sig = "Overbought"
        else:          p = 10; sig = "Neutre haut"
        pts += p
        details["rsi"] = {"value": round(rsi, 1), "signal": sig}

    # MACD (35 pts)
    if macd is not None and macd_sig is not None:
        p = 35 if macd > macd_sig else 5
        sig = "Haussier" if macd > macd_sig else "Baissier"
        pts += p
        details["macd"] = {"value": round(macd, 3), "signal": sig}

    # EMA (30 pts)
    if ema20 is not None and ema50 is not None:
        p = 30 if ema20 > ema50 else 5
        sig = "Haussier" if ema20 > ema50 else "Baissier"
        pts += p
        details["ema"] = {"ema20": round(ema20, 2), "ema50": round(ema50, 2), "signal": sig}

    return min(pts, 100), details


def compute_mtf_score(symbol):
    """
    Calcule les scores sur 1h, 4h et 1j.
    Retourne score MTF pondere et detail de chaque timeframe.
    """
    log.info(f"  MTF 1h...")
    s1h, d1h = score_timeframe(symbol, "1h")
    time.sleep(0.5)

    log.info(f"  MTF 4h...")
    s4h, d4h = score_timeframe(symbol, "4h")
    time.sleep(0.5)

    log.info(f"  MTF 1j...")
    s1d, d1d = score_timeframe(symbol, "1day")

    # Score pondere
    smtf = round(s1h * WEIGHT_1H + s4h * WEIGHT_4H + s1d * WEIGHT_1D)

    # Alignement des timeframes
    aligned = sum([
        1 if s1h >= SCORE_THRESHOLD else 0,
        1 if s4h >= SCORE_THRESHOLD else 0,
        1 if s1d >= SCORE_THRESHOLD else 0,
    ])

    return smtf, s1h, s4h, s1d, aligned, d1h, d4h, d1d


# ================================================================
# CLAUDE
# ================================================================

def claude_analyze(asset, price, chg, s1h, s4h, s1d, smtf, aligned, d1h, d4h, d1d):
    prompt = f"""Tu es un expert en swing trading multi-timeframe. Analyse cet actif.

Actif : {asset['name']} ({asset['symbol']}) - {asset['type']}
Prix : {asset['currency']}{price:.2f} - Variation 24h : {chg:+.2f}%

SCORES MULTI-TIMEFRAME :
Score 1h  : {s1h}/100 (poids 20%) - RSI {d1h.get('rsi',{}).get('value','?')} - MACD {d1h.get('macd',{}).get('signal','?')} - EMA {d1h.get('ema',{}).get('signal','?')}
Score 4h  : {s4h}/100 (poids 30%) - RSI {d4h.get('rsi',{}).get('value','?')} - MACD {d4h.get('macd',{}).get('signal','?')} - EMA {d4h.get('ema',{}).get('signal','?')}
Score 1j  : {s1d}/100 (poids 50%) - RSI {d1d.get('rsi',{}).get('value','?')} - MACD {d1d.get('macd',{}).get('signal','?')} - EMA {d1d.get('ema',{}).get('signal','?')}
Score MTF : {smtf}/100 (moyenne ponderee)
Timeframes alignes : {aligned}/3

Capital : {CAPITAL_TOTAL}{asset['currency']} - Risque/trade : {RISK_PER_TRADE_PCT}%

Reponds UNIQUEMENT avec ce JSON :
{{
  "signal": "BUY" ou "SELL" ou "WAIT",
  "entry": <prix>,
  "stop": <stop-loss>,
  "target1": <target 1>,
  "target2": <target 2>,
  "rr": <ratio>,
  "confiance": "faible" ou "moyen" ou "fort",
  "analyse": "<2 phrases : setup MTF + condition cle>"
}}"""

    try:
        resp = anthropic_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        text = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
        return json.loads(text)
    except Exception as e:
        log.error(f"Claude erreur : {e}")
        return None


# ================================================================
# TELEGRAM
# ================================================================

def tg_post(endpoint, payload):
    try:
        r = requests.post(f"{TG_BASE}/{endpoint}", json=payload, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"TG erreur : {e}")
        return None


def send_signal(asset, price, chg, s1h, s4h, s1d, smtf, aligned, signal_data):
    cur  = asset["currency"]
    sig  = signal_data["signal"]
    conf = signal_data.get("confiance", "moyen")

    sig_label  = {"BUY": "[BUY] ACHAT", "SELL": "[SELL] VENTE", "WAIT": "[WAIT] ATTENDRE"}.get(sig, sig)
    conf_label = {"fort": "[FORT]", "moyen": "[MOYEN]", "faible": "[FAIBLE]"}.get(conf, conf)

    entry = float(signal_data.get("entry", price)) or price
    stop  = float(signal_data.get("stop", price)) or price * 0.95
    t1    = float(signal_data.get("target1", price)) or price * 1.10
    t2    = float(signal_data.get("target2", price)) or price * 1.20

    risk_amt   = CAPITAL_TOTAL * (RISK_PER_TRADE_PCT / 100)
    risk_share = abs(entry - stop)
    nb_shares  = int(risk_amt / risk_share) if risk_share > 0 else 0
    stop_pct   = ((stop - entry) / entry * 100) if entry > 0 else 0
    t1_pct     = ((t1 - entry) / entry * 100) if entry > 0 else 0

    # Barre d'alignement visuelle
    bar = ""
    for tf, sc in [("1h", s1h), ("4h", s4h), ("1j", s1d)]:
        if sc >= SCORE_THRESHOLD:
            bar += f"[{tf}:OK:{sc}] "
        else:
            bar += f"[{tf}:--:{sc}] "

    text = (
        f"[MTF] {asset['name']} ({asset['symbol']}) - {sig_label}\n"
        f"----------------------------------------------\n"
        f"Prix : {cur}{price:.2f} ({chg:+.2f}%)\n\n"
        f"SCORES MULTI-TIMEFRAME :\n"
        f"{bar}\n"
        f"Score MTF : {smtf}/100 | Alignes : {aligned}/3\n\n"
        f"PLAN DE TRADE :\n"
        f"  Entry    : {cur}{entry:.2f}\n"
        f"  Stop     : {cur}{stop:.2f} ({stop_pct:.1f}%)\n"
        f"  Target 1 : {cur}{t1:.2f} (+{t1_pct:.1f}%)\n"
        f"  Target 2 : {cur}{t2:.2f}\n"
        f"  R/R      : {signal_data.get('rr','?')}:1\n\n"
        f"Position : ~{nb_shares} titres (risque {RISK_PER_TRADE_PCT}% = {cur}{risk_amt:.0f})\n\n"
        f"Confiance : {conf_label}\n"
        f"{signal_data.get('analyse','')}\n"
        f"----------------------------------------------\n"
        f"{datetime.now().strftime('%d/%m/%Y %H:%M')} | v2-MTF"
    )

    keyboard = {"inline_keyboard": [[
        {"text": "OK Je prends",    "callback_data": f"pris|{asset['symbol']}"},
        {"text": "NON Je passe",    "callback_data": f"passe|{asset['symbol']}"},
        {"text": "WATCH Surveille", "callback_data": f"surveille|{asset['symbol']}"},
    ]]}

    resp = tg_post("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": keyboard
    })

    if resp and resp.get("ok"):
        return resp["result"]["message_id"]
    return None


def answer_callback(cb_id, text):
    tg_post("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})


def edit_after_decision(message_id, original, decision):
    label = {"pris": "PRIS", "passe": "PASSE", "surveille": "SURVEILLE"}.get(decision, decision)
    tg_post("editMessageText", {
        "chat_id": TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text": f"{original}\n\nDecision : {label} - {datetime.now().strftime('%H:%M')}",
        "reply_markup": {"inline_keyboard": []}
    })


def send_simple(text):
    tg_post("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"})


def send_startup():
    syms = ", ".join([a["symbol"] for a in WATCHLIST_FULL])
    mode = "WEEKEND - Crypto only" if is_weekend() else "SEMAINE - Tous actifs"
    send_simple(
        f"[MTF] Trading IA v2 Multi-Timeframe - Demarre !\n"
        f"----------------------------------------------\n"
        f"Watchlist : {syms}\n"
        f"Scan toutes les {SCAN_INTERVAL_MIN} min\n"
        f"Seuil MTF : {SCORE_THRESHOLD}/100\n"
        f"Poids : 1h={int(WEIGHT_1H*100)}% | 4h={int(WEIGHT_4H*100)}% | 1j={int(WEIGHT_1D*100)}%\n"
        f"Capital : {CAPITAL_TOTAL}e | Risque : {RISK_PER_TRADE_PCT}%\n"
        f"Mode : {mode}\n"
        f"{datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
        f"----------------------------------------------\n"
        f"En attente de signaux MTF...\n"
        f"Commandes : /stats /watchlist /status /aide"
    )


def send_menu():
    """Envoie le menu principal avec boutons cliquables."""
    keyboard = {"inline_keyboard": [
        [
            {"text": "ZEC",    "callback_data": "analyse|ZEC"},
            {"text": "SOL",    "callback_data": "analyse|SOL"},
            {"text": "SEA",    "callback_data": "analyse|SE"},
            {"text": "SOUN",   "callback_data": "analyse|SOUN"},
        ],
        [
            {"text": "2CRSI",  "callback_data": "analyse|AL2SI"},
            {"text": "SOITEC", "callback_data": "analyse|SOI"},
            {"text": "RIBER",  "callback_data": "analyse|RIBER"},
            {"text": "GLD",    "callback_data": "analyse|GLD"},
        ],
        [
            {"text": "SILVER", "callback_data": "analyse|SLV"},
            {"text": "OIL",    "callback_data": "analyse|USO"},
            {"text": "Scores tous", "callback_data": "scores|all"},
            {"text": "Stats",  "callback_data": "stats|all"},
        ]
    ]}

    tg_post("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": "Que veux-tu analyser ? Clique directement :",
        "reply_markup": keyboard
    })


    stats = get_stats()
    send_simple(
        f"[MTF] Rapport journalier - {datetime.now().strftime('%d/%m/%Y')}\n"
        f"----------------------------------------------\n"
        f"Signaux envoyes : {stats['total']}\n"
        f"Pris      : {stats['pris']}\n"
        f"Passes    : {stats['passe']}\n"
        f"Surveilles: {stats['surveille']}\n"
        f"En attente: {stats['pending']}\n"
        f"----------------------------------------------"
    )


def handle_prix(ticker):
    """Repond au /prix TICKER."""
    send_simple(f"Recherche du prix pour {ticker}...")
    asset = find_asset(ticker)
    quote = get_quote(asset["symbol"])
    if not quote or "close" not in quote:
        send_simple(f"Impossible de recuperer le prix de {ticker}.")
        return
    price = float(quote["close"])
    chg   = float(quote.get("percent_change", 0))
    send_simple(
        f"Prix {ticker} :\n"
        f"  Cours : {asset['currency']}{price:.2f}\n"
        f"  Variation 24h : {chg:+.2f}%\n"
        f"{datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )


def handle_score(ticker):
    """Repond au /score TICKER."""
    send_simple(f"Calcul scores MTF pour {ticker}... (30-60 sec)")
    asset = find_asset(ticker)
    quote = get_quote(asset["symbol"])
    if not quote or "close" not in quote:
        send_simple(f"Impossible de recuperer les donnees de {ticker}.")
        return
    price = float(quote["close"])
    chg   = float(quote.get("percent_change", 0))
    smtf, s1h, s4h, s1d, aligned, d1h, d4h, d1d = compute_mtf_score(asset["symbol"])
    bar = ""
    for tf, sc in [("1h", s1h), ("4h", s4h), ("1j", s1d)]:
        status = "OK" if sc >= SCORE_THRESHOLD else "--"
        bar += f"  [{tf}] {sc}/100 {status}\n"
    verdict = "SETUP MATUR" if smtf >= SCORE_THRESHOLD and aligned >= 2 else "PAS ENCORE MATUR"
    send_simple(
        f"Scores MTF {ticker} :\n"
        f"Prix : {asset['currency']}{price:.2f} ({chg:+.2f}%)\n"
        f"{bar}"
        f"Score MTF : {smtf}/100\n"
        f"Alignes   : {aligned}/3\n"
        f"Verdict   : {verdict}\n"
        f"{datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )
    send_menu()


def handle_analyse(ticker):
    """Analyse complete MTF + signal Claude."""
    send_simple(f"Analyse MTF complete de {ticker} en cours... (30-60 sec)")
    asset = find_asset(ticker)
    quote = get_quote(asset["symbol"])
    if not quote or "close" not in quote:
        send_simple(f"Impossible de recuperer les donnees de {ticker}.\nVerifie le symbole.")
        send_menu()
        return
    price = float(quote["close"])
    chg   = float(quote.get("percent_change", 0))
    smtf, s1h, s4h, s1d, aligned, d1h, d4h, d1d = compute_mtf_score(asset["symbol"])
    send_simple(f"Scores MTF calcules. Analyse Claude en cours...")
    signal = claude_analyze(asset, price, chg, s1h, s4h, s1d, smtf, aligned, d1h, d4h, d1d)
    if not signal:
        send_simple(f"Erreur lors de l'analyse Claude pour {ticker}.")
        send_menu()
        return
    msg_id = send_signal(asset, price, chg, s1h, s4h, s1d, smtf, aligned, signal)
    if msg_id and signal.get("signal") in ("BUY", "SELL"):
        save_trade(asset["symbol"], asset["name"], signal, s1h, s4h, s1d, smtf, msg_id)
    send_menu()


# ================================================================
# POLLING TELEGRAM
# ================================================================

def telegram_polling():
    offset = 0
    log.info("[POLL] Polling MTF demarre...")

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

                if "callback_query" in upd:
                    cb         = upd["callback_query"]
                    cb_id      = cb["id"]
                    cb_data    = cb["data"]
                    message    = cb["message"]
                    message_id = message["message_id"]
                    original   = message.get("text", "")
                    parts      = cb_data.split("|")
                    action     = parts[0]
                    value      = parts[1] if len(parts) > 1 else ""

                    # Boutons du menu analyse
                    if action == "analyse":
                        answer_callback(cb_id, f"Analyse {value} en cours...")
                        threading.Thread(
                            target=handle_analyse,
                            args=(value,),
                            daemon=True
                        ).start()

                    # Bouton scores tous
                    elif action == "scores":
                        answer_callback(cb_id, "Calcul des scores...")
                        threading.Thread(
                            target=handle_scores_all,
                            daemon=True
                        ).start()

                    # Bouton stats
                    elif action == "stats":
                        answer_callback(cb_id, "Rapport en cours...")
                        send_daily_report()

                    # Boutons trade (pris/passe/surveille)
                    elif action in ("pris", "passe", "surveille"):
                        symbol   = value
                        decision = action
                        labels = {"pris": "Trade pris!", "passe": "Signal ignore", "surveille": "En surveillance"}
                        answer_callback(cb_id, labels.get(decision, decision))
                        edit_after_decision(message_id, original, decision)
                        update_decision(message_id, decision)

                        if decision == "pris":
                            send_simple(
                                f"Trade {symbol} enregistre!\n"
                                f"Ouvre XTB et place l'ordre avec stop + target."
                            )
                        # Renvoie le menu apres decision
                        send_menu()

                    else:
                        answer_callback(cb_id, "Commande inconnue")

                elif "message" in upd:
                    msg  = upd["message"]
                    text = msg.get("text", "")

                    if text == "/stats":
                        send_daily_report()
                    elif text == "/watchlist":
                        active = get_active_watchlist()
                        syms = "\n".join([f"  - {a['symbol']} ({a['name']})" for a in active])
                        send_simple(f"[MTF] Watchlist active :\n{syms}")
                    elif text == "/status":
                        stats = get_stats()
                        send_simple(
                            f"[MTF] Agent v2 actif\n"
                            f"Signaux aujourd'hui : {stats['total']}\n"
                            f"Pris : {stats['pris']} | Passes : {stats['passe']}"
                        )
                    elif text.startswith("/analyse ") or text.startswith("/analyze "):
                        # /analyse SEA ou /analyse ZEC/USD
                        parts_cmd = text.split(" ", 1)
                        if len(parts_cmd) > 1:
                            ticker = parts_cmd[1].strip().upper()
                            threading.Thread(
                                target=handle_analyse,
                                args=(ticker,),
                                daemon=True
                            ).start()
                        else:
                            send_simple(
                                "Usage : /analyse TICKER\n"
                                "Exemples :\n"
                                "  /analyse SEA\n"
                                "  /analyse ZEC/USD\n"
                                "  /analyse SOL/USD\n"
                                "  /analyse GLD"
                            )

                    elif text.startswith("/prix "):
                        ticker = text.split(" ", 1)[1].strip().upper()
                        threading.Thread(
                            target=handle_prix,
                            args=(ticker,),
                            daemon=True
                        ).start()

                    elif text.startswith("/score "):
                        ticker = text.split(" ", 1)[1].strip().upper()
                        threading.Thread(
                            target=handle_score,
                            args=(ticker,),
                            daemon=True
                        ).start()

                    elif text == "/menu":
                        send_menu()

                    elif text in ("/aide", "/help", "/start"):
                        send_simple(
                            f"[MTF] Trading IA v2 - Commandes :\n"
                            f"/stats          - Rapport du jour\n"
                            f"/watchlist      - Actifs surveilles\n"
                            f"/status         - Etat de l'agent\n"
                            f"/analyse TICKER - Analyse complete + signal\n"
                            f"/prix TICKER    - Prix actuel\n"
                            f"/score TICKER   - Scores MTF\n"
                            f"/aide           - Ce message\n\n"
                            f"Exemples :\n"
                            f"  /analyse SEA\n"
                            f"  /prix ZEC/USD\n"
                            f"  /score SOL/USD"
                        )

        except Exception as e:
            log.error(f"Polling erreur : {e}")
            time.sleep(5)


# ================================================================
# RAPPORT JOURNALIER AUTO
# ================================================================

def daily_report_scheduler():
    while True:
        now = datetime.now()
        if now.hour == 18 and now.minute == 0:
            send_daily_report()
            time.sleep(61)
        time.sleep(30)


# ================================================================
# COMMANDES A LA DEMANDE
# ================================================================

def find_asset(ticker):
    """Trouve un actif dans la watchlist par son ticker."""
    ticker = ticker.upper().strip()
    
    # Correspondances raccourcies
    shortcuts = {
        "ZEC": "ZEC/USD",
        "SOL": "SOL/USD",
        "BTC": "BTC/USD",
        "ETH": "ETH/USD",
        "SEA": "SE",
    }
    if ticker in shortcuts:
        ticker = shortcuts[ticker]
    
    for a in WATCHLIST_FULL:
        if a["symbol"].upper() == ticker:
            return a
        if a["name"].upper() == ticker:
            return a
        # Match partiel ex: "ZEC" dans "ZEC/USD"
        if ticker in a["symbol"].upper():
            return a
    
    # Si pas trouve, cree un actif generique
    log.warning(f"Actif {ticker} non trouve dans la watchlist - utilise tel quel")
    return {"symbol": ticker, "name": ticker, "type": "?", "currency": "$"}


def handle_prix(ticker):
    """Repond au /prix TICKER."""
    send_simple(f"Recherche du prix pour {ticker}...")
    asset = find_asset(ticker)
    quote = get_quote(asset["symbol"])
    if not quote or "close" not in quote:
        send_simple(f"Impossible de recuperer le prix de {ticker}.\nVerifie le symbole (ex: SEA, ZEC/USD, SOL/USD)")
        return
    price = float(quote["close"])
    chg   = float(quote.get("percent_change", 0))
    high  = quote.get("fifty_two_week", {}).get("high", "?")
    low   = quote.get("fifty_two_week", {}).get("low", "?")
    send_simple(
        f"Prix {ticker} :\n"
        f"----------------------------------------------\n"
        f"  Cours : {asset['currency']}{price:.2f}\n"
        f"  Variation 24h : {chg:+.2f}%\n"
        f"  52s haut : {asset['currency']}{high}\n"
        f"  52s bas  : {asset['currency']}{low}\n"
        f"{datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )


def handle_score(ticker):
    """Repond au /score TICKER - affiche les scores MTF."""
    send_simple(f"Calcul des scores MTF pour {ticker}... (30-60 sec)")
    asset = find_asset(ticker)
    quote = get_quote(asset["symbol"])
    if not quote or "close" not in quote:
        send_simple(f"Impossible de recuperer les donnees de {ticker}.")
        return
    price = float(quote["close"])
    chg   = float(quote.get("percent_change", 0))

    smtf, s1h, s4h, s1d, aligned, d1h, d4h, d1d = compute_mtf_score(asset["symbol"])

    bar = ""
    for tf, sc in [("1h", s1h), ("4h", s4h), ("1j", s1d)]:
        status = "OK" if sc >= SCORE_THRESHOLD else "--"
        bar += f"  [{tf}] {sc}/100 {status}\n"

    verdict = "SETUP MATUR" if smtf >= SCORE_THRESHOLD and aligned >= 2 else "PAS ENCORE MATUR"

    send_simple(
        f"Scores MTF {ticker} :\n"
        f"----------------------------------------------\n"
        f"Prix : {asset['currency']}{price:.2f} ({chg:+.2f}%)\n\n"
        f"TIMEFRAMES :\n"
        f"{bar}\n"
        f"Score MTF : {smtf}/100\n"
        f"Alignes   : {aligned}/3\n"
        f"Verdict   : {verdict}\n"
        f"----------------------------------------------\n"
        f"{datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )


def handle_scores_all():
    """Affiche un score rapide de tous les actifs de la watchlist active."""
    active = get_active_watchlist()
    send_simple(f"Calcul des scores pour {len(active)} actifs... (1-2 min)")
    results = []
    for asset in active:
        quote = get_quote(asset["symbol"])
        if not quote or "close" not in quote:
            results.append(f"  {asset['symbol']} : pas de donnees")
            continue
        price = float(quote["close"])
        chg   = float(quote.get("percent_change", 0))
        smtf, s1h, s4h, s1d, aligned, _, _, _ = compute_mtf_score(asset["symbol"])
        status = "OK" if smtf >= SCORE_THRESHOLD and aligned >= 2 else "--"
        results.append(
            f"  {status} {asset['symbol']} : {asset['currency']}{price:.2f} "
            f"({chg:+.1f}%) MTF:{smtf} [{aligned}/3]"
        )
        time.sleep(1)

    send_simple(
        f"Scores MTF - {datetime.now().strftime('%H:%M')}\n"
        f"----------------------------------------------\n"
        + "\n".join(results) +
        f"\n----------------------------------------------\n"
        f"OK = signal potentiel | -- = pas encore"
    )
    send_menu()


    """Repond au /analyse TICKER - analyse complete + signal."""
    send_simple(f"Analyse MTF complete de {ticker} en cours... (30-60 sec)")
    asset = find_asset(ticker)

    # Prix
    quote = get_quote(asset["symbol"])
    if not quote or "close" not in quote:
        send_simple(f"Impossible de recuperer les donnees de {ticker}.\nVerifie le symbole.")
        return

    price = float(quote["close"])
    chg   = float(quote.get("percent_change", 0))

    # Scores MTF
    smtf, s1h, s4h, s1d, aligned, d1h, d4h, d1d = compute_mtf_score(asset["symbol"])

    # Analyse Claude
    send_simple(f"Scores MTF calcules. Analyse Claude en cours...")
    signal = claude_analyze(asset, price, chg, s1h, s4h, s1d, smtf, aligned, d1h, d4h, d1d)

    if not signal:
        send_simple(f"Erreur lors de l'analyse Claude pour {ticker}.")
        return

    # Envoie le signal complet avec boutons
    msg_id = send_signal(asset, price, chg, s1h, s4h, s1d, smtf, aligned, signal)
    if msg_id and signal.get("signal") in ("BUY", "SELL"):
        save_trade(asset["symbol"], asset["name"], signal, s1h, s4h, s1d, smtf, msg_id)
        log.info(f"Analyse manuelle {ticker} -> {signal['signal']}")


# ================================================================
# SCAN PRINCIPAL
# ================================================================

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
    log.info(f"Scan MTF {symbol}...")

    quote = get_quote(symbol)
    if not quote or "close" not in quote:
        log.warning(f"{symbol} : pas de donnees")
        return

    price = float(quote["close"])
    chg   = float(quote.get("percent_change", 0))

    # Calcul des scores MTF
    smtf, s1h, s4h, s1d, aligned, d1h, d4h, d1d = compute_mtf_score(symbol)

    log.info(
        f"{symbol} | {asset['currency']}{price:.2f} ({chg:+.2f}%) | "
        f"1h:{s1h} 4h:{s4h} 1j:{s1d} MTF:{smtf}/100 | "
        f"Alignes:{aligned}/3"
    )

    # Alerte seulement si score MTF suffisant ET au moins 2 timeframes alignes
    if smtf >= SCORE_THRESHOLD and aligned >= 2 and can_alert(symbol):
        log.info(f"[TARGET] {symbol} MTF:{smtf} aligned:{aligned} -> Claude...")
        signal = claude_analyze(asset, price, chg, s1h, s4h, s1d, smtf, aligned, d1h, d4h, d1d)

        if signal and signal.get("signal") in ("BUY", "SELL"):
            msg_id = send_signal(asset, price, chg, s1h, s4h, s1d, smtf, aligned, signal)
            if msg_id:
                save_trade(symbol, asset["name"], signal, s1h, s4h, s1d, smtf, msg_id)
                already_alerted[symbol] = datetime.now()
                log.info(f"Signal {signal['signal']} envoye pour {symbol}")
    else:
        if smtf < SCORE_THRESHOLD:
            log.info(f"{symbol} : MTF score trop bas ({smtf} < {SCORE_THRESHOLD})")
        elif aligned < 2:
            log.info(f"{symbol} : timeframes pas assez alignes ({aligned}/3)")


def run_scan_cycle():
    log.info("=" * 50)
    log.info(f"Scan MTF - {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    log.info("=" * 50)
    active = get_active_watchlist()
    log.info(f"{len(active)} actifs a scanner")
    for asset in active:
        scan_asset(asset)
        time.sleep(2)


# ================================================================
# MAIN
# ================================================================

def main():
    log.info("[START] Trading IA v2 MTF - Demarrage")
    log.info(f"Poids : 1h={WEIGHT_1H} | 4h={WEIGHT_4H} | 1j={WEIGHT_1D}")

    init_db()

    threading.Thread(target=telegram_polling,       daemon=True).start()
    threading.Thread(target=daily_report_scheduler, daemon=True).start()

    send_startup()
    send_menu()

    while True:
        try:
            if is_trading_hours():
                run_scan_cycle()
            else:
                log.info(f"[PAUSE] Hors heures ({MARKET_OPEN_HOUR}h-{MARKET_CLOSE_HOUR}h)")

            log.info(f"[PAUSE] Prochain scan dans {SCAN_INTERVAL_MIN} min...")
            time.sleep(SCAN_INTERVAL_MIN * 60)

        except KeyboardInterrupt:
            log.info("[STOP] Agent MTF arrete")
            send_simple("[STOP] Trading IA v2 MTF - Arrete")
            break
        except Exception as e:
            log.error(f"Erreur : {e}")
            send_simple(f"[ATTENTION] Erreur agent MTF : {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
