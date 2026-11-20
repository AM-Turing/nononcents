# Local report analysis

The Reports page (**Outlook**, unless renamed) combines Django ledger calculations with an
optional **Deep Pattern Analysis** from a private local model. The calculations always come
from the ledger. The model only writes the narrative.

## What the page shows

Select a date range, then **Generate AI analysis**. The page renders:

- Spending, Bills, refunds, income, and internal-transfer totals.
- A net-spending pie chart and table by category, headed **Spending after refunds**. Refunds
  reduce category spending.
- Current, previous, absolute-change, and percentage-change category figures.
- Money in, money out, net movement, and prior-period change for every account.
- The Deep Pattern Analysis card, beside the pie chart. It opens with a bottom line up front
  (concerns, an expense summary against current and prior income, and a recommended way
  ahead), then gives an overview, good habits, concerning habits, neutral patterns,
  actionable options, account flow, and a conclusion.

For an in-progress range, the model receives an explicit as-of date and separate
actual-to-date, scheduled-remaining, and projected-full-period figures. It must not treat a
paycheck scheduled later in the range as lost income. Trend evidence includes six preceding
equal-length windows, daily aggregate deposit timing, expected pay dates, category history,
and adjusted Savings movement. Completed ranges use actual results rather than forecasts.

## Running the analysis in the background

Generating runs in a background thread (`budget/analysis_jobs.py`), because a local model
can take a minute or more, longer than a web request should wait.

1. Select **Generate AI analysis**. The page records a run and shows the same range with
   the run number in the address.
2. While the run is going, the page reloads itself every 10 seconds.
3. When it finishes, the card shows the analysis. If it fails, the card shows the reason
   (for example, the local model is not configured or returned invalid data).
4. A run still going after 10 minutes is reported as failed. A restart of the web
   process also ends any run in progress; it then appears as failed after that limit.
5. Requesting the same range again while a run is still going reuses that run instead of
   starting another.

The stored `ReportAnalysisRun` keeps only the checked result (or the error). The data sent to
the model is not stored. Use **Run the analysis again** on the page to start a new run.

## Trust boundary

Django sends the configured endpoint one JSON summary containing date boundaries, aggregate
monetary totals, daily aggregate income totals, scheduled pay dates, and category and
account names. It does not send individual transaction descriptions, merchant names, income
sources, notes, user details, passwords, API credentials, or database identifiers. Names are
treated as untrusted data labels. The model response must match a fixed JSON schema.

Before it is saved, `enforce_report_grounding` checks the narrative. Every dollar amount it
cites must appear in the report data, and it must not describe income as lost or missing
when the figures do not support that. If the check fails, the narrative is replaced by a
plain summary built from the same figures. Django templates escape all text before display.

The model cannot write to the ledger. Charts, tables, CSV output, and summary cards are
calculated independently with `Decimal` values and remain available if the model is disabled,
times out, or returns malformed data.

## Configuration

These settings are read from the environment. See `.env.example`, and `nononcents/settings.py` for how each one is read.

| Variable | Default | Meaning |
|---|---|---|
| `REPORT_LLM_PROVIDER` | `disabled` | `openai_compatible` enables the analysis (`llama_cpp` is accepted as an alias). Anything else disables it. |
| `REPORT_LLM_URL` | empty | OpenAI-compatible `/v1/chat/completions` URL. |
| `REPORT_LLM_MODEL` | empty | Model name the server expects. |
| `REPORT_LLM_API_KEY` | empty | Optional bearer token, sent only to `REPORT_LLM_URL`. Never logged or shown. |
| `REPORT_LLM_TIMEOUT_SECONDS` | `120` | Clamped to 5 to 300. |
| `REPORT_LLM_MAX_TOKENS` | `2200` | Clamped to 500 to 32000. Reasoning models spend part of this on thinking before they answer. |
| `REPORT_LLM_DISABLE_THINKING` | `false` | When `true`, the request asks the server not to reason before answering, where the server supports it. |

The analysis is enabled only when the provider is `openai_compatible` (or `llama_cpp`) and both the URL and the model are set. Keep the endpoint on a network you trust: it receives the summary described under Trust boundary.

Example for Ollama running on the Docker host (on Linux, the container also needs the `host-gateway` entry described in the deployment guide):

```dotenv
REPORT_LLM_PROVIDER=openai_compatible
REPORT_LLM_URL=http://host.docker.internal:11434/v1/chat/completions
REPORT_LLM_MODEL=your-model-name
REPORT_LLM_TIMEOUT_SECONDS=240
REPORT_LLM_DISABLE_THINKING=true
REPORT_LLM_MAX_TOKENS=4000
```

Reasoning models can take minutes to answer while reasoning is on, and a long answer can be cut short by `REPORT_LLM_MAX_TOKENS`. `REPORT_LLM_DISABLE_THINKING=true` gives a shorter, faster answer where the server supports that option. The model name must match what your server reports.

Set `REPORT_LLM_PROVIDER=disabled` to turn the narrative off. Totals, charts, tables, CSV, and Print / PDF remain available.
