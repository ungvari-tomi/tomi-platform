{{ config(tags=['catalog_freshness']) }}

-- Test empty/future snapshots as well as stale per-product observations.
WITH observations AS (
    SELECT m.product_id, MAX(b.retrieved_at) AS observed_at
    FROM {{ source('product_catalog_ops', 'product_batch_records') }} AS m
    INNER JOIN {{ source('product_catalog_ops', 'loaded_batches') }} AS b
        ON m.manifest_path = b.manifest_path
    WHERE b.source = 'product-catalog/products-v1'
    GROUP BY m.product_id
)
SELECT 'missing_stale_or_future_catalog' AS failure
WHERE (SELECT COUNT(*) FROM observations) <> 20
    OR EXISTS (
        SELECT 1 FROM observations
        WHERE observed_at IS NULL
            OR observed_at < CURRENT_TIMESTAMP - INTERVAL '24 hours'
            OR observed_at > CURRENT_TIMESTAMP
    )
