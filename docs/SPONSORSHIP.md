# Funding a public live tier

The board shows static checks only. A public live tier (Tier 3: an agent runs each skill's cases with the skill and
without it) needs a model budget or an LLM sponsor, because NVIDIA Build's free trial key may only be used for
internal evaluation ([ADR-0007](adr/0007-no-public-live-tier-without-funding.md)). This page says what a sponsor
would pay for, on what terms, and how the cost is worked out. Totals stay empty until the internal pilot (#121,
experiment E12) has measured tokens per skill. No total is estimated before that.

## What a sponsor pays for

Model calls for live runs of the catalog's skills, on one model the sponsor chooses or provides: the agent's calls
in both arms (with and without the skill), and the judge's calls. The sandbox, hosting and maintenance are not
part of it.

## Terms (ADR-0007, decision 4)

- One fixed agent and model per live column, so scores are comparable within that column only. Models are never
  mixed into one score.
- The sponsor is named next to every result it paid for.
- The sponsor has no say over which skills run or how they are scored.
- The caps and the sandbox still apply (ADR-0003, ADR-0004). Untrusted skills run only under gVisor, and the model
  key stays in the gateway.

## Cost formula

```
cost = skills × repeats × ( arms × (agent input tokens × input price + agent output tokens × output price)
                          + judge input tokens × judge input price + judge output tokens × judge output price )
       / 1,000,000
```

- `skills`: the number of skills run. The catalog had 166 on 2026-11-12.
- `repeats`: runs per skill. The pilot measures whether one is enough, from the spread between runs.
- `arms`: 2 (with the skill and the baseline).
- `agent input tokens`, `agent output tokens`: the agent's mean per skill, per arm, per run, as measured in E12.
- `judge input tokens`, `judge output tokens`: the judge's mean per skill, per run, both arms together, as
  measured in E12. The judge may run on a different, cheaper model than the agent, so it is priced on its own.
  Prices are per million tokens.

Token counts measured on one model don't carry over to another: tokenizers, step counts and caching differ. A
sponsor's estimate therefore needs a few runs on their model first. Nemotron's weights are open, so a paid Nemotron
host can use the pilot's measurements directly.

## Candidate models

Prices are list prices, each with its source and the date it was checked. A row without a checked price says so.

| Model | Agent (SkillEvaluator default) | Provider | Input $/M | Output $/M | Source, checked | Catalog total |
| --- | --- | --- | --- | --- | --- | --- |
| Nemotron 3 Super 120B (`nvidia/nemotron-3-super-120b-a12b`) | OpenCode | a paid Nemotron host | not yet checked | not yet checked | - | pending E12 measurements |
| Claude Sonnet 5.5 (`claude-sonnet-5-5`) | Claude Code | Anthropic | not yet checked | not yet checked | [anthropic.com/pricing](https://www.anthropic.com/pricing) | pending E12 measurements |
| GPT-5.4 mini (`gpt-5.4-mini`) | Codex | OpenAI | not yet checked | not yet checked | [openai.com/api/pricing](https://openai.com/api/pricing/) | pending E12 measurements |

Rows are added when a sponsor offers a model. A total is filled in only from E12's measurements and a checked price.
