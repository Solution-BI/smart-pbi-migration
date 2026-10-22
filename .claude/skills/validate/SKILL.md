---
name: validate
description: Run validation pass 1 or 2 of the migration factory for one dashboard – compare each Power BI visual with its MicroStrategy visualization through scripts and write a scorecard. Use only when the team runs /validate <dashboard> --pass 1|2.
disable-model-invocation: true
---

# Validate – stages 7 and 9

Hard rule: data never enters the conversation. Run src/validate and read runs/<id>/scorecard.json only. Never open data/.

- MicroStrategy side: dashboard instance, scenario selections applied, visualization data read through the REST API.
- Power BI side: one DAX query per visual (SUMMARIZECOLUMNS on the same grouping) through the DAX runner.
- Same data date on both sides.
- Pass 1: default filter state; rows within 5% and totals within 1% (config/thresholds.yaml).
- Pass 2: scenario file; exact match at displayed precision; response times; screenshots; evidence pack.

Report green, amber or red per visual and update the dashboard's status in registry.yaml.
