# Deploy to Render (view on your mobile)

Your code is now Render-ready. Files added: `render.yaml`, `Procfile`,
`runtime.txt`, and `tzdata` in `requirements.txt` (so IST timezone works on
Render's Linux servers — without it the 09:45–15:00 trade window would break).

## One-time setup

1. **Push the code to GitHub** (the `.env` is gitignored — your Dhan token will
   NOT be uploaded, which is correct):
   ```bash
   git add -A
   git commit -m "Render deploy config"
   git branch -M main
   git remote add origin https://github.com/<you>/smc-trader.git   # create the repo first
   git push -u origin main
   ```

2. **Create the service on Render**
   - Go to https://dashboard.render.com → **New +** → **Blueprint**
   - Connect your GitHub repo → Render reads `render.yaml` automatically
   - Click **Apply**

3. **Add your two secrets** (Render dashboard → your service → **Environment**):
   - `DHAN_CLIENT_ID` = `1110569990`
   - `DHAN_ACCESS_TOKEN` = `<your current Dhan token>`
   - Save → it redeploys. (Everything else is already set from `render.yaml`.)

4. **Open the URL** Render gives you (e.g. `https://smc-trader.onrender.com`) on
   your phone. Bookmark it / add to home screen for fast access.

## Important — read before you rely on it

- **Free plan sleeps after 15 min idle.** While the dashboard tab is **open**,
  the live SSE stream keeps it awake and the paper bot keeps running. If you
  **close the tab / lock the phone for 15+ min**, the service sleeps and the
  background paper-trading thread STOPS. Next visit cold-starts (~30–50s) but
  the bot won't have traded while asleep.
  - For tomorrow's test: keep the tab open, or upgrade to the **Starter plan
    ($7/mo)** which is always-on. For real money later, always-on is required.

- **Dhan token expires** (it's a short-lived JWT). When data stops loading,
  generate a fresh token in Dhan and update `DHAN_ACCESS_TOKEN` in Render's
  Environment tab (or via the Settings tab in the app) — no redeploy of code
  needed.

- **`LIVE_TRADING=false`** is set — it's paper-only. Do not flip it on Render
  until you've proven paper results and moved to an always-on plan.

- **Region** is set to `singapore` (closest Render offers to India). Latency to
  Dhan is fine for a 3-minute strategy.

## Start the paper bot
Open the deployed site → **Paper Trading** tab → set capital → **Start**.
Watch it live on your phone. Numbers update automatically (SSE, no refresh).
