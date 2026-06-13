# Value Metrics Web (React)

## Dev

Backend (from repo root):

```bash
.venv/bin/python backend/main.py
```

Frontend:

```bash
cd value_web
cp .env.example .env
npm run dev
```

Then open the Vite URL (prints in terminal).

## Interesting stocks (MVP)

Nav: **Stocks** (`/stocks`) — universe table, add tickers, **read-only** coverage gaps. Backfills are **not** triggered from the UI.

**Daily backfill script** (schedule via cron):

```bash
# From repo root
.venv/bin/python backend/interesting_stocks_daily_backfill.py
.venv/bin/python backend/interesting_stocks_daily_backfill.py --dry-run   # gap summary only

# Cron example (6:00 UTC daily)
# 0 6 * * * cd /path/to/market_analysis && .venv/bin/python backend/interesting_stocks_daily_backfill.py >> logs/interesting_stocks_backfill.log 2>&1
```

The script: seeds interesting stocks → incremental news ingest + universe preprocess → fills gaps (prices, fundamentals, news, analyst ratings) using the telegram_agent / value_metrics pipelines.

**API (read + manage list only):**

- `GET /value/interesting/stocks` — list + 2y coverage gaps
- `POST /value/interesting/stocks` body: `{"symbol":"NVDA"}`
- `DELETE /value/interesting/stocks/{symbol}`
- `GET /value/interesting/stocks/{symbol}/detail` — prices, metrics, news, research recommendations

Ticker detail: `/stocks/AAPL`

## API

- `GET /value/metrics?symbols=AAPL,MSFT`
- `GET /value/watchlist/{user_id}`
- `POST /value/watchlist/{user_id}/add` body: `{"symbols":["AAPL"]}`
- `POST /value/watchlist/{user_id}/remove` body: `{"symbols":["AAPL"]}`
- `GET /value/alerts/{user_id}`
- `POST /value/alerts/{user_id}/create` body: `{"symbol":"AAPL","metric":"pe","op":"lt","threshold":15}`

# React + TypeScript + Vite

This template provides a minimal setup to get React working in Vite with HMR and some ESLint rules.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Oxc](https://oxc.rs)
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/)

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the ESLint configuration

If you are developing a production application, we recommend updating the configuration to enable type-aware lint rules:

```js
export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...

      // Remove tseslint.configs.recommended and replace with this
      tseslint.configs.recommendedTypeChecked,
      // Alternatively, use this for stricter rules
      tseslint.configs.strictTypeChecked,
      // Optionally, add this for stylistic rules
      tseslint.configs.stylisticTypeChecked,

      // Other configs...
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])
```

You can also install [eslint-plugin-react-x](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-x) and [eslint-plugin-react-dom](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-dom) for React-specific lint rules:

```js
// eslint.config.js
import reactX from 'eslint-plugin-react-x'
import reactDom from 'eslint-plugin-react-dom'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...
      // Enable lint rules for React
      reactX.configs['recommended-typescript'],
      // Enable lint rules for React DOM
      reactDom.configs.recommended,
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])
```
