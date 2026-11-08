# DeTect

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Chrome](https://img.shields.io/badge/Chrome-Manifest_V3-4285F4?logo=googlechrome&logoColor=white)
![JavaScript](https://img.shields.io/badge/JavaScript-Vanilla-F7DF1E?logo=javascript&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-Database-003B57?logo=sqlite&logoColor=white)
![Gemini](https://img.shields.io/badge/Gemini-API-4285F4)

DeTect helps you assess bot risk of a Mastodon profile. Paste a profile link to analyse up to **300 public followers** and get a **score out of 100**, a colour-coded risk scale and a short explanation.

Python calculates the statistics and runs hypothesis tests to determine the likelyhood of the profile being a bot. Gemini explains the results in a concise manner. For a closer look, expand **stats analysis** to see the key figures behind the result.

## Project layout

```text
DeTect/
├── backend/
│   ├── local_server.py       # Connects the extension to Python
│   ├── run_pipeline.py       # Collects data and runs analysis
│   ├── collect_mastodon.py   # Reads public Mastodon data
│   ├── history.py            # Saves and merges follower records
│   ├── analysis/             # Statistics, scoring and research tools
│   ├── llm/                  # Gemini integration and prompts
│   ├── models/               # Follower data model
│   └── tests/                # Backend tests
├── extension/
│   ├── manifest.json
│   ├── popup.html
│   ├── popup.css
│   ├── popup.js
│   ├── logic.mjs
│   └── tests/                # Extension tests
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## Run locally

You need **Python 3.10+**, **Google Chrome** and a **Gemini API key**. From the project root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` if you do not already have one:

```sh
cp .env.example .env
```

Add your key to `.env`:

```dotenv
GEMINI_API_KEY=your_key_here
GEMINI_MODEL=gemini-3.5-flash-lite
```

Start the backend and keep the terminal open:

```sh
python3 backend/local_server.py
```

Then load the extension:

1. Open `chrome://extensions` and enable **Developer mode**.
2. Click **Load unpacked** and select the **extension** folder.
3. Open DeTect → **Connection**, paste the connection code from the terminal and click **Connect**.
4. Paste a profile URL or `@username@server`, or choose **Use current tab**, then click **Analyse**.

The progress indicator follows three steps: **Reading followers → Analysing patterns → Writing explanation**. Restarting the backend creates a new connection code.

### Configuration

The backend runs at `127.0.0.1:8765`. Both the server and command-line pipeline load `.env` from the project root. On macOS, you can also start the server with `backend/Start DeTect.command`.

You can save a Gemini key through the extension’s **Connection** dropdown. For the server, settings saved in `backend/history/.detest/gemini.json` override terminal environment variables and `.env`. An explicit `--model` option overrides the configured model.

The core app uses Python’s standard library. Matplotlib is included for the optional `backend/plot_benford.py` chart exporter.

Your key stays on the backend. Gemini receives summary statistics and hypothesis-test results, without individual follower records or biographies. `.env`, saved history and generated reports are ignored by Git.

## What the analysis measures

| Analysis | Purpose |
| --- | --- |
| Benford first digits | Compare follower-count first digits with the Benford reference pattern. |
| Following imbalance | Find accounts following many more people than follow them. |
| Activity, age and profile completeness | Measure low activity, recently created accounts and sparse profiles. |
| Follower-count distribution | Describe the spread of follower counts using quartiles, IQR and log-scale standard deviation. |
| Bootstrap intervals | Check how much results vary when the sample is resampled. |
| Hypothesis tests | Compare patterns with stated reference models, adjusting for multiple tests. |

The bot-risk score uses these weights:

| Indicator | Weight |
| --- | --- |
| Benford deviation | 35% |
| Following imbalance | 25% |
| Low activity | 20% |
| New accounts | 10% |
| Incomplete profiles | 10% |

If an indicator lacks usable data, its weight is redistributed across the available indicators. Risk labels are **low** up to 30, **moderate** up to 60, **high** up to 80 and **very high** above 80. Too little data produces an unavailable result.

The **stats analysis** dropdown shows:

- **Goodness of fit score (χ²):** how far the first-digit pattern differs from the Benford reference. Lower means closer agreement.
- **Adjusted p-value:** the goodness-of-fit test’s p-value after Holm correction.
- **Following imbalance:** the percentage of followers with an unusually high following-to-follower ratio.
- **Low-activity followers:** the percentage with few posts.

Percentages use followers with known values. Tests that cannot be calculated show **Not available**. The χ² statistic is separate from the overall bot-risk score.

The score uses manually chosen hackathon rules. It is not a probability or proof that an account is fake. Public follower samples may be incomplete, and legitimate accounts can show the same patterns. Hypothesis tests evaluate their stated references; they do not identify bots.

## Command-line tools

Run these commands from the project root:

```sh
# Collect and analyse an audience
python3 backend/run_pipeline.py collect '@username@server' --limit 300

# Import an existing collection
python3 backend/run_pipeline.py import /path/to/collection

# Update comparisons across saved accounts
python3 backend/run_pipeline.py compare

# Explain a saved report without collecting again
python3 backend/run_pipeline.py explain '@username@server'

# Preview the prompt without calling Gemini
python3 backend/run_pipeline.py explain '@username@server' --prepare-only
```

Commands use `backend/history/` by default. Pass `--store` to use another location, or `--help` to see options.

The collector supports Mastodon profiles with accessible follower lists. Instagram and bridged Bluesky profiles are unsupported. Direct command-line collection can use an exported `MASTODON_ACCESS_TOKEN` if the instance requires one; the `.env` loader reads Gemini settings only.

## Saved data

New collections merge with existing records for the same target account. Each follower’s newest record is kept, and statistics are recalculated. Different target accounts stay separate; identical imports reuse the existing run.

**Snapshot reports** describe one collection. **Cumulative reports** describe all followers observed over time, which may differ from the current follower list.

All paths below are inside `backend/history/`:

| Path | Contents |
| --- | --- |
| `history.sqlite3` | Saved records and calculated results |
| `collections/` | Collected API data |
| `runs/<run_id>/snapshot/` and `cumulative/` | Statistical reports |
| `comparison.md` and `comparison.json` | Account comparisons |
| `extension_results/` | Extension results and Gemini explanations |
| `explanations/` | Detailed command-line explanations |

Keep the entire history folder when moving the project. If Gemini fails, the statistics remain saved. **Retry explanation** reuses them while the backend is running.

## Research tools

```sh
# Test the scoring rules on simulated audiences
python3 backend/run_pipeline.py validate

# Compare shared followers across saved accounts
python3 backend/run_pipeline.py network
```

Validation writes an interactive report to `backend/experiments/`. Network analysis needs at least two saved accounts and writes overlap measures to `backend/history/network/`. These tools do not change the score or establish bot activity.

Scoring options are in `backend/config.example.json`. Hypothesis-test settings are in `backend/hypotheses.example.json`.

## Tests

```sh
PYTHONPATH=backend python3 -m unittest discover -s backend/tests
node extension/tests/test_popup.mjs
```

Python tests use temporary data and simulated Gemini responses, so no API key is needed. Node.js is required only for the extension tests, which simulate popup interactions without opening Chrome.
