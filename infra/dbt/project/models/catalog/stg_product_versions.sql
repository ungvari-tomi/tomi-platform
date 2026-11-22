{{ config(materialized='view') }}

SELECT
    p.product_id, p.revision, p.schema_version, p.sku, p.name AS product_name,
    p.category, p.status AS catalog_status, p.created_at, p.updated_at,
    p.document_json, p.document_sha256, p.first_manifest AS catalog_manifest,
    b.retrieved_at AS catalog_observed_at, b.loaded_at AS catalog_loaded_at,
    JSON_EXTRACT(p.document_json, '$.attributes') AS attributes_json,
    JSON_EXTRACT(p.document_json, '$.markets') AS markets_json,
    JSON_EXTRACT(p.document_json, '$.variants') AS variants_json
FROM {{ source('product_catalog', 'product_versions') }} AS p
LEFT JOIN {{ source('product_catalog_ops', 'loaded_batches') }} AS b
    ON p.first_manifest = b.manifest_path
