{{ config(materialized='view') }}

with dated_orders as (
    select *, cast(timezone('UTC', ordered_at) as date) as order_date
    from {{ ref('stg_orders') }}
), matched as (
    select
        o.*,
        fx.rate_date as fx_rate_date,
        fx.quote_units_per_base_unit as currency_units_per_eur,
        fx.source_manifest_path as fx_rate_manifest_path,
        fx.request_manifest_path as fx_request_manifest_path,
        order_date - fx.rate_date as fx_age_days
    from dated_orders o
    left join lateral (
        select r.*, m.manifest_path as request_manifest_path
        from {{ source('retailflow_ops', 'fx_batch_records') }} m
        join {{ ref('stg_exchange_rates') }} r
          on m.provider = r.rate_provider and m.base = r.base_currency_code
         and m.quote = r.quote_currency_code and m.rate_date = r.rate_date
        where o.currency_code != 'EUR' and m.requested_date = o.order_date
          and r.rate_provider = 'ECB'
          and r.base_currency_code = 'EUR' and r.quote_currency_code = o.currency_code
          and r.rate_date <= o.order_date
        order by r.rate_date desc, m.manifest_path
        limit 1
    ) fx on true
), classified as (
    select *,
        case when currency_code = 'EUR' then 'identity'
             when currency_code not in ('HUF','PLN','CZK','RON','GBP','CHF','SEK','NOK','DKK') then 'unsupported_currency'
             when fx_rate_date is null then 'missing_rate'
             when fx_age_days > 7 then 'stale_rate'
             else 'converted' end as conversion_status,
        cast(currency_units_per_eur * 1000000000000::hugeint as hugeint) as scaled_rate
    from matched
), converted as (
    select * exclude (scaled_rate),
        case when conversion_status = 'identity' then total_minor
             when conversion_status = 'converted' then cast(
                 (2::hugeint * total_minor * 1000000000000::hugeint + scaled_rate)
                 // nullif(2::hugeint * scaled_rate, 0) as bigint
             ) end as eur_total_minor
    from classified
)
select *,
    cast(cast(eur_total_minor as decimal(24, 0)) * 0.01::decimal(3, 2)
         as decimal(24, 2)) as total_eur
from converted
