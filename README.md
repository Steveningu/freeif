# FreeIf

**Event-backed promotions for businesses. Priced and hedged on Gemini Predictions, guaranteed on the XRP Ledger.**

"Buy today, and if the event happens, your purchase is free."

## The problem

In 2019 a Houston furniture store promised a full refund on mattresses over $3,000 if the Astros won the World Series. To cover the liability, the owner flew between casinos in several states and placed about $13M in hedge bets. The promotion was a huge marketing success. Running it was painful:

- **Hedging is manual.** Big retailers use slow, expensive prize-indemnity insurers. Small businesses cannot.
- **Customers have to trust the store.** If the event happens, will the store actually pay? Claims, delays, fine print.
- **Payouts are manual.** Thousands of refunds processed one by one.

## What FreeIf does

A business turns a marketing budget into the boldest promise it can afford.

```
promo cost = P(event) x refund % x units x price
```

- **Gemini Predictions** supplies P(event) from a live order book, so the cost is known before the promo starts, and lets the business hedge the same contract.
- **The XRP Ledger** locks each customer's refund in escrow the moment they pay, so the promise is credible, and settles every refund automatically.

The business sets the refund percentage and a unit cap ("first 200 units only"), so its worst case and its expected cost are both known before the promo opens.

The customer never bets. They buy a product at the listed price. The business carries the risk and lays it off on a regulated exchange.

## How it works

1. **Price** (Gemini): the business picks an event. The live Gemini book gives the market-implied probability and the cost per customer.
2. **Dry run** (XRPL `simulate`, XLS-69): every refund escrow is simulated against the live ledger before anyone pays.
3. **Lock** (XRPL Token Escrow, XLS-85, plus a PREIMAGE-SHA-256 crypto-condition): when a customer pays, their refund is locked on-ledger immediately. The business only holds the condition hash, so it cannot withhold refunds.
4. **Hedge** (Gemini): the business buys YES on the same contract. If the promo pays out, the hedge covers it, so the business's cost is fixed regardless of the outcome.
5. **Resolve** (XRPL Price Oracle, XLS-47): an independent attester reads the result and publishes it on-ledger with `OracleSet`.
6. **Settle**: on YES, the attester reveals the fulfillment and every refund is released with `EscrowFinish`. On NO, the escrows expire and `EscrowCancel` returns the funds to the business. Nobody files a claim.

## XRPL features used

Token Escrow on an issued dollar, crypto-conditions, `simulate`, Price Oracle, trustlines, payments. No smart contracts: every step is a native ledger transaction.

## Trust model

The business cannot withhold refunds because it never holds the release key. The attester holds it and reveals it only on a YES resolution. In this demo the attester is a separate account and module. In production it would be a multisig of independent signers or the exchange's own settlement signature.

## Honest notes

- The testnet dollar is issued by our own account and stands in for RLUSD.
- Live odds come from Gemini's public production API. When a book is one-sided, the hedge is priced at the ask and the UI says so.
- Resolution: the attester first looks for Gemini's own resolution on the contract. If it is not exposed yet, it falls back to Gemini's public 1-minute candles (window open vs close) and labels the source both in the UI and in the on-ledger oracle record.
- Hedge orders go to the Gemini sandbox and require a sandbox API key. Without one, the UI shows the hedge as a plan (size, price, cost) and marks it as not submitted.
- The demo uses Gemini's 5-minute crypto contracts so an entire promo settles on camera. A real business would pick a game, the weather, or any other Gemini event. The code is the same.

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 setup_wallets.py          # once, about 1 to 2 minutes, writes wallets.json (gitignored)
# optional, enables real hedge orders (Gemini sandbox key with trading permission):
export GEMINI_KEY=... GEMINI_SECRET=...
uvicorn server:app --port 8010    # open http://localhost:8010
```

## Files

| File | Role |
|---|---|
| `server.py` | API, promo state machine, attester loop |
| `xrpl_ops.py` | Every XRPL transaction and the `simulate` call |
| `gemini.py` | Live odds, resolution, sandbox hedge orders |
| `attester.py` | Holds the release preimage, issues conditions and fulfillments |
| `setup_wallets.py` | Funds issuer, business, attester and five customers on testnet |
| `static/index.html` | Dashboard |
