"""
Requirement #10: Telegram updates for entries, exits, errors, and periodic heartbeats.

Setup:
  1. Message @BotFather on Telegram -> /newbot -> get your bot token.
  2. Message your new bot once (anything), then hit
     https://api.telegram.org/bot<TOKEN>/getUpdates to find your chat_id.
  3. Put both in your .env / settings.
"""

import requests

from core.funding_window import next_funding_time


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
            try:
                print(f"[telegram send failed] {e}")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # startup / shutdown
    # ------------------------------------------------------------------

    def startup(self, mode: str, exchange_pairs: list, scan_mode: str,
                leverage: int, notional_inr: float = 0, quantity: float = 0):
        """Send a clear startup confirmation so the user knows the bot is live."""
        pairs_str = "\n".join(f"  🔄 {f} ↔ {h}" for f, h in exchange_pairs)
        sizing = f"Notional: ₹{notional_inr:,.0f}" if notional_inr > 0 else f"Qty: {quantity}"
        self.send(
            f"🚀 <b>FUNDING ARB BOT — ONLINE</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>Mode:</b> <code>{mode}</code>\n"
            f"⚙️ <b>Scan Mode:</b> <code>{scan_mode}</code>\n"
            f"⚖️ <b>Leverage:</b> <code>{leverage}x</code>\n"
            f"💰 <b>Sizing:</b> <code>{sizing}</code>\n"
            f"⏱️ <b>Next Funding:</b> <code>{next_funding_time()}</code>\n"
            f"\n"
            f"<b>Active Pairs:</b>\n{pairs_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👀 <i>Monitoring markets for opportunities...</i>"
        )

    def window_open(self, pair_label: str, funding_time):
        """Sent right when the entry window opens before scanning."""
        self.send(
            f"🔔 <b>ENTRY WINDOW OPEN</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔄 <b>Pair:</b> {pair_label}\n"
            f"⏱️ <b>Funding At:</b> {funding_time}\n"
            f"🔍 <i>Scanning for best coin...</i>"
        )

    # ------------------------------------------------------------------
    # convenience wrappers used throughout engine.py
    # ------------------------------------------------------------------

    def entry(self, symbol, ex_a, ex_b, edge_pct, leverage):
        """Legacy simple entry notification."""
        self.send(
            f"🟢 <b>ENTRY</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Symbol: <b>{symbol}</b>\n"
            f"Exchanges: {ex_a} ↔ {ex_b}\n"
            f"Edge: {edge_pct:+.4f}% | Lev: {leverage}x"
        )

    def entry_full(self, base_asset, funding_ex, hedge_ex, opp):
        """Detailed entry notification with full cost breakdown."""
        # Determine readable direction text
        if opp.funding_side == "sell":
            direction_text = f"🟥 SHORT on {funding_ex} / 🟩 LONG on {hedge_ex}"
            collect_ex = funding_ex
            pay_ex = hedge_ex
        else:
            direction_text = f"🟩 LONG on {funding_ex} / 🟥 SHORT on {hedge_ex}"
            collect_ex = hedge_ex
            pay_ex = funding_ex

        self.send(
            f"⚡ <b>EXECUTING TRADE — {base_asset}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🧭 <b>Direction:</b> {direction_text}\n"
            f"\n"
            f"🏦 <b>Exchanges:</b>\n"
            f"  • {funding_ex}: {opp.funding_symbol} @ ₹{opp.funding_mark:,.2f}\n"
            f"  • {hedge_ex}: {opp.hedge_symbol} @ ₹{opp.hedge_mark:,.2f}\n"
            f"\n"
            f"💸 <b>Funding Edge:</b>\n"
            f"  🟢 Collect ({collect_ex}): {opp.funding_rate*100:+.4f}%\n"
            f"  🔴 Pay ({pay_ex}):     {opp.hedge_rate*100:+.4f}%\n"
            f"  ✨ Net funding:     {opp.net_funding_pct:+.4f}%\n"
            f"\n"
            f"📉 <b>Cost Breakdown:</b>\n"
            f"  Fees (entry+exit): -{opp.entry_fees_pct + opp.exit_fees_pct:.4f}%\n"
            f"  Spread cost:     -{opp.spread_cost_pct:.4f}%\n"
            f"  Slippage buffer: -{opp.slippage_cost_pct:.4f}%\n"
            f"  <b>Total costs:     -{opp.total_cost_pct:.4f}%</b>\n"
            f"\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💎 <b>NET P&L ESTIMATE: {opp.net_pnl_pct:+.4f}%</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>Position:</b> {opp.quantity:.6f} {base_asset} (₹{opp.notional_inr:,.0f})"
        )

    def exit(self, symbol_a, symbol_b=None, exchange_a=None, exchange_b=None,
             pnl_estimate=None, entry_basis_pct=None, current_basis_pct=None,
             hold_seconds=None):
        """Detailed exit notification with context."""
        lines = [
            f"🔵 <b>EXIT</b>",
            f"━━━━━━━━━━━━━━━━",
            f"",
            f"<b>Positions Closed:</b>"
        ]
        if exchange_a and symbol_a:
            lines.append(f"  {exchange_a}: {symbol_a}")
        if exchange_b and symbol_b:
            lines.append(f"  {exchange_b}: {symbol_b}")
        if hold_seconds is not None:
            if hold_seconds < 60:
                hold_str = f"{hold_seconds:.0f}s"
            elif hold_seconds < 3600:
                hold_str = f"{hold_seconds/60:.1f}m"
            else:
                hold_str = f"{hold_seconds/3600:.1f}h"
            lines.append(f"  Held for: {hold_str}")
        if entry_basis_pct is not None and current_basis_pct is not None:
            lines.append(f"  Basis at entry: {entry_basis_pct:.4f}%")
            lines.append(f"  Basis at exit:  {current_basis_pct:.4f}%")
            drift = current_basis_pct - entry_basis_pct
            lines.append(f"  Drift: {drift:+.4f}%")
        lines.append(f"  ⏱ {next_funding_time()}")
        if pnl_estimate is not None:
            lines.extend([
                f"",
                f"<b>━━━━━━━━━━━━━━━━</b>",
                f"<b>P&L: {pnl_estimate:+.2f} INR</b>",
                f"<b>━━━━━━━━━━━━━━━━</b>",
            ])
        self.send("\n".join(lines))

    def leg_risk(self, symbol_a, symbol_b, exchange_a, exchange_b,
                 filled_exchange, unfilled_exchange, filled_symbol, unfilled_symbol):
        """Detailed leg risk notification — one leg filled, other didn't."""
        self.send(
            f"⚠️ <b>LEG RISK</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"One leg filled but the other did NOT.\n"
            f"Closing the filled leg immediately.\n"
            f"\n"
            f"<b>Filled:</b>\n"
            f"  Exchange: {filled_exchange}\n"
            f"  Symbol:   {filled_symbol}\n"
            f"\n"
            f"<b>Unfilled:</b>\n"
            f"  Exchange: {unfilled_exchange}\n"
            f"  Symbol:   {unfilled_symbol}\n"
            f"\n"
            f"<b>Exchanges:</b> {exchange_a} ↔ {exchange_b}"
        )

    def basis_drift_stop(self, symbol_a, symbol_b, exchange_a, exchange_b,
                         drift_pct, entry_basis_pct, current_basis_pct):
        """Basis drift kill-switch notification."""
        self.send(
            f"🛑 <b>BASIS DRIFT STOP</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"Cross-exchange price gap moved too far against us.\n"
            f"Closing both legs NOW.\n"
            f"\n"
            f"<b>Positions:</b>\n"
            f"  {exchange_a}: {symbol_a}\n"
            f"  {exchange_b}: {symbol_b}\n"
            f"\n"
            f"<b>Basis Info:</b>\n"
            f"  Entry basis:  {entry_basis_pct:.4f}%\n"
            f"  Current basis: {current_basis_pct:.4f}%\n"
            f"  Drift: {drift_pct:+.4f}% (killed at threshold)"
        )

    def error(self, context, err):
        self.send(
            f"❌ <b>ERROR</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Context: {context}\n"
            f"Details: {err}"
        )

    def heartbeat(self, status: str):
        self.send(f"ℹ️ {status}")

    def opportunity_scan(self, pair_label, coin, net_pnl_pct, funding_pct,
                         cost_pct, direction_info):
        self.send(
            f"🔍 <b>SCAN — {pair_label}</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"Best coin: <b>{coin}</b>\n"
            f"Net P&L: {net_pnl_pct:+.4f}%\n"
            f"Funding: {funding_pct:.4f}% | Costs: {cost_pct:.4f}%\n"
            f"{direction_info}"
        )

    def no_opportunity(self, pair_label, coins_scanned):
        self.send(
            f"⚪ <b>NO EDGE — {pair_label}</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Scanned {coins_scanned} coins — no opportunity clears the cost threshold."
        )

