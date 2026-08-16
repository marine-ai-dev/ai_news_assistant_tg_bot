# 🚀 Running this on a server

Setup for **Oracle Cloud Always Free**, Ubuntu, systemd, no Docker. One small VM runs
the review bot, the scheduler and a collection timer, with SQLite and media on the
boot volume.

---

## ⚠️ First, what a server can and cannot do here

Worth being exact about this before provisioning anything, because it decides what you
are actually building.

| | On the server | Why |
|---|---|---|
| 📥 Collecting and deduplicating news | ✅ automatic, four times a day | Deterministic Python |
| ✍️ Evaluating stories and writing posts | ❌ **stays on your machine** | There is **no LLM API in this project**. The editorial layer is a Claude Code session exchanging JSON files with the application |
| 📱 Reviewing and approving from your phone | ✅ the bot runs continuously | |
| 📅 Scheduling a post | ✅ from the bot or the CLI | |
| 📣 Publishing at the scheduled time | ✅ automatic | But **only** content you already approved *and* queued yourself |

So the working rhythm is: **the server collects and publishes; you write and approve.**
The bot exists so approving does not require a laptop.

This is deliberate, not a limitation waiting to be removed. Removing it would mean a
machine choosing what a real audience reads.

---

## 🖥 1. The instance

In the Oracle Cloud console, create a Compute instance:

- **Shape: `VM.Standard.E2.1.Micro`** (AMD x86, 1 OCPU, 1 GB) — Always Free eligible.
  Prefer this over the Ampere ARM shapes: every dependency here has x86 wheels, and
  ARM occasionally means building `selectolax` or `pydantic-core` from source on a
  1 GB machine.
- **Image:** Ubuntu 22.04 or 24.04 LTS
- **Boot volume:** the default 50 GB is far more than this needs
- Add your SSH public key

Nothing needs an inbound port. The bot **polls** Telegram rather than receiving
webhooks, so no ingress rule, no domain, no TLS certificate, no reverse proxy.

> 💾 1 GB of RAM is enough — two Python processes and SQLite. It is not enough to
> compile large wheels, which is the real reason for choosing x86.

---

## 🧰 2. Prepare the machine

```bash
sudo apt update && sudo apt install -y python3.11 python3.11-venv git sqlite3 tzdata

# The application always resolves publication times in Europe/Kyiv explicitly, so this
# is only so journal timestamps and the collection timer read the way you think.
sudo timedatectl set-timezone Europe/Kyiv
```

A dedicated unprivileged user, and directories that survive a redeploy:

```bash
sudo useradd --system --create-home --home-dir /opt/ai-news --shell /usr/sbin/nologin ainews
sudo mkdir -p /opt/ai-news/{app,data,media} /etc/ai-news
sudo chown -R ainews:ainews /opt/ai-news
```

| Path | Holds | Must survive redeploys |
|---|---|---|
| `/opt/ai-news/app` | the checkout and its virtualenv | no |
| `/opt/ai-news/data` | **SQLite + WAL + backups** | **yes** |
| `/opt/ai-news/media` | **post images and PDFs** | **yes** |
| `/etc/ai-news/env` | **the bot token** | yes |

> 🖼 `media/` matters more than it looks. A scheduled post whose image is missing is
> **held for review instead of published** — correct behaviour, and a confusing one to
> debug if the directory quietly went away.

---

## 📦 3. Install

```bash
sudo -u ainews git clone https://github.com/marine-ai-dev/ai_news_assistant_tg_bot.git /opt/ai-news/app
cd /opt/ai-news/app
sudo -u ainews python3.11 -m venv .venv
sudo -u ainews .venv/bin/pip install -e .
```

---

## 🔐 4. Configuration

```bash
sudo install -o ainews -g ainews -m 600 /dev/null /etc/ai-news/env
sudo -e /etc/ai-news/env
```

```ini
# Absolute paths: systemd sets no working directory you should rely on for data.
AI_NEWS_DATA_DIR=/opt/ai-news/data
AI_NEWS_MEDIA_DIR=/opt/ai-news/media

AI_NEWS_ENVIRONMENT=production
# Structured logs, so journalctl -o json is useful. The redaction filter keeps the
# token out of them either way.
AI_NEWS_LOG_FORMAT=json
AI_NEWS_LOG_LEVEL=INFO

# From @BotFather. This is the only secret this application has.
AI_NEWS_TELEGRAM_BOT_TOKEN=
# Where approved posts go: @username or a numeric chat id.
AI_NEWS_TELEGRAM_CHANNEL=
# The single account allowed to use the review bot. Without it the bot exits at
# startup. Find yours with: ai-news telegram whoami
AI_NEWS_TELEGRAM_OWNER_USER_ID=
```

`chmod 600`, owned by `ainews`. The deploy script warns if it is anything else.

> 🚫 Do **not** set `AI_NEWS_AUTO_PUBLISH_ENABLED`. There is no code path behind it and
> the application refuses to start if it is true. It exists so its absence is testable.

Then create the database and check the setup:

