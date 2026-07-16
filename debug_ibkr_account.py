"""
IBKR Account Debugger — USD-focused view for a CAD base-currency account.
Usage: .venv/bin/python debug_ibkr_account.py
"""
import asyncio

HOST = "127.0.0.1"
PORT = 7497
CLIENT_ID = 42

def test_yahoo_fallback(symbol: str):
    import urllib.request
    import json
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            if not data.get('chart', {}).get('result'):
                return None
            meta = data['chart']['result'][0]['meta']
            return {
                'price': meta.get('regularMarketPrice'),
                'prev_close': meta.get('chartPreviousClose')
            }
    except Exception as e:
        print("Fallback Error:", e)
        return None

async def main():
    from ib_async import IB

    ib = IB()

    print(f"\n🔌 Connecting to TWS at {HOST}:{PORT} clientId={CLIENT_ID}...")
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID)
    print("✅ Connected!")

    print("⏳ Waiting 5s for data to stream...")
    await asyncio.sleep(5)

    accounts = ib.managedAccounts()
    print(f"   Account: {accounts}\n")

    av_list = ib.accountValues()

    # ── All currencies present ───────────────────────────────────────────
    currencies = sorted(set(av.currency for av in av_list))
    print(f"📌 Currencies in accountValues: {currencies}\n")

    # ── USD-specific entries ─────────────────────────────────────────────
    print("=" * 60)
    print("💵 USD-denominated account values:")
    print("=" * 60)
    usd_vals = [av for av in av_list if av.currency == "USD"]
    if usd_vals:
        for av in sorted(usd_vals, key=lambda x: x.tag):
            print(f"  {av.tag:<35} value={av.value}")
    else:
        print("  ⚠️  No USD entries found in accountValues()")

    # ── Key totals in all currencies ─────────────────────────────────────
    print("\n" + "=" * 60)
    print("📊 Key tags across all currencies:")
    print("=" * 60)
    key_tags = ["TotalCashValue", "NetLiquidation", "BuyingPower",
                "GrossPositionValue", "EquityWithLoanValue"]
    for tag in key_tags:
        matches = [av for av in av_list if av.tag == tag]
        for av in matches:
            print(f"  {av.tag:<35} currency={av.currency:<6} value={av.value}")

    # ── USD positions from portfolio ─────────────────────────────────────
    print("\n" + "=" * 60)
    print("📋 portfolio() — positions (values in stock's native currency):")
    print("=" * 60)
    total_usd_mv = 0.0
    for p in ib.portfolio():
        mv = float(p.marketValue) if p.marketValue else 0
        cur = getattr(p.contract, 'currency', 'USD')
        if cur == 'USD':
            total_usd_mv += mv
        print(f"  {p.contract.symbol:<8} currency={cur} "
              f"pos={p.position:<5} marketValue={mv:.2f} "
              f"unrealizedPNL={p.unrealizedPNL:.2f}")
    print(f"\n  ➡ Total USD stock market value: ${total_usd_mv:,.2f}")

    # ── Positions via reqPositionsAsync ─────────────────────────────────
    print("\n" + "=" * 60)
    print("📋 reqPositionsAsync():")
    print("=" * 60)
    try:
        positions = await ib.reqPositionsAsync()
        for p in (positions or []):
            cur = getattr(p.contract, 'currency', '?')
            print(f"  {p.contract.symbol:<8} secType={p.contract.secType:<6} "
                  f"currency={cur:<4} qty={p.position} avgCost={p.avgCost:.4f}")
    except Exception as e:
        print(f"  ❌ {e}")

    # ── Market Data Quote Diagnostics ───────────────────────────────────
    print("\n" + "=" * 60)
    print("📡 Testing Free Delayed Market Data (Type 3) Quotes:")
    print("=" * 60)
    from ib_async import Stock
    test_symbols = ['AAPL', 'MSFT', 'SPY', 'JNJ']
    for sym in test_symbols:
        print(f"🔍 Requesting quote for {sym}...")
        contract = Stock(sym, 'SMART', 'USD')
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified or qualified[0] is None:
            print(f"  ❌ Failed to qualify contract for {sym}")
            continue
        
        # Set market data type to 3 (delayed)
        ib.reqMarketDataType(3)
        
        # Request ticker stream
        tickers = await ib.reqTickersAsync(qualified[0])
        if not tickers:
            print(f"  ❌ reqTickersAsync returned empty for {sym}")
            continue
        
        ticker = tickers[0]
        
        # Wait up to 5 seconds to observe when prices populate
        start_time = asyncio.get_event_loop().time()
        found = False
        for i in range(25):  # 25 * 200ms = 5 seconds max
            last_price = getattr(ticker, 'last', 0.0)
            bid = getattr(ticker, 'bid', 0.0)
            ask = getattr(ticker, 'ask', 0.0)
            
            # Print state if values start coming in or on final loop
            if (last_price == last_price and last_price > 0) or \
               (bid == bid and bid > 0) or \
               (ask == ask and ask > 0):
                elapsed = asyncio.get_event_loop().time() - start_time
                print(f"  ✅ Received data for {sym} in {elapsed:.2f}s:")
                print(f"     Bid: {bid} | Ask: {ask} | Last: {last_price} | Close: {ticker.close}")
                found = True
                break
            await asyncio.sleep(0.2)
            
        if not found:
            print(f"  ⚠️  Timed out (5s) waiting for {sym} price. Ticker state:")
            print(f"     Bid: {ticker.bid} | Ask: {ticker.ask} | Last: {ticker.last} | Close: {ticker.close}")
            print(f"     Attempting Yahoo Finance fallback for {sym}...")
            yf_res = test_yahoo_fallback(sym)
            if yf_res:
                print(f"     ✅ Yahoo Finance fallback successful: Price = ${yf_res['price']:.2f}")
            else:
                print(f"     ❌ Yahoo Finance fallback failed.")

    print("\n✅ Done.")
    ib.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
