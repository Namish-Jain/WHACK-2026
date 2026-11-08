# DeTect

DeTect is a local Chrome extension that analyses a Mastodon profile's public followers and explains unusual audience patterns in plain language. Python computes the statistics and bot-risk score; Gemini writes the short explanation.

Each analysis reads up to 300 public followers. The extension shows **Reading followers → Analysing patterns → Writing explanation**, followed by a score out of 100, a green-to-red scale and a short explanation.

## Project layout

```text
DeTect/
├── backend/
│   ├── local_server.py       # Local API for the extension
│   ├── run_pipeline.py       # Collection, history and research commands
│   ├── collect_mastodon.py   # Mastodon API collector
│   ├── public_transport.py   # Public connections and redirect handling
│   ├── history.py            # SQLite storage and follower deduplication
│   ├── analyze.py            # Standalone analysis and report rendering
│   ├── analysis/             # Statistics, scoring and hypothesis tests
│   ├── llm/                  # Gemini integration, settings and prompts
│   ├── models/               # Follower data model
│   └── tests/                # Python tests and aggregate regression fixture
├── extension/
│   ├── manifest.json
│   ├── popup.html
│   ├── popup.css
│   ├── popup.js
│   ├── logic.mjs
│   └── tests/                # Popup interaction tests
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

Local data is created in `backend/history/`. It is ignored by Git, along with `.env`, generated reports and caches.

## Run locally

Requires **Python 3.10+** and **Google Chrome**. Run these commands from the project root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
```

If you already have a `.env`, keep it instead of running the copy command. Edit it locally:

```dotenv
GEMINI_API_KEY=your_key_here
GEMINI_MODEL=gemini-3.5-flash-lite
```

Start the backend:

```sh
python3 backend/local_server.py
```

Keep that terminal open. The server listens on `127.0.0.1:8765` and prints a connection code. On macOS, `backend/Start DeTect.command` also starts it using `python3`.

1. Open `chrome://extensions` and enable **Developer mode**.
2. Choose **Load unpacked**, then select this project's **extension** folder.
3. Open DeTect, expand **Connection**, paste the terminal's connection code and select **Connect**.
4. Paste a profile URL or `@username@server`, or select **Use current tab**, then **Analyse**.

When moving from an older layout, remove the old unpacked extension and load `extension/` from its new location. Restarting the backend generates a new connection code.

### Configuration

The server and pipeline CLI read `.env` from the project root, regardless of the directory they are launched from. The core application uses Python's standard library. Matplotlib, the only listed dependency, supports the optional `backend/plot_benford.py` chart exporter.

You can also save a key through **Connection**. For the extension server, saved settings in `backend/history/.detest/gemini.json` take precedence over terminal environment variables, which take precedence over `.env`. To switch entirely to `.env`, remove only that saved settings file and restart. An explicit `--model` option takes precedence over other model settings.

Credentials stay on the Python side. They are not stored in Chrome, sent in URLs or written to statistical reports. Gemini receives aggregate evidence, including hypothesis-test results, rather than follower rows or biographies. Never commit your real `.env` or runtime history.

## Statistical analysis

| Analysis | What it measures |
| --- | --- |
| Benford first digits | Positive follower-count first digits compared with a Benford reference, with sample-size and range checks. |
| Following imbalance | Accounts following many more people than follow them, using smoothed ratios. |
| Activity, age and completeness | Low posting activity, recent creation and sparse profiles, retaining missing values as unknown. |
| Follower-count distribution | Quartiles, IQR, outliers and standard deviation of `ln(count + 1)`. |
| Bootstrap intervals | Variation when resampling the observed followers. |
| Hypothesis tests | Benford chi-square goodness of fit and optional binomial tests against supplied reference rates, with Holm adjustment. |

The score combines Benford (35%), following imbalance (25%), low activity (20%), new accounts (10%) and incomplete profiles (10%). Unusable components are excluded and remaining weights are renormalised. Labels are low risk up to 30, moderate up to 60, high up to 80 and very high above 80. Insufficient data remains unavailable.

The weights and labels are hackathon heuristics, not a trained bot classifier. The score is not a probability and cannot establish whether the profile owner or its followers are fake. Public samples may be unrepresentative; a Benford mismatch alone is not evidence of manipulation. Hypothesis tests assess their stated references, not whether someone is a bot.

## Pipeline and saved data

From the project root:

```sh
# Collect and analyse a public audience
python3 backend/run_pipeline.py collect '@username@server' --limit 300

# Import an existing collector export
python3 backend/run_pipeline.py import /path/to/collection

# Rebuild comparison exports
python3 backend/run_pipeline.py compare

# Explain a saved report without recollecting
python3 backend/run_pipeline.py explain '@username@server'

# Inspect the prompt without calling Gemini
python3 backend/run_pipeline.py explain '@username@server' --prepare-only
```

Commands share `backend/history/`; use `--store` to select another persistent directory. Some Mastodon servers hide followers, require authentication or are unreachable. Bridged Bluesky and Instagram profiles are unsupported. Direct CLI collection supports an exported `MASTODON_ACCESS_TOKEN` for the chosen instance; the `.env` loader reads Gemini settings only.

Collections merge records by stable follower identity, retaining the newest observation. Scores are recomputed from records rather than averaged. Accounts remain separate and identical imports reuse their run. Snapshots describe one collection; cumulative reports describe everyone observed across collections, not the current follower list.

| Local output | Contents |
| --- | --- |
| `backend/history/history.sqlite3` | Authoritative history and computed reports |
| `backend/history/collections/` | Collected API exports |
| `backend/history/runs/<run_id>/snapshot/` and `cumulative/` | Statistical reports and data |
| `backend/history/comparison.md` and `comparison.json` | Latest account comparisons |
| `backend/history/extension_results/` | Extension results and Gemini artifacts |
| `backend/history/explanations/` | Detailed CLI explanations |

Keep the whole history directory when moving the project. A Gemini failure preserves statistics; **Retry explanation** reuses them while the backend remains running.

## Research tools

```sh
python3 backend/run_pipeline.py validate
python3 backend/run_pipeline.py network
```

Validation runs synthetic scenarios and writes an interactive report to `backend/experiments/` without altering collected history. Network analysis compares latest snapshots using shared counts, Jaccard similarity and overlap coefficients. It needs at least two accounts and writes to `backend/history/network/`. Neither tool trains the score or proves bot activity.

`backend/config.example.json` documents scoring settings. `backend/hypotheses.example.json` is the starting point for explicitly justified hypothesis references. Use `--help` on each command for options.

## Tests

From the project root:

```sh
PYTHONPATH=backend python3 -m unittest discover -s backend/tests
node extension/tests/test_popup.mjs
```

Node.js is needed only for JavaScript tests. Python tests use temporary data and mocked Gemini requests; no real API key is needed. Popup tests use DOM doubles, not a loaded Chrome extension. The aggregate regression fixture preserves numerical checks without follower rows or original account/path identifiers.

## Troubleshooting

- **Offline or connection rejected:** start the backend and reconnect with the code from that terminal. Restarting changes the code.
- **Port already in use:** stop the old server before starting another.
- **Gemini timeout, quota or unavailable model:** check the specific error, model access and quota, then retry. Model availability can change.
- **Changed `.env`:** restart the backend. A saved Connection key can override it.
- **Old extension layout:** reload after code changes; load the new folder if it moved.
