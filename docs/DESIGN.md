# Design system

This file is the doc of record for how evalskills looks: the web app, the repo docs and the system diagrams. Enes
approved it step by step on 2026-11-08. Reference mockups are in `docs/design/ref-*.png`. They use sample data, not
results.
Enes's feedback after the first deploy (2026-11-10, #77) changed the header, the search row, the result view (now a
dialog), motion and security pills. His next round (#81) gave each severity its own colour, cut rows to `owner/repo`
and made only the leaderboard scroll. The reference mockups predate both.

The rules adapt [unslop](https://github.com/heydamir/unslop) by Damir Sokolovsky (CC BY 4.0).

## Character

The page should feel exact, quiet and nocturnal: a dark page with high contrast and no decoration. The leaderboard
is the product, so everything else stays out of its way.

- Quiet chrome: one neutral ramp and one accent, and the accent is used rarely.
- No gradients, glows, dot grids, blobs, emoji or uppercase tracked labels. Pills are used for one thing only:
  security findings (see Security pills).
- Signature detail: every result shows `owner/repo/path @ <short sha>`, the exact commit evaluated, in the result
  view. Rows show `owner/repo` only (#81).

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
| critical `#FF8589` | 8.6 | 8.1 |
| high `#FFA877` | 10.6 | 10.1 |
| medium `#D6C05A` | 11.0 | 10.5 |

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
- Leaderboard rows are 64–72 tall on desktop. On mobile, the security pills may wrap to a third line.
- Radius: 8 for buttons, inputs and tabs, 12 for the open result and panels, 14 for diagram zones. An inner radius is
  the outer radius minus the padding.
- Motion: colour and opacity transitions, 150 ms, `cubic-bezier(0.22, 1, 0.36, 1)`. The result dialog and the
  completion notice also move 8 px (24 px for the mobile sheet) while fading, over 180 ms. Nothing loops, except
  the running indicator. All motion is off under `prefers-reduced-motion`.

## Layout

See `ref-layout.png`.

- One centered column, about 720 px wide, with the same structure on mobile and desktop.
- From top to bottom: a top bar with the wordmark (Geist 20/28, 500, `--fg`) on the left and the GitHub mark on
  the right; a header row; the category tabs; search on its own full-width row; the score note and security key;
  the leaderboard; a footer.
- The header row is a compact split (#89): the title "Evaluate agent skills" (26/32, 500) over the subtitle "Results
  from NVIDIA SkillEvaluator, pinned to the commit tested", left-aligned, with **Check a skill** (40 px, primary) on
  the right, centred against them. On mobile they stack in reading order, left-aligned. A centred stack with a big
  button was the template default (unslop) and took more than half of the first screen.
- The page scrolls to its end. The leaderboard sits in its own container, at most the viewport height minus 96 px
  (64 px on mobile), its last row closed by a hairline. Its scroll hands over to the page at either end, so the footer is
  always reachable (#89). On desktop the board starts in the upper half of the first screen.
- Footer: one quiet 13 px row under the board, "evalskills · Static results from NVIDIA SkillEvaluator" on the left,
  "How scores work" and "Source" (only with `SOURCE_REPO_URL`) on the right; stacked on mobile.
- Scrollbars are thin and quiet everywhere: `--line2` thumb on a transparent track, 8 px in WebKit.
- The GitHub mark links to the project's source. It appears only when the server sets `SOURCE_REPO_URL`.
- **Check a skill** opens the repo input in place below the header row, full column width, with no modal. "Find skills" lists the skills found at the
  resolved commit as a checklist, and "Evaluate N skills" queues them.
- Repositories with many skills (#83): a filter box and "Select all" sit above the checklist. Skills that need a run
  come first. Listed skills whose content is unchanged come last, ticked and disabled as "Up to date". The limit of 10
  counts only skills that need a run. "Select all" ticks the visible ones up to the limit and then turns into "Clear".
  To add a whole repository, submit 10, then look it up again.

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
count distinct finding types at medium or above, the same figure as the result view's Security line. On rows, a
muted "Security:" label comes first, and a one-line key above the list names the three colours (#81).

Each severity has its own colour, used the same way on pills, in the Security line (each part in its own colour) and
in the findings list:

| Pill | Colour |
| --- | --- |
| `N critical` | red text `#FF8589` on red at 12%, red border at 25% (as before #81; Enes kept it) |
| `N high` | orange text `#FFA877` on `#F0884D` at 12%, border at 28% |
| `N medium` | yellow text `#D6C05A` on yellow at 8%, border at 22% |
| `clean` (nothing at medium or above, complete analysis) | `--muted` text, `--line2` border |
| `partial scan` (dialog only: SkillSpector reported incomplete analysis) | `--muted` text, dashed `--line2` border |
| `scan incomplete` (a scanner crashed, counts hidden) | amber text, dashed amber border |

`partial scan` appears only in the dialog, after any severity pills, and the Security line says in words that
findings may be missing. Rows leave it out because it applies to most skills. A partial analysis never reads as
clean, so a partial scan with no findings shows no pill on its row; the key says what a row without pills
means. A skill without a completed scan shows no pill.
A tooltip says the pills are static pattern matches, not confirmed vulnerabilities.

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
