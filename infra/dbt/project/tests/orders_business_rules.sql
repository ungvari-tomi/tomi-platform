-- Mirror the version-compatible synthetic retail contract; raw rows remain visible.
WITH profiles(country_code,currency_code,minimum,maximum,step) AS (
    VALUES
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
SELECT o.*
FROM {{ ref('stg_orders') }} o
LEFT JOIN profiles p ON o.country_code=p.country_code
WHERE o.total_minor != o.quantity * o.unit_price_minor
   OR o.quantity > 5
   OR TRIM(o.product_id)='' OR TRIM(o.dataset_id)='' OR TRIM(o.source_manifest_path)=''
   OR p.country_code IS NULL OR o.currency_code != p.currency_code
   OR o.unit_price_minor NOT BETWEEN p.minimum AND p.maximum
   OR o.unit_price_minor % p.step != 0
