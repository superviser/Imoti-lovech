# Имоти Ловеч

Уеб приложение което показва всички обяви за продажби в град Ловеч от [imot.bg](https://www.imot.bg/obiavi/prodazhbi/grad-lovech), обновявани автоматично на всеки 30 минути.

## Локално стартиране

```bash
# Първоначално генериране на data.json
python scraper.py

# Сервиране на статичния сайт
python -m http.server 8000 -d public
# Отвори http://localhost:8000
```

## Deploy (100% безплатно)

### 1. GitHub
- Създай нов GitHub repo и push-ни този директорий
- GitHub Actions ще започне cron-а (`*/30 * * * *`) автоматично

### 2. Cloudflare Pages
- Cloudflare Dashboard → Workers & Pages → **Create application** → **Pages** → **Connect to Git**
- Избери repo-то → Save
- Build settings:
  - **Build command**: *(оставете празно)*
  - **Build output directory**: `public`
- Save and Deploy

Всеки път когато GitHub Action commit-не нов `public/data.json`, Cloudflare Pages auto-deploy-ва за ~30 секунди.

## Архитектура

```
GitHub Actions (cron) → scraper.py → public/data.json → git push
                                                         ↓
                                          Cloudflare Pages auto-deploy
                                                         ↓
                                              Browser: HTML + JS филтри
```

## Файлове

- `scraper.py` — Python scraper с 25 паралелни workera
- `public/index.html`, `app.js`, `style.css` — статичен frontend
- `public/data.json` — данните (auto-generated, committed by GH Actions)
- `.github/workflows/scrape.yml` — cron workflow

## "NEW" badge логика

- При първо посещение на сайта: localStorage записва `installed_at = now` и нищо не се маркира като ново
- Обява е "NEW" ако: `first_seen > installed_at` AND не е още кликната
- Click на обява → ID-то се записва в localStorage → NEW badge изчезва
- Бутон "Маркирай всички като прочетени" нулира всички NEW badges
