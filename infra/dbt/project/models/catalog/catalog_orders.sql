{{ config(materialized='view') }}

-- Preserve the order grain and minor units. No variants/markets array expansion.
SELECT
    o.*,
    p.product_name AS current_product_name, p.category AS current_product_category,
    p.catalog_status, p.revision AS catalog_revision,
    p.catalog_manifest, p.catalog_observed_at,
    p.product_id IS NOT NULL AS catalog_matched
FROM {{ source('retailflow_raw', 'orders') }} AS o
LEFT JOIN {{ ref('current_products') }} AS p ON o.product_id = p.product_id
