"""One-time setup on XRPL Testnet. Creates and funds:
issuer (test USD, stands in for RLUSD), merchant, attester, 5 customers.
Writes seeds to wallets.json (gitignored). Takes ~1-2 minutes."""
import json
import sys

from xrpl.models.transactions import AccountSet, AccountSetAsfFlag, Payment, TrustSet
from xrpl.wallet import generate_faucet_wallet

from xrpl_ops import client, submit, tx_link, usd

N_CUSTOMERS = int(sys.argv[1]) if len(sys.argv) > 1 else 5


def step(label, tx, w):
    code, h, _ = submit(tx, w)
    print(f"{label:22s} {code}  {tx_link(h)}")
    if code != "tesSUCCESS":
        sys.exit(f"stopped at {label}")


print(f"funding {3 + N_CUSTOMERS} testnet wallets from the faucet...")
issuer, merchant, attester = (generate_faucet_wallet(client) for _ in range(3))
customers = [generate_faucet_wallet(client) for _ in range(N_CUSTOMERS)]

step("issuer DefaultRipple", AccountSet(account=issuer.address, set_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE), issuer)
step("issuer AllowLocking", AccountSet(account=issuer.address, set_flag=AccountSetAsfFlag.ASF_ALLOW_TRUSTLINE_LOCKING), issuer)
for name, w, amt in [("merchant", merchant, 5000)] + [(f"customer{i+1}", c, 500) for i, c in enumerate(customers)]:
    step(f"{name} TrustSet", TrustSet(account=w.address, limit_amount=usd(issuer.address, 1_000_000)), w)
    step(f"{name} fund {amt} USD", Payment(account=issuer.address, destination=w.address, amount=usd(issuer.address, amt)), issuer)

json.dump({"issuer": issuer.seed, "merchant": merchant.seed, "attester": attester.seed,
           "customers": [c.seed for c in customers]}, open("wallets.json", "w"), indent=1)
print("\nsaved wallets.json")
print("merchant:", merchant.address, "| attester:", attester.address, "| issuer:", issuer.address)
