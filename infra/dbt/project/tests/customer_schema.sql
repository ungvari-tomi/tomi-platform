{{ config(enabled=var('customer_pipeline', false), tags=['customers']) }}
-- A valid checksum cannot legitimize adding contact fields or key material.
{% set contracts = {
 'orders': ['dataset_id','order_id','created_at','country','currency','product_id',
            'quantity','unit_price_minor','total_minor','customer_token'],
 'customers': ['dataset_id','customer_token','as_of','country','age_band','synthetic'],
 'batches': ['manifest','sha256','loaded_at','seed','interval_seconds','key_id'],
 'order_batches': ['manifest','dataset_id','order_id'],
 'customer_batches': ['manifest','dataset_id','customer_token','as_of']
} %}
SELECT table_name, column_name FROM information_schema.columns
WHERE table_catalog = CURRENT_DATABASE() AND table_schema = 'privacy_demo'
  AND (
  {% for table, columns in contracts.items() %}
    (table_name = '{{ table }}' AND column_name NOT IN (
      {% for column in columns %}'{{ column }}'{% if not loop.last %},{% endif %}{% endfor %}
    )){% if not loop.last %} OR {% endif %}
  {% endfor %}
  )