```bash
cd /opt/ai-news/app
sudo -u ainews .venv/bin/ai-news db init
sudo -u ainews .venv/bin/ai-news doctor          # local only, no network
sudo -u ainews .venv/bin/ai-news telegram doctor # read-only: getMe, getChat, rights
```

`doctor` exits **1** if anything is wrong, including a stale schema or a half-finished
Telegram configuration — it will tell you specifically that the review bot cannot start
if the owner id is missing.

---

## ⚙️ 5. Install the services

```bash
sudo cp /opt/ai-news/app/deploy/systemd/*.service /etc/systemd/system/
sudo cp /opt/ai-news/app/deploy/systemd/*.timer   /etc/systemd/system/
sudo systemctl daemon-reload

sudo systemctl enable --now ai-news-bot.service
sudo systemctl enable --now ai-news-scheduler.service
sudo systemctl enable --now ai-news-collect.timer
```

| Unit | What it does |
|---|---|
| `ai-news-bot.service` | The private review bot. Long-poll, restarts on failure |
| `ai-news-scheduler.service` | Publishes due queue items. Sleeps until the next one |
| `ai-news-collect.timer` | Runs `collect` + `process` at 07:30, 11:30, 15:30, 19:30 |

Check it:

```bash
systemctl status ai-news-bot ai-news-scheduler
systemctl list-timers ai-news-collect.timer
journalctl -fu ai-news-bot -u ai-news-scheduler
```

> 📵 **Exactly one review bot may run.** Telegram allows one `getUpdates` poller per
> token and answers a second with `409 Conflict`. Do not run `ai-news telegram
> review-bot` by hand while the service is up — stop the service first.

---

## 🔄 6. Updating

```bash
sudo -u ainews /opt/ai-news/app/deploy/update.sh
```

The order is the point:

```
stop services → git pull → pip install → db migrate → doctor → start services
                                              ↓          ↓
                                          must pass  must pass
```

**If `db migrate` or `doctor` fails, the services stay down.** A bot running against a
schema it does not understand is worse than a bot that is off: the failure is loud, the
database is untouched, and the backup taken moments earlier is the state before the
attempt. Ten backups are kept in `data/`.

Update and verify without starting anything:

```bash
sudo -u ainews /opt/ai-news/app/deploy/update.sh --no-restart
```

Migrations are **never** applied by starting a service. A process that changes the
schema as a side effect of being restarted is a process that can change the schema at
three in the morning because something crashed.

---

## 🔁 7. What happens across restarts

Everything here is designed for a machine that reboots, so nothing needs care:

- 🗄 **SQLite** is in WAL mode with foreign keys on. The `.sqlite3`, `-wal` and `-shm`
  files live together in `data/`.
- ⏰ **Overdue posts are not blasted out.** After downtime the scheduler applies the
  overdue policy: a short delay publishes normally, a long one is **held for review**.
  News is stricter than an explainer. See `ai-news queue policy`.
- 🔒 **A worker that died mid-send does not republish.** Its lease expires, the item is
  reassessed from scratch, and anything whose outcome is unknown is held for a person.
- 📱 **The bot discards its backlog on start.** Button taps that arrived while it was
  down are confirmed and ignored rather than acted on — a tap on a card from last
  Tuesday should not approve anything today.
- 📥 **Collection catches up.** `Persistent=true` means a missed 07:30 run happens when
  the machine comes back, rather than waiting for 11:30.

---

## 🩺 8. When something is wrong

```bash
sudo -u ainews /opt/ai-news/app/.venv/bin/ai-news doctor        # local health
sudo -u ainews /opt/ai-news/app/.venv/bin/ai-news telegram doctor
sudo -u ainews /opt/ai-news/app/.venv/bin/ai-news queue list    # what is scheduled
sudo -u ainews /opt/ai-news/app/.venv/bin/ai-news scheduler once --dry-run  # decides, sends nothing
journalctl -u ai-news-bot -n 100 --no-pager
```

| Symptom | Cause |
|---|---|
| Bot exits immediately | `AI_NEWS_TELEGRAM_OWNER_USER_ID` empty — `doctor` says so |
| Both units restart-loop after an update | Migrations pending. Run `update.sh`, which applies them in the right order |
| `409 Conflict` in the log | A second poller somewhere. Only one bot per token |
| A queued post did not go out | `ai-news queue show <id>` — the hold reason is in plain words |
| Post held for a missing image | `media/` is not where the approved bundle expects it |

---

## 💾 9. Backing up

Everything that matters is three paths:

```bash
sudo -u ainews sqlite3 /opt/ai-news/data/ai_news.sqlite3 ".backup '/tmp/ai-news.sqlite3'"
sudo tar czf ai-news-backup.tar.gz -C /opt/ai-news media -C /tmp ai-news.sqlite3
sudo cp /etc/ai-news/env ./env.backup   # contains the token — treat it as a credential
```

Use `.backup` rather than copying the file: it is safe while the WAL exists and gives
you a single consistent file.

---

## 🔭 What is deliberately not here

No Docker, no reverse proxy, no webhook endpoint, no message broker, no external
database, no metrics stack, and no inbound port. One person, one channel, one small
machine. Adding any of it would be infrastructure with nothing behind it.
