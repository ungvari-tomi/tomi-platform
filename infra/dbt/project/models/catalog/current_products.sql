{{ config(materialized='view') }}

-- Latest observed metadata, including retired products; not historical validity.
SELECT *
FROM {{ ref('stg_product_versions') }}
QUALIFY ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY revision DESC) = 1
