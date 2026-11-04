{{ config(enabled=var('check_freshness', false), tags=['freshness']) }}

with required(quote) as (
    values ('HUF'), ('PLN'), ('CZK'), ('RON'), ('GBP'), ('CHF'), ('SEK'), ('NOK'), ('DKK')
), published as (
    select quote, max(rate_date) as latest_date
    from {{ source('retailflow_raw', 'exchange_rates') }}
    where provider = 'ECB' and base = 'EUR'
    group by quote
), loaded as (
    select m.quote, max(b.loaded_at) as latest_load
    from {{ source('retailflow_ops', 'fx_batch_records') }} m
    join {{ source('retailflow_ops', 'loaded_batches') }} b using (manifest_path)
    where m.provider = 'ECB' and m.base = 'EUR' and b.source = 'exchange-rates'
    group by m.quote
)
select q.quote
from required q
left join published p using (quote)
left join loaded l using (quote)
where p.latest_date is null
   or p.latest_date > cast(timezone('UTC', current_timestamp) as date)
   or cast(timezone('UTC', current_timestamp) as date) - p.latest_date > 7
   or l.latest_load is null
   or l.latest_load < current_timestamp - interval '30 hours'
   or l.latest_load > current_timestamp + interval '60 seconds'
