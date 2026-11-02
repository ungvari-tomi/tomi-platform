select *
from {{ ref('orders_eur') }}
where
    (conversion_status in ('identity', 'converted')
     and (eur_total_minor is null or eur_total_minor < 0 or total_eur is null))
    or (conversion_status not in ('identity', 'converted')
        and (eur_total_minor is not null or total_eur is not null))
    or (conversion_status = 'identity' and (currency_code != 'EUR' or eur_total_minor != total_minor))
    or (conversion_status = 'converted'
        and (currency_code = 'EUR' or fx_age_days not between 0 and 7
             or currency_units_per_eur <= 0 or fx_request_manifest_path is null))
    or fx_rate_date > order_date
