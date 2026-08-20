# Dashboard design QA

## Visual target

- Selected reference:
  `/Users/akorotkevich/.codex/generated_images/01a0207e-a46d-73a3-8977-bf7083387cb3/exec-abdd9b36-3972-4ceb-8578-801fe1ea39b9.png`
- Reference dimensions: 864 × 1821 pixels
- Implementation viewport: 1440 × 1000 CSS pixels, with ordinary page scrolling
- Combined top-section comparison:
  `artifacts/design-qa/reference-vs-implementation-top.png`

## Visible comparison

The implementation follows the selected dark analytical visual system: compact
sticky controls, a four-column KPI band, restrained borders and radii, green/red
outcome accents, blue interaction accents, an 8/4 executive grid, a 4/8 pattern
grid, six annual cards, an 8/4 ledger/detail grid, and a three-card quality footer.
The page remains deliberately scrollable rather than compressing every analytical
section into one viewport.

The implementation uses real Plotly and grid components rather than reproduced
image assets. Synthetic values therefore differ from the visual target while panel
hierarchy, density, alignment, typography, color, and interaction placement remain
visually consistent.

## Interaction and state checks

- Period selection changes the checked horizon state.
- Episode search filters the ledger and updates the selected detail panel.
- Profile and market-class dimensions are independently available.
- Synthetic history renders all designed visual states.
- Retained local history renders populated multi-year, heatmap, exposure, episode,
  notable-change, and data-quality sections.
- Unsupported retained-data metrics show explicit unavailable states.
- Readiness returns HTTP 200 at `/readyz`.
- Fresh browser sessions report no warnings or errors.

## Fix history

1. Changed annual and episode grids to the ordinary desktop breakpoint so their
   selected side-by-side layouts do not collapse prematurely.
2. Added a dark grid scrollbar treatment to eliminate an inconsistent light strip.
3. Added the active page size to the grid selector to remove component warnings.
4. Deduplicated timezone options when the source timezone is already UTC.
5. Ordered retained-data episodes newest first, compacted long decimal display, and
   shortened generated episode labels for readable drill-downs.
6. Removed unsupported unit labels from real-data detail headings while retaining
   synthetic unit-specific labels where the underlying values exist.

## Evidence

- `artifacts/design-qa/synthetic-dashboard-top.png`
- `artifacts/design-qa/synthetic-dashboard-lower.png`
- `artifacts/design-qa/synthetic-dashboard-quality-final.png`
- `artifacts/design-qa/reference-vs-implementation-top.png`

final result: passed
