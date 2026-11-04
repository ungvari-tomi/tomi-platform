{{ config(materialized='view') }}

SELECT
    CAST(provider AS VARCHAR) AS rate_provider,
    CAST(base AS VARCHAR) AS base_currency_code,
    CAST(quote AS VARCHAR) AS quote_currency_code,
    CAST(rate_date AS DATE) AS rate_date,
    CAST(rate AS DECIMAL(24, 12)) AS quote_units_per_base_unit,
    CAST(first_manifest AS VARCHAR) AS source_manifest_path
FROM {{ source('retailflow_raw', 'exchange_rates') }}
