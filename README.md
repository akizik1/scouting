# Scout Finder

**Filters in, watch list out.** A desktop app for football scouts.

**Free to run.** By default it uses the Google Gemini free tier (no credit card) and DuckDuckGo search. You can also run the AI fully offline with Ollama, or switch to Claude (paid) for the best briefs.

Set what you are looking for: position, age, budget, leagues, nationality, foot, height, minutes, contract and stat thresholds. Scout Finder searches free football databases for real players who pass, reads open match reports, scouting blogs and fan forums, and gives you a short watch list:

- **3 strengths and 3 risks** per player, every claim with its source links and a confidence note (how many independent sources agree; fan-only claims are always *Low*),
- **what to check on video** in the next match,
- a **signal** (🟢 Watch now · 🟡 Monitor · 🔴 Skip) with the reason, and an honest **data quality** label when information is thin,
- the **next fixture** and where to watch it.

Pick the players you want to follow and add them to a simple **Notion kanban board** with one click (optional).

> The AI prepares the list. The scout watches and decides.

![Scout Finder (example data)](docs/screenshot.png)

## Start it (double-click)

You need **Python 3.10+**. If it is missing, the launcher opens the download page (on Windows, tick *Add python.exe to PATH* while installing).

1. **Code → Download ZIP** on GitHub, unzip, put the folder anywhere (e.g. Desktop).
2. Double-click:
   - **macOS:** `Start Scout Finder.command`. The first time, macOS may say the developer is unidentified: right-click the file → **Open** → **Open**. Only once.
   - **Windows:** `Start Scout Finder.bat`. If SmartScreen appears: **More info → Run anyway**.
3. The first start takes about a minute (it installs what it needs). Then the app opens in its own window. Keep the small terminal window open; closing it quits the app.
4. Get a free Gemini API key: go to https://aistudio.google.com/apikey, sign in with a Google account, **Create API key**. No credit card.
5. In **Settings**, paste the key. Keys are stored only on your computer, in a `Scout Finder` folder in your home directory.

Tip: make a desktop shortcut. macOS: right-click → *Make Alias*. Windows: right-click → *Send to → Desktop (create shortcut)*.

## Use it

1. Set filters on the left. Quick chips for common choices (U21, ≤ €500k, leagues, 900+ minutes, stat thresholds), or type your own.
2. **Find players.** Progress shows step by step, usually under a minute per player.
3. Read the cards. Filter by signal, sort by age, price or data quality.
4. **＋ Watchlist** on a card sends that player to Notion; **Add all 🟢** sends every *Watch now* player. **Report** downloads the list as Markdown.
5. Every search is saved. Open old ones from **Saved searches**.

## Notion watchlist (optional)

1. In Notion, create a page with a database called **Watchlist**. Suggested properties: `Status` (status: To watch, Watching, Shortlist, Passed), `Signal`, `Position`, `Club`, `League`, `Nationality`, `Date of birth`, `Foot`, `Market value (€)`, `Data`, `Next match`, `Stats`, `Profile`, `My notes`. Add a **Board** view grouped by `Status`.
   The app only writes properties that exist, so you can keep it smaller.
2. https://www.notion.so/profile/integrations → **New integration** (Internal) → copy the secret.
3. On your Watchlist page: `•••` → **Connections** → add the integration.
4. Paste the secret into **Settings → Notion token**.

New players land in **To watch** with the full AI brief inside the card. If a player is already on the board, the card is refreshed but never moved.

## Command line (optional)

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=...               # NOTION_TOKEN too if you use --notion
python scout_finder.py --position LW,RW --age-max 21 --foot Left --max-value 500000 \
  --leagues "Brazil Série B, Ghana" --min-minutes 900 \
  --criteria "G+A per 90 >= 0.4; dribble success >= 50%" --must-haves "pace in transition"
python scout_finder.py --from-json watchlist.json --notion   # add a saved list to Notion
# other providers: --provider ollama   or   --provider claude (needs ANTHROPIC_API_KEY)
```

## AI providers

| Provider | Cost | Setup | Notes |
|---|---|---|---|
| **Google Gemini** (default) | Free tier | Free key from aistudio.google.com/apikey | Picks a Flash-Lite model automatically. Rate limits apply (per minute and per day), enough for a few dozen player briefs a day. When the limit is hit, the app waits and retries. **On the free tier Google may use your prompts and answers to improve its products, and human reviewers may read them.** Don't put private information in the filters. |
| **Ollama** | Free, offline | Install ollama.com, then `ollama pull qwen3:8b` | Runs on your computer, nothing leaves it except the web searches. Slower, needs about 16 GB RAM. Smaller models make more mistakes. |
| **Claude** | Paid (Anthropic API) | console.anthropic.com key | Reads more sources with its own web search, best briefs. Roughly $1–3 for a 5-player search. |

Free limits change. Check Google's current numbers at https://ai.google.dev/gemini-api/docs/rate-limits.

## How it works

- `scout_finder.py` does the work.
  - **Free providers (Gemini, Ollama):** the app searches DuckDuckGo, downloads the pages and gives their text to the model with numbered sources. The model may only use those sources. Afterwards the app removes any link the model did not actually read and lowers the confidence of claims that are left with fewer sources. Answers are checked against a strict schema (Pydantic) and retried once if they don't fit.
  - **Claude:** Claude uses server-side **web search** and **web fetch** to find candidates and read sources, then a second step turns the notes into the same schema. That step may not add facts that are not in the notes.
- `app.py` starts a small local server and opens `ui.html` in a Chrome/Edge app window (or your browser). Nothing is hosted online.

## Honest limits

- It is a **watch-list tool, not a decision tool**. Stats and opinions decide what to watch, never who to sign.
- Fan forums are noisy and biased. That is why each claim shows how many independent sources back it.
- Lower leagues have little public data. The app says what is missing instead of guessing.
- The model can misread a source. Every claim links to its source so you can check.
- Free models are weaker than paid ones, and DuckDuckGo can rate-limit many searches in a row. If a search returns nothing, wait a minute and try again.
- Some sites (e.g. Transfermarkt) sometimes block automatic page reading. The app then uses the search snippet and says the data is thin.
