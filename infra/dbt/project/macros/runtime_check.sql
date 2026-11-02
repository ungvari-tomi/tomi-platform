{% macro runtime_check() %}
  {% for name in ['orders', 'exchange_rates'] %}
    {% set relation = source('retailflow_raw', name) %}
    {% set result = run_query('select count(*) as row_count from ' ~ relation) %}
    {{ log('Verified raw source ' ~ name ~ ': ' ~ result.columns[0].values()[0] ~ ' rows', info=true) }}
  {% endfor %}
{% endmacro %}
