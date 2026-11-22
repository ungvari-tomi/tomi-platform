{{ config(materialized='view') }}

SELECT
    i.*,
    p.product_name AS current_product_name, p.category AS current_product_category,
    p.catalog_status, p.revision AS catalog_revision,
    p.catalog_manifest, p.catalog_observed_at,
    p.product_id IS NOT NULL AS catalog_matched
FROM {{ source('inventory_stream', 'inventory_movements') }} AS i
LEFT JOIN {{ ref('current_products') }} AS p ON i.product_id = p.product_id
