# Design system

This file is the doc of record for how evalskills looks: the web app, the repo docs and the system diagrams. Enes
approved it step by step on 2026-11-08. Reference mockups are in `docs/design/ref-*.png`. They use sample data, not
results.
Enes's feedback after the first deploy (2026-11-10, #77) changed the header, the search row, the result view (now a
dialog), motion and security pills; the reference mockups predate it.

The rules adapt [unslop](https://github.com/heydamir/unslop) by Damir Sokolovsky (CC BY 4.0).

## Character

The page should feel exact, quiet and nocturnal: a dark page with high contrast and no decoration. The leaderboard
is the product, so everything else stays out of its way.

- Quiet chrome: one neutral ramp and one accent, and the accent is used rarely.
- No gradients, glows, dot grids, blobs, emoji or uppercase tracked labels. Pills are used for one thing only:
  security findings (see Security pills).
- Signature detail: every result shows `owner/repo @ <short sha>`, the exact commit evaluated. It appears on rows
  and in the result view, and nowhere else.

## Colour

| Token | Value | Use |
| --- | --- | --- |
| `--bg` | `#07080B` | Page |
| `--card` | `#0E1015` | Open result, inputs, diagram boxes |
| `--raised` | `#161922` | Active tab, code, secondary button |
| `--line` | `#EEF0F5` at 8% (`#EEF0F514`) | Hairlines and borders |
| `--fg` | `#EEF0F5` | Primary text, primary button fill |
| `--fg2` | `#A3A9B8` | Secondary text |
| `--muted` | `#7A8296` | Meta, ranks, captions |
| `--accent` | `#5FD4C4` | Running status, the main path in diagrams, focus ring |

Contrast on `--bg` and on `--card`:

| Colour | On `--bg` | On `--card` |
| --- | --- | --- |
| `--fg` | 17.6 | 16.7 |
| `--fg2` | 8.5 | 8.1 |
| `--muted` | 5.2 | 4.9 |
| accent | 11.2 | 10.6 |
| inconclusive | 9.0 | 8.6 |
| failed | 5.1 | 4.9 |

- The primary button is `--fg` with `--bg` text, not the accent.
- Depth comes from surface steps and hairlines. There are no shadows.
- There is no light theme for now.

## Type

There are two families and two weights (400 and 500). Both fonts are OFL, so ship their licence with the font files.

| Use | Family | Size / line | Weight |
| --- | --- | --- | --- |
| Title | Geist | 30 / 36, -0.01em | 500 |
| Body, subtitle | Geist | 15 / 22 | 400 |
| Skill name | Geist | 15 | 500 |
| Controls, tabs, buttons | Geist | 14 | 400 or 500 |
| Meta (`repo @ sha`, rank, labels) | Geist Mono | 12 | 400 |
| Score | Geist Mono | 18, tabular | 400 |

- Use sentence case everywhere and no italics.
- Small text gets no negative tracking.
- Scores use one format: one decimal, tabular figures.

## Space, radius and motion

- Spacing sits on a 4px grid: 4, 8, 12, 16, 24, 32, 48, 64.
- Leaderboard rows are 64–72 tall on desktop. On mobile, a long path and the security pills may wrap to a third
  line.
- Radius: 8 for buttons, inputs and tabs, 12 for the open result and panels, 14 for diagram zones. An inner radius is
  the outer radius minus the padding.
- Motion: colour and opacity transitions, 150 ms, `cubic-bezier(0.22, 1, 0.36, 1)`. The result dialog and the
  completion notice also move 8 px (24 px for the mobile sheet) while fading, over 180 ms. Nothing loops, except
  the running indicator. All motion is off under `prefers-reduced-motion`.

## Layout

See `ref-layout.png`.

- One centered column, about 720 px wide, with the same structure on mobile and desktop.
- From top to bottom: a top bar with the wordmark (Geist 20/28, 500, `--fg`) on the left and the GitHub mark on
  the right, the title "Evaluate agent skills", the subtitle "Results from NVIDIA SkillEvaluator, pinned to the
  commit tested", the **Add a skill** button, the category tabs, search on its own full-width row, then the
  leaderboard.
- The GitHub mark links to the project's source. It appears only when the server sets `SOURCE_REPO_URL`.
- **Add a skill** turns into the repo input in place, with no modal. "Find skills" lists the skills found at the
  resolved commit as a checklist, and "Evaluate N skills" queues them.

## Statuses

See `ref-type-status.png`. A status is text with a 6 px dot, in the score column.

| Status | Colour | Text |
| --- | --- | --- |
| Completed | `--fg` | The score itself, no label |
| Running | accent `#5FD4C4` | `Running 2 of 6` (progress when known) |
| Queued | `--fg2`, hollow dot | `Queued, 3 ahead` |
| Inconclusive | `#D9A55B` | `Inconclusive` |
| Unsupported | `--muted`, dim dot | `Unsupported` |
| Failed | `#E5484D` | `Failed` |

A missing score is never shown as 0 and never left blank without a status.

## Result view

See `ref-result-view.png`. Clicking a row opens the result in a dialog (radius 12, `--card`, hairline border,
black backdrop at 60%), with the skill name, `owner/repo @ sha` and the security pills in a sticky header. On
mobile it is a bottom sheet and its two columns stack. Esc, the close button or a backdrop click closes it, and
`/s/<id>` opens it directly.
- After a submission, the dialog for the first submitted skill opens at once and follows queued, running and
  completed live. When the result lands it says "Evaluation complete". If the dialog was closed meanwhile, a small
  notice at the bottom offers "View".

- **Score breakdown:** Live tasks, Structure and Security each sit on their own line with their own figure. They are
  never blended. Live shows the no-skill baseline next to it. A one-line note says what the ranking uses and why
  scores from different case sets are not directly comparable.
- **Evaluation:** commit, evaluator version, agent and model, number of cases (and whether they were generated),
  runtime, cost, and date.
- **Links:** "Source at `<sha>`" and "Full report".
- **No install line.** We provide a score and a report. We are not a skill library (Enes, 2026-11-08). This drops
  the brief's "installation instructions where verified" on purpose.

## Security pills

Each completed row, and the dialog header, shows security findings as small mono pills (11 px, radius 9). They
count distinct finding types at medium or above, the same figure as the result view's Security line.

| Pill | Colour |
| --- | --- |
| `N critical`, `N high` | red text `#FF8589` on red at 12%, red border at 25% |
| `N medium` | amber text on amber at 10%, amber border at 24% |
| `clean` (nothing at medium or above, complete analysis) | `--muted` text, `--line2` border |
| `partial` (SkillSpector reported incomplete analysis) | `--muted` text, dashed `--line2` border |
| `scan incomplete` (a scanner crashed, counts hidden) | amber text, dashed amber border |

`partial` sits after any severity pills and replaces `clean`: a partial analysis never reads as clean. A skill
without a completed scan shows no pill. A tooltip says the pills are static pattern
matches, not confirmed vulnerabilities.

## Copy

- Claim only what a row can show. Avoid "real tasks", "any skill", "tested in production" and similar, and name
  NVIDIA SkillEvaluator as the engine.
- Use short labels with no trailing full stops on labels or buttons, and no exclamation marks.
- UI text uses no em dashes.

## Diagrams

See `ref-diagram.png`. Diagrams use the same tokens as the UI.

- **Boxes:** `--card` fill, hairline border, radius 8. The name is in Geist 14/500, with one mono detail line in
  `--muted`.
- **Zones:** a trusted zone is a faint panel (`--fg` at 2%) with a hairline border. An untrusted zone has a dashed
  hairline. Each zone gets a Geist 13 label and one mono line saying what the zone guarantees.
- **Arrows:** 1.25 px `#4A5163`, open arrowheads, right angles only, with short mono labels.
- **Accent:** teal marks only the path the diagram is about, plus the one box it centres on.
- No icons, shadows or glows. Render at 2x and keep the HTML source next to the PNG.
