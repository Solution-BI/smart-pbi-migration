# Smart Axiata – MicroStrategy → Power BI migration factory

Translates MicroStrategy dashboards (metadata only) into Power BI projects:
TMDL semantic models in models/, PBIR reports in reports/.
Full rules: docs/Migration_Factory_Build_Spec.md.

## Hard rules
- Never open files under data/. Read runs/<id>/scorecard.json only.
- Never read DAX query results through an MCP tool; comparisons run via src/validate.
- One Power Query query per source table, steps only from rules/folding.yaml;
  no Value.NativeQuery, no Odbc.Query.
- Never resolve a flag silently: log it in decisions/<dashboard>.yaml as open,
  with a proposal, and apply the default from rules/flags.yaml.
- Keep the MicroStrategy ID as an annotation on every table, column, measure and visual.
- Skip dashboards whose status is locked in registry.yaml.

## Stages
/extract → /parse → /consolidate (Tech Lead approves consolidation.yaml)
→ /build-model <model> → /build-report <dashboard> → /validate <dashboard> --pass 1

## Conventions
- Models: SM_<Domain>_<Subject>; measures in a _Measures table; keys hidden.
- Names: business names from MicroStrategy; titles copied word for word.
- Theme: from theme/ only; no colours or fonts at visual level.

## Done, per dashboard
generated → pass 1 scored → flags closed → human review (locked) → pass 2 → evidence pack
