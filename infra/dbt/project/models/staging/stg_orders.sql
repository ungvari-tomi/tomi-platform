{{ config(materialized='view') }}

SELECT
    CAST(dataset_id AS VARCHAR) AS dataset_id,
    CAST(order_id AS BIGINT) AS order_id,
    CAST(created_at AS TIMESTAMPTZ) AS ordered_at,
    CAST(country AS VARCHAR) AS country_code,
    CAST(currency AS VARCHAR) AS currency_code,
    CAST(product_id AS VARCHAR) AS product_id,
    CAST(quantity AS INTEGER) AS quantity,
    CAST(unit_price_minor AS BIGINT) AS unit_price_minor,
    CAST(total_minor AS BIGINT) AS total_minor,
    CAST(first_manifest AS VARCHAR) AS source_manifest_path
FROM {{ source('retailflow_raw', 'orders') }}
