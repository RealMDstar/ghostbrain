# Ghost Brain — Gemini Web → OpenAI-compatible API Bridge

یک پراکسی محلی که حساب(های) Gemini وب شما را به یک API کاملاً سازگار با OpenAI تبدیل می‌کند
(برای Claude Code، Roo/Cline، Open WebUI، AutoClaw/OpenClaw، curl). بدون API Key گوگل —
از همان مرورگر و اکانت خودتان استفاده می‌کند.

## امکانات

- **Token Pool**: چند پروفایل مرورگر (چند اکانت) با round-robin و failover خودکار.
- **Bean's Brain**: تزریق system prompt، پل ابزار (tool-call)، حفظ پایداری سشن، فشرده‌سازی پیام‌های طولانی.
- **مسیریابی مدل پویا**: مدل‌ها از داخل خود اکانت کشف می‌شوند؛ `/v1/models` و UI هر بار
  لیست واقعی مدل‌های همان حساب را نشان می‌دهند (نه لیست ثابت و قدیمی).
- **روتینگ مدل خارجی**: هر مدلی (مثل `gpt-*` یا یک LLM محلی) را می‌توان با `ghost_routes.json`
  به یک API خارجی OpenAI-compatible فوروارد کرد — استریم و غیراستریم، pass-through پارامترها.
- **پشتیبانی ابزار (tool_calls)**: اگر Gemini بلوک JSON ابزار تولید کند، به‌صورت
  `message.tool_calls` استاندارد برمی‌گردد تا Roo/Cline/OpenClaw ابزار را اجرا کنند.
- **سازگار با اسپک OpenAI**: `stream` پیش‌فرض false، chunk های کامل (id/object/created/model/index)،
  `finish_reason`، `[DONE]`، `usage` (با stream_options)، خطاها با شکل
  `{"error":{"message","type","code"}}` و کد وضعیت واقعی (404 مدل ناشناخته، 429 ریت‌لیمیت، ...).
- **پنجره کنترل**: آدرس سرور را نشان می‌دهد؛ بستن پنجره = توقف کامل برنامه و بستن مرورگرها.
- **مقاوم‌سازی**: خرابی یک پروفایل بقیه را نمی‌کشد؛ قفل تک‌نمونه‌ای؛ تشخیص خودکار لاگین مجدد
  (بعد از ورود در پنجره Chrome، ورکر بدون ریاستارت سبز می‌شود)؛ rate-limit handling.
- **auth اختیاری**: با `GHOST_API_KEY` فقط کلاینت‌هایی با `Authorization: Bearer <key>` پذیرفته می‌شوند.

## راه‌اندازی سریع (از سورس)

```powershell
# پایتون 3.11+ (نیازمند tkinter؛ بیلد 3.11 خود ویندوز مشکلی ندارد)
py -3.11 -m pip install -r requirements.txt
py -3.11 -m playwright install chromium

# اجرا
py -3.11 Gemini_Ghost_Brain.py --workers 2
```

پنجره کنترل باز می‌شود و برای هر پروفایل یک پنجره Chrome واقعی باز می‌شود —
وارد حساب Google خود شوید (فقط بار اول). وقتی ورکرها سبز شدند، API روی
`http://127.0.0.1:8000` آماده است.

## ساخت EXE (ویندوز)

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
# خروجی: .\dist\GhostBrain.exe  (حدود ۴۰۰ مگابایت)
```

الزامات بیلد: پایتون 3.11 با tkinter (اسکریپت بیلد پیش‌شرط را چک می‌کند)،
باندل‌شده‌های Playwright از قبل دانلود شده (`playwright install`). اجرای اول exe
~۳۰-۶۰ ثانیه برای extract طول می‌کشد.

## استفاده

```bash
# فهرست همه مدل‌ها (خارجی + اکانت + aliases)
curl http://127.0.0.1:8000/v1/models

# چت استریم (شناسه مدل را از /v1/models بگیرید)
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"3-6-flash","stream":true,
       "messages":[{"role":"user","content":"سلام!"}]}'

# غیراستریم (پیش‌فرض وقتی stream نفرستید)
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"3-6-flash",
       "messages":[{"role":"user","content":"۲+۲؟"}]}'
```

### اتصال به AutoClaw / OpenClaw (از Control UI یا config)

پایه: `http://127.0.0.1:8000/v1` — روش‌ها (هر سه در داک‌های OpenClaw مستندند):

1. **روش پیشنهادی — provider سفارشی** (افزودن به `~/.openclaw/openclaw.json` از تب **Config**
   در Control UI روی `http://127.0.0.1:18789`، یا با `openclaw onboard --non-interactive
   --auth-choice custom-api-key --custom-base-url "http://127.0.0.1:8000/v1"
   --custom-model-id "<id>" --custom-compatibility openai`):

```json5
{
  models: {
    mode: "merge",
    providers: {
      ghost: {
        baseUrl: "http://127.0.0.1:8000/v1",
        apiKey: "sk-local",   // لوپ‌بک → هر مقدار غیرخالی کافی است
        api: "openai-completions",
        timeoutSeconds: 300,
        models: [
          { id: "3-6-flash", name: "Ghost Gemini 3.6 Flash",
            reasoning: false, input: ["text"],
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
            contextWindow: 120000, maxTokens: 8192 },
          // ... بقیه id ها را از GET /v1/models بردارید
        ],
      },
    },
  },
  agents: {
    defaults: {
      model: { primary: "ghost/3-6-flash" },
      models: { "ghost/*": {} },
    },
  },
}
```

