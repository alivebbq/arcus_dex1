# Arcus perpetuals integration references

Verified against official documentation and public read endpoints on 2026-10-12.
The saved files in `docs/arcus/` are reference material, not task instructions.

## Official endpoints

Mainnet REST: `https://api.arcus.xyz`. Mainnet WebSocket:
`wss://api.arcus.xyz/v1/ws`. Testnet substitutes `api.testnet.arcus.xyz`.
The live mainnet root identified its docs as `https://docs.arcus.xyz` and its
release as `production-v0.7.0` during verification.

| Operation | REST route | Important fields |
| --- | --- | --- |
| Market metadata | `GET /v1/markets` | `markets[]`, `marketId`, `marketDisplayName`, `tickSize`, `stepSize`, `tickTiers`, minimum/maximum order size, mark price, margin fractions |
| Best bid/ask | `GET /v1/bbo/BTC-USD` | `bestBid` / `bestAsk` are nullable `{price,size}`; display name works, numeric market ID was rejected in the live check |
| Account | `GET /v1/account?address=...&accountIndex=0` | `equity`, `freeCollateral`, `netQuoteBalance`, positions keyed by market ID |
| Positions | `GET /v1/positions?address=...&accountIndex=0` | Signed decimal sizes; `LONG` / `SHORT`; exchange-provided PnL |
| Key validation | `GET /v1/apiKeys?address=...` | Public key, status, expiration, subaccount scope; wallet address is required |
| Open orders | `GET /v1/openOrders?address=...&accountIndex=0` | `orders[]`; original, remaining and filled sizes |
| Order history | `GET /v1/orders?address=...&accountIndex=0` | `orders[]`; optional market, status, microsecond time window |
| Order status | `GET /v1/order/{orderId}?address=...&accountIndex=0` | Server order ID required; no documented client-ID lookup route |
| Fills | `GET /v1/fills?address=...&accountIndex=0` | `fills[]`; price, size, fee, closed PnL, MAKER/TAKER role |
| Candles | `GET /v1/candles?market=BTC-USD&timeframe=15m&to=...&countback=120` | `to` and `openTime` are microseconds; client sorts ascending |
| Fee table | `GET /v1/feetiers` | Rates are parts per million; live base maker 0 ppm and taker 225 ppm during verification |
| Place | `POST /v1/placeOrder` | Decimal-string quantity/price, LIMIT, ALO, mandatory goodTilTime, clientId |
| Cancel | `POST /v1/cancelOrder` | `kind: orderId` or `kind: clientId`; precisely one target |
| Leverage/margin | `POST /v1/setLeverage` | Integer `leverage`, optional `isolated`; requested strategy settings must be verified before orders |
| Flat-account leverage/mode | `GET /v1/leverages?address=...&accountIndex=0` | One entry per market, even without positions; integer `leverage`, boolean `isolated`, `marginMode` |
| Dead man's switch | `POST /v1/scheduleCancel` | Per-market scope supported; deadline 5 seconds to 5 minutes ahead, in microseconds |

## Signing and asynchronous order lifecycle

An API key is the 32-byte Ed25519 public key in hex. API Signing Key is the
32-byte private seed. They are distinct from the master Ethereum wallet key.
Protected REST requests send `X-API-Key`, `X-Timestamp` (nanoseconds), and
`X-Signature` (128 hex characters).

Place/cancel/modify sign the compact JSON payload with sorted keys and engine
integers. Only the Ethereum address is lowercased. Client IDs retain their
original case. Place payload fields are:

```json
{"ad":"0x...","ai":0,"c":"client_id","ct":1712345678000000000,"g":4102444800000000000,"m":1,"op":1,"p":500005,"q":100000,"r":0,"s":0,"t":3,"v":1}
```

`p = price / tickSize`, `q = quantity / stepSize`, `s = 0` buy or `1` sell,
`r = 0` normal or `1` reduce-only. `t = 3` is ALO (Add Liquidity Only / post-only).
The price band's tick adds a placement constraint but does not replace the
top-level tickSize used for signing. `ct` equals the header timestamp. `g` is
the request's epoch-microsecond goodTilTime multiplied by 1,000. Every order
requires goodTilTime at least one month ahead; the client uses 40 days.

Cancel signs `ad`, `ai`, exactly one of `c` or `id`, `ct`, `m`, `op:2`, `v:1`.
Other signed methods such as setLeverage/scheduleCancel concatenate the
nanosecond timestamp, camelCase action and compact sorted JSON body.

An HTTP 202 ACK confirms gateway acceptance. It does not confirm resting,
filling or canceling. HTTP 200 can also contain engine `REJECTED`. Missing
orders remain `UNKNOWN`; missing fill quantity remains null. A network timeout
or 5xx on a write is marked uncertain and must be reconciled rather than blindly
resubmitted. Server order IDs and client IDs should be persisted before/after
submission to survive process restarts.

