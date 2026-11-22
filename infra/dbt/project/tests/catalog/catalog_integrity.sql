-- Raw revision values and lineage remain visible as failing quality checks.
SELECT p.product_id, p.revision
FROM {{ source('product_catalog', 'product_versions') }} AS p
LEFT JOIN {{ source('product_catalog_ops', 'loaded_batches') }} AS b
    ON p.first_manifest = b.manifest_path
WHERE p.revision < 1 OR p.schema_version <> 1 OR p.updated_at < p.created_at
    OR p.updated_at > CURRENT_TIMESTAMP
    OR b.manifest_path IS NULL OR b.source <> 'product-catalog/products-v1'
    OR SHA256(p.document_json) <> p.document_sha256
UNION ALL
SELECT m.product_id, m.revision
FROM {{ source('product_catalog_ops', 'product_batch_records') }} AS m
LEFT JOIN {{ source('product_catalog', 'product_versions') }} AS p
    ON m.product_id = p.product_id AND m.revision = p.revision
LEFT JOIN {{ source('product_catalog_ops', 'loaded_batches') }} AS b
    ON m.manifest_path = b.manifest_path
WHERE p.product_id IS NULL OR b.manifest_path IS NULL
