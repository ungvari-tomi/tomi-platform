# Exchange-rate client

[Ten-currency generation and EUR valuation](european-currencies.md) are deployed; the versioned migration and live pipelines are verified.

The client adds a real reference-data source alongside the synthetic orders API.
It demonstrates HTTP ingestion boundaries, provenance, failure handling, schema
validation, and exact decimal arithmetic. It does not load a warehouse, expose a
local rate endpoint, or modify order responses. The separate landing CLI can now
persist its original response body in Azurite.

## Source and contract

- Dataset: daily EUR/HUF reference exchange rates.
- Publisher: European Central Bank (ECB); API distributor: Frankfurter.
- Internal owner: RetailFlow project maintainer; classification: public reference data.
- Approved project use: portfolio demonstrations, tests, and analytical conversion.
- [Frankfurter documentation](https://frankfurter.dev/) documents free public
  access without an API key, historical dates, and provider filtering.
- [ECB reference rates](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html)
  normally update on working days at around 16:00 CET, excluding TARGET closing
  days. They are reference information, not transaction execution quotes.
- Requests use `https://api.frankfurter.dev/v2/rates` with `date`, `base=EUR`,
  `quotes=HUF`, and `providers=ecb`, avoiding Frankfurter's default blended rates.
- The response must contain exactly one EUR/HUF observation, a finite positive
  rate, and a publication date on or before the requested date. Malformed JSON,
  missing fields, incorrect currency pairs, and invalid rates are rejected.
- Output retains `requested_date`, `rate_date`, `base`, `quote`, decimal `rate`,
  `source`, and `provider`. Source attribution records the explicitly requested
  provider; it is not cryptographic verification of upstream provenance.

## Dates, precision, and failures

A Saturday request was verified live on 2026-10-31 to return Friday 2026-10-30's
366.33 HUF/EUR rate. The client preserves the returned date instead of relabelling
it. It does not predict holiday calendars or silently invent missing observations.
It rejects rates after the requested date; it does not impose an age limit or
independently verify that the provider returned the newest possible observation.
Future monitoring should use the publication calendar, not the order-generation
interval. An old historical request is not inherently stale.

Numeric JSON rates are parsed directly into Decimal and serialized as strings.
For HUF and EUR, both accounting units have two decimal places, so converting HUF
minor units to euro cents requires division by the HUF-per-EUR rate. The helper
rounds half up once at the total level. Negative amounts are supported for future
refund scenarios. It is a calculation helper, not implemented warehouse enrichment.

HTTPX uses TLS verification and a 10-second timeout per network phase. The client
does not follow redirects and does not use environment proxy settings. There are
at most three attempts for transport failures and HTTP 429/500/502/503/504.
Backoff is 1 then 2 seconds; numeric `Retry-After` overrides this up to 30 seconds
per wait. HTTP-date or malformed `Retry-After` values use normal backoff. Other
HTTP errors, including 404 and 422, and invalid payloads fail immediately. Retry
events are JSON logs on stderr; CLI output is JSON on stdout, with nonzero exit
status on failure. There is no fallback to synthetic rates and no retry forever.

## Reproducibility, cost, and remaining work

`tests/fixtures/frankfurter_eur_huf_2026-10-30.json` contains the raw response
retrieved from Frankfurter on 2026-10-31 using the above provider/date parameters.
Tests replay it for both weekday and weekend scenarios using HTTPX MockTransport;
they never require the public API. Live weekend behavior was verified separately.
The fixture contains public financial reference data, not customer information;
the project's MIT license does not replace upstream data terms. See the linked
ECB publication page and its copyright terms for source reuse conditions.

No account, paid service, trial, or cloud provisioning is required. A live request
does rely on internet access and an external public service, whose future terms
or availability are outside this project. No background polling was introduced.
The landing CLI now stores original response bodies and retrieval timestamps in
Azurite for replay. A warehouse rate table, automatic caching, scheduling,
freshness monitoring, and transformation-layer EUR columns remain future work.
