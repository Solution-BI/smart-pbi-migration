---
name: build-report
description: Run stage 6 of the migration factory for one dashboard – generate the PBIR report in reports/<dashboard>.Report from its IR, its semantic model and the theme template. Use only when the team runs /build-report <dashboard>.
disable-model-invocation: true
context: fork
---

# Build report – stage 6

Skip the dashboard if registry.yaml marks it `locked`.

- definition.pbir bound by path to the model assigned in consolidation.yaml.
- One page per MicroStrategy page, named "Chapter – Page" when there is more than one chapter; every page starts from the template in theme/.
- Visual types from rules/visuals.yaml; titles copied word for word; sort order and number formats carried over.
- Filter panel filters go to the filter pane – report level for one chapter, page level otherwise. Selectors become slicers; selector targets become edit interactions.
- Anything unmapped becomes a NO_EQUIVALENT or LAYOUT_APPROX flag.
- Validate every file against the PBIR JSON schemas before finishing.
