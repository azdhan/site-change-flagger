# 🔔 Govt Website Change Detector

A **zero-cost, fully automated** system that watches government websites and
alerts you when anything changes — a new notice, a new PDF, updated text.
No AI, no servers, no subscriptions. It runs entirely on GitHub's free tier.

## How it works

```
GitHub Actions (free cron, every 3 hours)
   └─ check.py fetches every site in sites.yaml
        ├─ static HTTP fetch; JavaScript-heavy portals are rendered
        │  in headless Chromium automatically
        ├─ compares against each site's "memory" of recently seen content
        ├─ suspected changes are confirmed with a second fetch
        │  (rotating banners / bilingual flips don't cause false alarms)
        └─ on change: updates
             ├─ docs/index.html  → public dashboard (GitHub Pages)
             ├─ docs/feed.xml    → RSS feed
             └─ alert.txt        → optional email (Gmail, free)
```

- **New content** (never seen before) is reported immediately, with the URL
  of every new link so you can jump straight to the new notice/PDF.
- **Removed content** is reported only after being absent for 3 consecutive
  checks, so temporary glitches never false-alarm.
- Bot-protection pages, gateway errors, and cookie banners are recognized
  and ignored.

## One-time setup (~10 minutes)

### 1. Create a GitHub repository and push this folder

Create a **public** repo (public = unlimited free Actions minutes + free
Pages) at <https://github.com/new>, then:

```bash
git init
git add .
git commit -m "Website change detector"
git branch -M main
git remote add origin https://github.com/<YOUR-USERNAME>/<REPO-NAME>.git
git push -u origin main
```

### 2. Enable the dashboard (GitHub Pages)

Repo → **Settings → Pages** → under *Build and deployment*:
- Source: **Deploy from a branch**
- Branch: **main**, folder: **/docs** → Save

Your public dashboard will be live at
`https://<YOUR-USERNAME>.github.io/<REPO-NAME>/` within a minute or two.
Anyone with the link can see it.

### 3. Enable workflow permissions

Repo → **Settings → Actions → General → Workflow permissions** →
select **Read and write permissions** → Save.
(The bot needs this to commit detected changes back to the repo.)

### 4. Test it

Repo → **Actions** tab → *Check websites for changes* → **Run workflow**.
After it finishes, the dashboard shows the status of every site.
From then on it runs automatically every 3 hours.

## Getting alerts

Pick any (or all) of these — all free:

| Channel | Setup |
|---|---|
| **Dashboard** | Nothing — just bookmark your GitHub Pages URL |
| **RSS** | Point any feed reader at `https://<user>.github.io/<repo>/feed.xml` |
| **RSS → Email** | Paste the feed URL into [Blogtrottr](https://blogtrottr.com) (free) — emails you on every new entry |
| **Direct email** | Add 3 repo secrets (below) |
| **GitHub native** | Click **Watch → All activity** on the repo — GitHub notifies you on every change commit |

### Direct email via Gmail (optional)

1. Create a Gmail **App Password**: <https://myaccount.google.com/apppasswords>
   (requires 2-step verification on your Google account).
2. Repo → **Settings → Secrets and variables → Actions** → add:
   - `MAIL_USERNAME` — your Gmail address
   - `MAIL_PASSWORD` — the 16-character app password
   - `MAIL_TO` — where alerts go (can be the same address)

That's it. When nothing changes, no email is sent.

## Adding more websites (the 5 → 60 plan)

Edit [sites.yaml](sites.yaml) and add two lines per site:

```yaml
  - name: Ministry of Home Affairs
    url: https://www.mha.gov.in/
```

Commit and push (or edit directly on github.com — the *Edit* pencil button).
The next scheduled run creates a baseline automatically; changes are
reported from the run after that. Nothing else to configure.

**Tip:** you can monitor any page, not just homepages — a ministry's
"Press Releases" or "What's New" page is often a better signal than its
homepage. Add both if you like; each entry is independent.

## Tuning

- **Check frequency**: edit the `cron:` line in
  [.github/workflows/check.yml](.github/workflows/check.yml).
- **False alarms from a specific site**: add a regex to `ignore_patterns`
  in [sites.yaml](sites.yaml) matching the noisy text.
- **A site that needs browser rendering** is detected automatically, but you
  can force it with `render: true` under that site's entry.

## Notes & limitations

- Some govt sites serve Hindi and English alternately. The system remembers
  both variants, so the first day may produce a few "new text" alerts for
  interface strings; it goes quiet on its own after the memory warms up.
- If a site blocks GitHub's cloud IPs entirely, its tile shows
  "unreachable" on the dashboard rather than producing false alerts.
- Only page-level changes are detected (new/changed/removed text and links).
  The contents of PDFs are not read — but new PDF links are exactly what
  gets reported, with clickable URLs.

## Running locally (optional)

```bash
pip install -r requirements.txt
python -m playwright install chromium
python check.py
```
