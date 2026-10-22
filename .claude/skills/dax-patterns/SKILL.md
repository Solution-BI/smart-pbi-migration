---
name: dax-patterns
description: Pattern library for translating MicroStrategy metrics into DAX measures – aggregations, count distinct, compound metrics and smart totals, level (dimensionality) metrics, conditional metrics, transformations, percent to total, rank, running functions and semi-additive metrics. Use whenever a MicroStrategy metric expression or dimensionality has to become a DAX measure, even for a single metric.
---

# MicroStrategy → DAX patterns

Read the expression tree and dimensionality units from the Modeling API extract, then apply the matching pattern.

| MicroStrategy | DAX pattern |
| --- | --- |
| Sum, Avg, Min, Max, Count on a fact | SUM, AVERAGE, MIN, MAX, COUNT on the fact column |
| Count distinct of an attribute | DISTINCTCOUNT on the fact table's key column, not the dimension's |
| Compound metric with smart totals | DIVIDE([A], [B]) – totals stay a ratio of totals |
| Compound metric without smart totals | SUMX over the visual grain – flag SUBTOTAL_RULE |
| Level metric (ignore or absolute) | CALCULATE([Base], REMOVEFILTERS(…)) or ALLEXCEPT, from the dimensionality units |
| Conditional metric | CALCULATE([Base], <filter>), checking the report-filter settings |
| Last year, YTD, MTD, previous month | SAMEPERIODLASTYEAR, DATESYTD, DATESMTD, DATEADD on the marked Date table |
| Percent to total, rank | DIVIDE([M], CALCULATE([M], ALLSELECTED(…))); RANKX over ALLSELECTED |
| Running and moving functions | WINDOW or OFFSET – flag OLAP_FUNCTION |
| First or last in fact | LASTNONBLANKVALUE or a closing-balance pattern – flag SEMI_ADDITIVE |

Rules:
- Use DIVIDE for every ratio.
- Keep the MicroStrategy format string and add the metric ID as an annotation.
- A construct not in the table: propose DAX with a one-line explanation and flag METRIC_PATTERN; it closes only after a test against MicroStrategy values.
