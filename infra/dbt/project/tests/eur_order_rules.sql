SELECT *
FROM {{ ref('orders_eur') }}
WHERE
    (conversion_status IN ('identity', 'converted')
     AND (eur_total_minor IS NULL OR eur_total_minor < 0 OR total_eur IS NULL))
    OR (conversion_status NOT IN ('identity', 'converted')
        AND (eur_total_minor IS NOT NULL OR total_eur IS NOT NULL))
    OR (conversion_status = 'identity' AND (currency_code != 'EUR' OR eur_total_minor != total_minor))
    OR (conversion_status = 'converted'
        AND (currency_code = 'EUR' OR fx_age_days NOT BETWEEN 0 AND 7
             OR currency_units_per_eur <= 0 OR fx_request_manifest_path IS NULL))
    OR fx_rate_date > order_date
