{{ config(materialized='view') }}

select
    cast(provider as varchar) as rate_provider,
    cast(base as varchar) as base_currency_code,
    cast(quote as varchar) as quote_currency_code,
    cast(rate_date as date) as rate_date,
    cast(rate as decimal(24, 12)) as quote_units_per_base_unit,
    cast(first_manifest as varchar) as source_manifest_path
from {{ source('retailflow_raw', 'exchange_rates') }}