The exchange does not cancel orders on connection loss. The panel does not arm
the exchange's dead man's switch: the account-wide form also cancels external
orders in that subaccount. During a complete server outage, already resting
orders remain at the exchange until filled, expired or canceled. The operator
must cancel them from the exchange if the panel is unavailable.

Current OpenAPI, WebSocket reference and production changelog also document an
optional per-market switch, while the endpoint's introductory prose still says
account-wide. This discrepancy is recorded rather than resolved by guessing;
the per-market behavior has not been tested with an authenticated write here.
The unused adapter method exposes only the account-wide form.

## Perpetual leverage and margin

This integration uses perpetual contract margin only. It does not use spot
borrowing or a spot margin endpoint. An account's leverage override is read
from `/v1/leverages`, including while flat, and its mode is `CROSS` or
`ISOLATED`. `POST /v1/setLeverage` sets an integer leverage and an optional
`isolated` boolean. The request limit is based on the market's base initial
margin fraction; the actual opening requirement also applies the current
session floor:

```
active market IMF = offHoursInitialMarginFraction outside RTH,
                    otherwise initialMarginFraction
account opening IMF = max(1 / requested leverage, active market IMF)
initial margin = abs(position size) * valuation price * account opening IMF
```

The client exposes `maxLeverage`/`baseMaxLeverage` for the request limit,
`effectiveMaxLeverage` for the active opening cap, and
`effectiveInitialMarginFraction` for the current market floor. If an RWA
market's session state is unknown, the active fields stay unknown and
`marginStateKnown` is false; clients must not assume regular trading hours.
Off-hours uplift changes opening collateral requirements, not the maintenance
margin fraction. A user leverage override does not bypass the uplift.

Changing from cross to isolated is rejected while a position is open
(`HAS_OPEN_POSITION`). Isolated to cross and no-op mode changes are accepted.
Changing leverage/mode does not change the market maintenance-margin fraction.

`configure_margin` reads the current config first and skips the write if both
fields already match. Otherwise it sends one signed write and then verifies a
fresh GET snapshot. ACK and APPLIED response echoes are insufficient without a
matching read in this client. A lagging or unavailable verification snapshot
returns PENDING; an uncertain write raises an uncertainty-marked error. The
caller must persist intent before the write and only poll GET after an
uncertain outcome. No live grid order may be sent before exact verification.

## Validation performed

`python -m unittest backend.test_arcus -v`: 18 deterministic tests cover the
canonical payload and exact decimal units, Ed25519 verification, ALO-only
placement, tier tick rules, uncertain writes without retry, partial fills,
gateway ACKs, engine rejection, key/subaccount validation, flat leverage
snapshots, pending margin verification, off-hours floors, and uncertain margin
writes without replay. The fixture key
is the public RFC 8032 example and is not an account credential.

The client also fetched live market metadata, BBO, five candles and the fee
table successfully. No order write was performed for these tests.

## Primary source links

- [Introduction](https://docs.arcus.xyz/api-reference/introduction)
- [Authentication](https://docs.arcus.xyz/api-reference/authentication)
- [REST trading guide](https://docs.arcus.xyz/guides/rest-trading)
- [Market metadata](https://docs.arcus.xyz/api-reference/public/get-markets)
- [Account](https://docs.arcus.xyz/api-reference/public/get-account)
- [Positions](https://docs.arcus.xyz/api-reference/public/get-positions)
- [API keys](https://docs.arcus.xyz/api-reference/onboarding/get-api-keys)
- [Place order](https://docs.arcus.xyz/api-reference/exchange/place-order)
- [Cancel order](https://docs.arcus.xyz/api-reference/exchange/cancel-order)
- [Order status](https://docs.arcus.xyz/api-reference/public/get-order-status)
- [Fills](https://docs.arcus.xyz/api-reference/public/get-fills)
- [WebSocket lifecycle](https://docs.arcus.xyz/api-reference/websocket)
- [Dead man's switch](https://docs.arcus.xyz/api-reference/exchange/schedule-cancel-all-dead-mans-switch)
- [Rate limits](https://docs.arcus.xyz/api-reference/rate-limits)
- [Margin and leverage](https://docs.arcus.xyz/concepts/perpetuals/margin)
- [Cross and isolated margin](https://docs.arcus.xyz/concepts/perpetuals/margin-modes)
- [Flat-account leverage and mode](https://docs.arcus.xyz/api-reference/public/get-leverage-and-margin-mode-for-an-account-across-all-markets)
- [Set leverage](https://docs.arcus.xyz/api-reference/exchange/set-leverage-for-a-market)