2. **میان‌بُر vLLM** (کشف خودکار مدل‌ها): چون پورت ۸۰۰۰ پورت پیش‌فرض vLLM است،
   `env: { VLLM_API_KEY: "any" }` + `agents.defaults.models: { "vllm/*": {} }` + `primary: "vllm/<id>"`.

3. **env-var**: `env: { OPENAI_API_KEY: "sk-local", OPENAI_BASE_URL: "http://127.0.0.1:8000/v1" }`
   + `primary: "openai/<id>"` (روی provider باندل‌شده `openai`).

بعد از تغییر، `openclaw config validate` و `openclaw infer model run --local --model ghost/<id>
--prompt "Reply with exactly: pong" --json` را برای تست بزنید. تغییرات دسته `models` hot-apply
می‌شوند (ریاستارت لازم نیست).

### Claude Code / Roo / Cline

- **Roo / Cline / Open WebUI**: `Base URL = http://127.0.0.1:8000/v1` و مدل = هر id از `/v1/models`.
- **Claude Code**: خود Claude Code فقط اندپوینت Anthropic می‌پذیرد؛ برای اتصال از
  `claude-code-router` استفاده کنید یا مدل‌های Ghost Brain را در AutoClaw/OpenClaw (بالا) ثبت کنید
  و Claude Code را به OpenClaw وصل کنید.

## روتینگ مدل خارجی — `ghost_routes.json`

فایل را کنار برنامه بگذارید (یا `GHOST_ROUTES=<path>`). الگو: `ghost_routes.example.json`.

```json
{
  "routes": [
    { "match": "gpt-*", "base_url": "https://api.openai.com/v1",
      "api_key": "<key>", "models": ["gpt-4o"], "label": "OpenAI" },
    { "match": "my-local", "base_url": "http://127.0.0.1:9999/v1",
      "api_key": "", "models": ["my-local"], "label": "Local LLM" }
  ]
}
```

درخواست‌هایی که `model` شان با `match` (الگوی fnmatch) تطبیق کند به آن endpoint فوروارد می‌شوند
(stream و non-stream، همراه با temperature/max_tokens/tools/...). این فایل ممکن است کلید API داشته
باشد — در گیت نرود (`.gitignore` دارد).

## متغیرهای محیطی

| متغیر | پیش‌فرض | توضیح |
|---|---|---|
| `GHOST_HOST` | `127.0.0.1` | بایند سرور؛ **برای امنیت فقط لوپ‌بک بگذارید** |
| `GHOST_PORT` | `8000` | پورت سرور |
| `GHOST_WORKERS` | `2` | تعداد پروفایل/ورکر |
| `GHOST_CHANNEL` | `chrome` | مرورگر واقعی برای لاگین گوگل (جلوگیری از «مرورگر ناامن»). مقدار `chromium` = مرورگر باندل‌شده |
| `GHOST_AUTO_OPEN` | `1` | بازکردن خودکار داشبورد در مرورگر پیش‌فرض وقتی ورکر سالم شد |
| `GHOST_API_KEY` | *(خالی)* | اگر ست شود، درخواست‌ها به `Authorization: Bearer <key>` نیاز دارند |
| `GHOST_ROUTES` | `ghost_routes.json` | مسیر فایل روتینگ خارجی |
| `GHOST_IDLE_TIMEOUT` | `120` | ثانیه بدون پیشرفت → توقف اجباری تولید |
| `GHOST_COOLDOWN` | `60` | ثانیه cooldown بعد از rate-limit |
| `GHOST_MAX_PROMPT` | `60000` | حداکثر طول کاراکتری payload ارسالی به جمنای |

## ساختار

```
Gemini_Ghost_Brain.py   کد اصلی تک‌فایلی (سرور + ورکرها + UI جاسازی‌شده)
ui.html                 رابط وب (اگر کنار برنامه باشد، بارگذاری می‌شود؛ وگرنه نسخه جاسازی‌شده)
build_exe.ps1           اسکریپت بیلد PyInstaller
requirements.txt        وابستگی‌های پایتون
ghost_routes.example.json  نمونه روتینگ خارجی
Gemini_Profiles/        (ایجاد خودکار) پروفایل‌های مرورگر هر اکانت — در گیت نرود
```

## امنیت و سلب مسئولیت ⚠️

- API **بدون احراز هویت** است (مگر `GHOST_API_KEY` بگذارید) — فقط روی `127.0.0.1` بگذارید؛
  هرگز روی اینترنت/شبکه بازش نکنید.
- استفادهٔ خودکار از اکانت شخصی Google (تحت ToS گوگل) ریسک محدودیت/مشکل برای اکانت دارد؛
  با احتیاط و حجم کم استفاده کنید.
- پروفایل‌ها در `Gemini_Profiles/` شامل کوکی‌ها و اطلاعات لاگین‌اند — هرگز منتشر نکنید.
- سلکتورهای UI گوگل ممکن است با تغییرات جمنای خراب شوند؛ در صورت مشکل، لاگ بدهید.

## لایسنس

MIT — رجوع به `LICENSE`.
