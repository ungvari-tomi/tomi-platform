SELECT b.manifest_path
FROM {{ source('product_catalog_ops', 'loaded_batches') }} AS b
LEFT JOIN {{ source('product_catalog_ops', 'product_batch_records') }} AS m
    ON b.manifest_path = m.manifest_path
WHERE b.source = 'product-catalog/products-v1'
GROUP BY b.manifest_path, b.rows_seen
HAVING COUNT(m.product_id) <> b.rows_seen OR COUNT(DISTINCT m.product_id) <> 20
