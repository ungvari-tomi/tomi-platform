-- Mirror the version-compatible synthetic retail contract; raw rows remain visible.
with profiles(country_code,currency_code,minimum,maximum,step) as (
    values
        ('HU','HUF',200000,8000000,100),
        ('DE','EUR',500,20000,1),
        ('FR','EUR',500,20000,1),
        ('PL','PLN',2000,90000,1),
        ('CZ','CZK',12000,500000,100),
        ('RO','RON',2500,100000,1),
        ('GB','GBP',400,18000,1),
        ('CH','CHF',450,19000,1),
        ('SE','SEK',5000,230000,100),
        ('NO','NOK',5500,240000,100),
        ('DK','DKK',3500,150000,1)
)
select o.*
from {{ ref('stg_orders') }} o
left join profiles p on o.country_code=p.country_code
where o.total_minor != o.quantity * o.unit_price_minor
   or o.quantity > 5
   or trim(o.product_id)='' or trim(o.dataset_id)='' or trim(o.source_manifest_path)=''
   or p.country_code is null or o.currency_code != p.currency_code
   or o.unit_price_minor not between p.minimum and p.maximum
   or o.unit_price_minor % p.step != 0
