{{ config(materialized='view') }}

WITH dated_orders AS (
    SELECT *, CAST(TIMEZONE('UTC', ordered_at) AS DATE) AS order_date
    FROM {{ ref('stg_orders') }}
), matched AS (
    SELECT
        o.*,
        fx.rate_date AS fx_rate_date,
        fx.quote_units_per_base_unit AS currency_units_per_eur,
        fx.source_manifest_path AS fx_rate_manifest_path,
        fx.request_manifest_path AS fx_request_manifest_path,
        order_date - fx.rate_date AS fx_age_days
    FROM dated_orders o
    LEFT JOIN LATERAL (
        SELECT r.*, m.manifest_path AS request_manifest_path
        FROM {{ source('retailflow_ops', 'fx_batch_records') }} m
        JOIN {{ ref('stg_exchange_rates') }} r
          ON m.provider = r.rate_provider AND m.base = r.base_currency_code
         AND m.quote = r.quote_currency_code AND m.rate_date = r.rate_date
        WHERE o.currency_code != 'EUR' AND m.requested_date = o.order_date
          AND r.rate_provider = 'ECB'
          AND r.base_currency_code = 'EUR' AND r.quote_currency_code = o.currency_code
          AND r.rate_date <= o.order_date
        ORDER BY r.rate_date DESC, m.manifest_path
        LIMIT 1
    ) fx ON TRUE
), classified AS (
    SELECT *,
        CASE WHEN currency_code = 'EUR' THEN 'identity'
             WHEN currency_code NOT IN ('HUF','PLN','CZK','RON','GBP','CHF','SEK','NOK','DKK') THEN 'unsupported_currency'
             WHEN fx_rate_date IS NULL THEN 'missing_rate'
             WHEN fx_age_days > 7 THEN 'stale_rate'
             ELSE 'converted' END AS conversion_status,
        CAST(currency_units_per_eur * 1000000000000::HUGEINT AS HUGEINT) AS scaled_rate
    FROM matched
), converted AS (
    SELECT * EXCLUDE (scaled_rate),
        CASE WHEN conversion_status = 'identity' THEN total_minor
             WHEN conversion_status = 'converted' THEN CAST(
                 (2::HUGEINT * total_minor * 1000000000000::HUGEINT + scaled_rate)
                 // NULLIF(2::HUGEINT * scaled_rate, 0) AS BIGINT
             ) END AS eur_total_minor
    FROM classified
)
SELECT *,
    CAST(CAST(eur_total_minor AS DECIMAL(24, 0)) * 0.01::DECIMAL(3, 2)
         AS DECIMAL(24, 2)) AS total_eur
FROM converted
