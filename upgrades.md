# TradeSight Future Upgrades & Brainstorming

This document tracks planned structural upgrades for the TradeSight trading orchestrator to make it production-ready for live capital.

---

## 1. Upgrade from Market Orders to Marketable Limit Orders
* **Goal:** Reduce execution slippage on trade entry and exit.
* **Details:** 
  * Instead of placing a standard market order which fills at any price, place a limit order priced slightly through the bid/ask spread (e.g., current ask + $0.05).
  * This guarantees a fast fill like a market order under normal conditions, but protects the account from massive losses if the price gaps during execution.

---

## 2. Upgrade from Local Stop-Management to Broker-Side Bracket Orders
* **Goal:** Eliminate operational risk (internet drop, script crash, power outage) leaving positions unprotected.
* **Details:**
  * When opening a position, submit the entry order alongside a dependent Stop-Loss order and Take-Profit limit order (a classic OCO / Bracket Order structure) directly to TWS.
  * TWS/IBKR servers will hold and manage the exits, ensuring protection even if the local Python orchestrator goes offline.

---

## 3. Brainstorming & Research Items (For Implementation Phase)
* **Partial Fills:** How does the script handle a bracket order where only a portion of the entry size gets filled?
* **Exit Modification:** How do we dynamically adjust a broker-side stop loss when the strategy triggers a trailing stop or custom indicator exit mid-trade?
* **Sector Groups Tuning:** Expanding the overnight tuner to optimize parameters by asset sector groups (Tech vs. Staples vs. Energy) continuously.
