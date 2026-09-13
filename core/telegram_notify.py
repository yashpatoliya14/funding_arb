"""
Requirement #10: Telegram updates for entries, exits, errors, and periodic heartbeats.

Setup:
  1. Message @BotFather on Telegram -> /newbot -> get your bot token.
  2. Message your new bot once (anything), then hit
     https://api.telegram.org/bot<TOKEN>/getUpdates to find your chat_id.
  3. Put both in your .env / settings.
"""

import requests


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled and bool(bot_token) and bool(chat_id)
        self._base = f"https://api.telegram.org/bot{bot_token}"

    def send(self, message: str):
        if not self.enabled:
            print(f"[telegram disabled] {message}")
            return
        try:
            requests.post(f"{self._base}/sendMessage",
                           json={"chat_id": self.chat_id, "text": message,
                                 "parse_mode": "HTML"}, timeout=5)
        except Exception as e:
            print(f"[telegram send failed] {e} -- message was: {message}")

    # convenience wrappers used throughout engine.py
    def entry(self, symbol, ex_a, ex_b, edge_pct, leverage):
        self.send(f"🟢 <b>ENTRY</b> {symbol}\n{ex_a} vs {ex_b}\n"
                  f"Net edge: {edge_pct:.4f}% | Leverage: {leverage}x")

    def leg_risk(self, symbol, filled_exchange, unfilled_exchange):
        self.send(f"⚠️ <b>LEG RISK</b> {symbol}\n{filled_exchange} filled, "
                  f"{unfilled_exchange} did not. Closing {filled_exchange} immediately.")

    def basis_drift_stop(self, symbol, drift_pct):
        self.send(f"🛑 <b>BASIS DRIFT STOP</b> {symbol}\nDrift {drift_pct:.3f}% exceeded limit. "
                  f"Closing both legs.")

    def exit(self, symbol, pnl_estimate=None):
        pnl_str = f" | Est. P&L: {pnl_estimate:.2f} INR" if pnl_estimate is not None else ""
        self.send(f"🔵 <b>EXIT</b> {symbol}{pnl_str}")

    def error(self, context, err):
        self.send(f"❌ <b>ERROR</b> [{context}]\n{err}")

    def heartbeat(self, status: str):
        self.send(f"ℹ️ {status}")
