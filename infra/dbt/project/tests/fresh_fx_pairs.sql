{{ config(enabled=var('check_freshness', false), tags=['freshness']) }}

WITH required(quote) AS (
    VALUES ('HUF'), ('PLN'), ('CZK'), ('RON'), ('GBP'), ('CHF'), ('SEK'), ('NOK'), ('DKK')
), published AS (
    SELECT quote, MAX(rate_date) AS latest_date
    FROM {{ source('retailflow_raw', 'exchange_rates') }}
    WHERE provider = 'ECB' AND base = 'EUR'
    GROUP BY quote
), loaded AS (
    SELECT m.quote, MAX(b.loaded_at) AS latest_load
    FROM {{ source('retailflow_ops', 'fx_batch_records') }} m
    JOIN {{ source('retailflow_ops', 'loaded_batches') }} b USING (manifest_path)
    WHERE m.provider = 'ECB' AND m.base = 'EUR' AND b.source = 'exchange-rates'
    GROUP BY m.quote
)
SELECT q.quote
FROM required q
LEFT JOIN published p USING (quote)
LEFT JOIN loaded l USING (quote)
WHERE p.latest_date IS NULL
   OR p.latest_date > CAST(TIMEZONE('UTC', CURRENT_TIMESTAMP) AS DATE)
   OR CAST(TIMEZONE('UTC', CURRENT_TIMESTAMP) AS DATE) - p.latest_date > 7
   OR l.latest_load IS NULL
   OR l.latest_load < CURRENT_TIMESTAMP - INTERVAL '30 hours'
   OR l.latest_load > CURRENT_TIMESTAMP + INTERVAL '60 seconds'
