# Shared task sets

Empty on purpose. Under ADR-0006 we don't author benchmark tasks or graders. A set here is vendored and adapted
from a public benchmark, one directory per set (`evals.json`, optional `grader.py`, `config.yml`, `files/`), and
must carry a `SOURCE.md` naming the upstream benchmark, its exact revision and its licence, and saying what was
adapted. `pipeline/tier3.py` refuses a set without one. Enes approves each set before it lands.

Without a set, `pipeline/tier3.py` runs a skill on SkillEvaluator's own content: the skill's published `evals/`,
or the starter case SkillEvaluator prepares.
