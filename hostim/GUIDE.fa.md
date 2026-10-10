# راهنمای استقرار Hostim — سامانهٔ اتوماسیون مرورگر مجید

این سند فقط به **مسیر استقرار Hostim** می‌پردازد. مسیر Google Colab هیچ تغییری
نکرده است و در **بخش ب** همین سند، سلول‌به‌سلول و با نام شاخهٔ پایدار
`colab-stable` توضیح داده شده است.

تمام فایل‌های این مسیر داخل پوشهٔ `hostim/` هستند. هیچ فایلی به ریشهٔ مخزن
اضافه نشده و هیچ‌کدام از اسکریپت‌های Colab (`install.sh`،
`start_colab_browser.sh`، `stop_colab_browser.sh`، `browser_control.sh`،
`automation/`) تغییر نکرده‌اند.

---

## فهرست

- [بخش الف — استقرار روی Hostim](#بخش-الف--استقرار-روی-hostim)
  - [۱. نتیجهٔ امکان‌سنجی](#۱-نتیجهٔ-امکان‌سنجی-چه-چیزی-تأیید-شده-و-چه-چیزی-تأییدنشده-است)
  - [۲. فایل‌های این مسیر](#۲-فایل‌های-این-مسیر)
  - [۳. معماری در Hostim](#۳-معماری-در-hostim)
  - [۴. تست محلی با Docker (بدون Hostim)](#۴-تست-محلی-با-docker-بدون-hostim)
  - [۵. استقرار با CLI](#۵-استقرار-با-cli)
  - [۶. استقرار با template](#۶-استقرار-با-template)
  - [۷. استقرار از داشبورد](#۷-استقرار-از-داشبورد)
  - [۸. مدیریت secretها](#۸-مدیریت-secretها)
  - [۹. verification گام‌به‌گام](#۹-verification-گام‌به‌گام-با-خروجی-مورد-انتظار)
  - [۱۰. دادهٔ پایدار و بکاپ](#۱۰-دادهٔ-پایدار-و-بکاپ)
  - [۱۱. عیب‌یابی](#۱۱-عیب‌یابی)
  - [۱۲. امنیت](#۱۲-امنیت)
  - [۱۳. چه چیزهایی تست نشده‌اند](#۱۳-چه-چیزهایی-تست-نشده‌اند-صریحاً)
  - [۱۴. فارسی، صفحه‌کلید و NumLock](#۱۴-فارسی-صفحه‌کلید-و-numlock)
  - [۱۵. ایجنت همکار](#۱۵-ایجنت-همکار-coworker-agent)
  - [۱۶. تازه‌های این نسخه: چت، عملیات، کلید ایجنت، تلگرام دوکاناله، نشانگر](#۱۶-تازه‌های-این-نسخه)
- [بخش ب — مسیر Colab (شاخهٔ پایدار `colab-stable`)](#بخش-ب--مسیر-colab-شاخهٔ-پایدار-colab-stable)

---

# بخش الف — استقرار روی Hostim

## ۱. نتیجهٔ امکان‌سنجی: چه چیزی تأیید شده و چه چیزی تأییدنشده است

منبع: مستندات عمومی [hostim.dev/docs](https://hostim.dev/docs/intro/)،
[CLI manual](https://hostim.dev/cli-manual.md)،
[changelog](https://hostim.dev/docs/changelog/) و
[pricing](https://hostim.dev/docs/billing/pricing-model/) — بدون ورود به حساب
کاربری.

این جدول **پیش از** اولین استقرار واقعی نوشته شد و بعد از آن به‌روزرسانی شده
است؛ هر ردیفی که با استقرار واقعی سنجیده شده صریحاً همان‌جا گفته شده.

| # | موضوع | وضعیت | نکتهٔ عملی |
|---|---|---|---|
| ۱ | Dockerfile / image سفارشی | ✅ تأییدشده (build واقعی موفق) | build از Git با مسیر دلخواه Dockerfile؛ image باید `linux/amd64` و زیر ۴ گیگابایت باشد. **در عمل:** context ریشهٔ مخزن است، پس Dockerfile باید در ریشه باشد (بخش «mirror ریشه»)؛ اندازهٔ نهایی مستقیم اندازه‌گیری نشد |
| ۲ | چند فرایند ماندگار در یک کانتینر | ✅ تأییدشده (با استقرار واقعی) | مستندات نه منع کرده نه تأیید، ولی در عمل کانتینر با `tini` به‌عنوان PID 1، Xvfb + fluxbox + x11vnc + Chrome + سرور (+ مرورگر ایجنت) را هم‌زمان نگه می‌دارد. auto-restart خودِ پلتفرم بعد از crash طبق مستندات است، نه چیزی که من دیده باشم |
| ۳ | یک پورت HTTP عمومی برای noVNC/API | ✅ تأییدشده | هر app دقیقاً یک `httpPort` دارد؛ دامنهٔ `*.hostim.dev` با HTTPS خودکار |
| ۴ | WebSocket از مسیر HTTPS/proxy | ✅ تأییدشده (با استقرار واقعی) | ingress پلتفرم Traefik v3 است. **در عمل:** دسکتاپ noVNC از دامنهٔ عمومی باز شد و سرعتش توسط شما تأیید شد، بدون هیچ تنظیم اضافه. آنچه ماند: `idle timeout` برای نشست‌های **طولانی** (Hostim هیچ عددی مستند نکرده) |
| ۵ | volume پایدار | ✅ تأییدشده | mount روی `/data`؛ پایدار بین deploy؛ بکاپ روزانه با ۷ روز نگه‌داری |
| ۶ | منابع / root / seccomp / privileged | 🔶 پلن‌ها تأییدشده، بقیه **تأییدنشده** | سقف ۳ vCPU / ۸GB؛ هیچ فلگی برای privileged وجود ندارد؛ هر app داخل یک microVM از Kata Containers است |

**جمع‌بندی:** استقرار انجام شد و کار می‌کند. طراحی فعلی پروژه (همه‌چیز روی یک
پورت، VNC و CDP فقط روی loopback، بدون نیاز به privileged) با محدودیت‌های
Hostim جور درآمد. از سه ریسک اصلیِ اول راه:

1. ✅ اجرای واقعی Chrome روی Xvfb داخل microVM از Kata **کار کرد** — دسکتاپ
   دیده شد و swiftshader رندر کرد. آنچه باز مانده اندازهٔ `/dev/shm` و رفتار
   نشست‌های بسیار طولانی است.
2. 🔶 عبور WebSocket noVNC از ingress **کار کرد**؛ فقط idle timeout نشست‌های
   طولانی تأییدنشده ماند (اگر نشست بعد از زمان ثابتی — مثلاً دقیقاً ۶۰ یا
   ۱۸۰ ثانیه بی‌کاری — قطع شد، همان نشانه است).
3. ✅ کاربر non-root (uid 1000) بدون هیچ مشکل دسترسی کار کرد؛ volume بعد از
   mount قابل نوشتن است.

ریسک تأییدنشدهٔ **تازه**، کل لایهٔ ایجنت همکار است: مرورگر دوم روی `:2`،
چت واقعی با DeepSeek از راه وب، ارسال واقعی تلگرام، نصب Telethon در image و
thread زمان‌بند در بازهٔ طولانی. فهرست دقیق در بخش **۱۵.۱۰**.

### پلن‌ها (تأییدشده)

| پلن | هسته | RAM | قیمت ماهانه |
|---|---|---|---|
| `sa-1-1` | ۱ | ۱ گیگابایت | €۲.۵ |
| `sa-2-2` | ۲ | ۲ گیگابایت | €۴.۵ |
| `sa-3-4` | ۳ | ۴ گیگابایت | €۷.۵ |
| `sa-4-8` | ۴ | ۸ گیگابایت | €۱۳.۵ |
| `da-1-4` (اختصاصی) | ۱ | ۴ گیگابایت | €۱۸ |

> توصیهٔ مهندسی من (نه مستند Hostim): برای Chrome + Xvfb + x11vnc + سرور
> Python حداقل `sa-2-2` و راحت‌تر `sa-3-4` یا `da-1-4`. روی ۱ گیگابایت احتمال
> OOM هنگام رندر صفحه‌های سنگین زیاد است.

> volume: پلن ۵ گیگابایت `vol-1` است (€۱ در ماه). شناسهٔ پلن رایگان ۱ گیگابایتی
> در مستندات نیامده؛ قبل از ساخت با دستور زیر فهرست واقعی را ببینید:
> `hostim regions pricing eu-center --for volume`

---

## ۲. فایل‌های این مسیر

```text
Dockerfile                     # mirror بایت‌به‌بایت hostim/Dockerfile (چرا؟ پایین)
Dockerfile.dockerignore        # کپی hostim/Dockerfile.dockerignore برای همان mirror
hostim/
├── Dockerfile                 # منبع حقیقت: image اتمی Ubuntu 24.04 + میزکار + Chrome، بدون cloudflared
├── Dockerfile.dockerignore    # کوچک‌سازی build context وقتی با -f hostim/Dockerfile می‌سازید
├── docker-entrypoint.sh       # سوپروایزر: ترتیب راه‌اندازی، health، راه‌اندازی مجدد، خروج تمیز
├── healthcheck.sh             # پروب سلامت داخل کانتینر (برای docker و برای hostim exec)
├── compose.yaml               # فقط برای تست محلی با docker compose
├── hostim-template.yaml       # template بومی Hostim (اپ + volume)
└── GUIDE.fa.md                # همین راهنما
tests/
└── test_hostim_deploy.py      # تست قراردادهای بالا؛ با run_tests.sh خودکار اجرا می‌شود
```

### mirror ریشه: `Dockerfile`

`Dockerfile` در ریشهٔ مخزن **فقط یک کپی بایت‌به‌بایت از `hostim/Dockerfile`**
است و منبع حقیقت همان `hostim/Dockerfile` می‌ماند. دلیلش تجربهٔ واقعی اولین
build روی Hostim است. BuildKit منبع git را به این شکل می‌گیرد:

```text
#1 [internal] load git source https://github.com/<repo>.git#<commit>
error: failed to solve: failed to read dockerfile: open Dockerfile: no such file or directory
```

در این syntax، fragment می‌تواند `#<ref>:<subdir>` باشد تا context یک
زیرپوشه شود، ولی Hostim فقط `#<commit>` می‌فرستد. نتیجه: **context ریشهٔ مخزن
است و نام Dockerfile هم پیش‌فرضِ `Dockerfile` در همان ریشه** — یعنی مسیر
`hostim/Dockerfile` در این حالت اعمال نمی‌شود. خبر خوب اینکه context دقیقاً
همان چیزی است که Dockerfile ما فرض کرده (`COPY automation/ ...`)، پس فقط جای
فایل مسئله بود نه محتوایش.

**نگهداری:** هر تغییری را اول در `hostim/Dockerfile` بدهید، بعد mirror را
بازتولید کنید:

```bash
python3 - <<'PY'
import re
canon = open("hostim/Dockerfile", encoding="utf-8").read()
lines = canon.splitlines(keepends=True)
NOTE = re.compile(r"(?ms)^# ===== BEGIN hostim-root-mirror-note =====\n.*?"
                  r"^# ===== END hostim-root-mirror-note =====\n")
note = NOTE.search(open("Dockerfile", encoding="utf-8").read()).group(0)
open("Dockerfile", "w", encoding="utf-8").write(lines[0] + note + "".join(lines[1:]))
open("Dockerfile.dockerignore", "w", encoding="utf-8").write(
    open("hostim/Dockerfile.dockerignore", encoding="utf-8").read())
print("mirror regenerated")
PY
```

اگر این کار را نکنید هم چیزی بی‌سروصدا خراب نمی‌شود: تست
`TestRootMirrorOfHostimDockerfile` در `tests/test_hostim_deploy.py` برابری
بایت‌به‌بایت این دو را بررسی می‌کند و در صورت جدا شدنشان `run_tests.sh` قرمز
می‌شود. همان تست بررسی می‌کند که `# syntax=docker/dockerfile:1` حتماً خط اول
بماند (وگرنه BuildKit آن را نادیده می‌گیرد).

> نکته دربارهٔ `Dockerfile.dockerignore`: BuildKit پیش از
> `<context>/.dockerignore` فایل `<مسیر Dockerfile>.dockerignore` را می‌خواند.
> این قرارداد برای build محلی مهم است (`node_modules` بیرون می‌ماند)، ولی برای
> build از منبع Git تقریباً بی‌اثر است چون کل مخزنِ commit‌شده فقط ~۵۴۵
> کیلوبایت است و `node_modules` در Git نیست. Dockerfile هم هرگز `COPY . .`
> ندارد و فقط مسیرهای صریح را کپی می‌کند.

---

## ۳. معماری در Hostim

```text
مرورگر انسان
    |
    | HTTPS  +  WSS   (یک دامنه، یک پورت)
    v
Traefik v3 (ingress خودِ Hostim، گواهی Let's Encrypt)
    |
    v  httpPort = 6080
کانتینر اپ  (یک microVM از Kata، پلن sa-2-2، replicas = 1)
│
│  tini  (PID 1: بازکردن zombieها، ارسال SIGTERM)
│   └── docker-entrypoint.sh   (سوپروایزر)
│        ├── Xvfb :1            1366x768x24
│        ├── fluxbox            (مدیر پنجره، با dbus-launch)
│        ├── x11vnc             127.0.0.1:5901   ← خصوصی
│        ├── google-chrome      127.0.0.1:9222   ← خصوصی (CDP)
│        └── automation/server.py   0.0.0.0:6080
│              ├── فایل‌های noVNC        /vnc.html
│              ├── sidebar و panel       /automation/*
│              ├── API JSON              /automation/api/*
│              └── پروکسی WebSocket      /websockify → 127.0.0.1:5901
│
└── volume پایدار روی /data
     ├── chrome-profile/     پروفایل کروم
     ├── automation/         flowها، runها، صفحه‌های شناسایی‌شده، archive اسکرین‌شات‌ها
     ├── logs/               لاگ Xvfb/fluxbox/x11vnc/chrome
     ├── secrets.env         mode 0600 — AUTOMATION_TOKEN و VNC_PASSWORD
     └── vnc.pass            mode 0600
```

تفاوت‌های عمدی با مسیر Colab:

| مورد | Colab (`start_colab_browser.sh`) | Hostim (`hostim/docker-entrypoint.sh`) |
|---|---|---|
| مسیر عمومی | Cloudflare Quick Tunnel | ingress خودِ Hostim با دامنهٔ HTTPS |
| bind سرور | `127.0.0.1:6080` | `0.0.0.0:$PORT` |
| دادهٔ ماندگار | `.runtime/` (با حذف runtime از بین می‌رود) | volume روی `/data` |
| توکن API | روی command line (`--token`) | فقط از راه محیط (`AUTOMATION_TOKEN`) تا در `/proc/<pid>/cmdline` دیده نشود |
| web root noVNC | `.runtime/www` | `/tmp` (ephemeral) تا پس از ارتقای image نسخهٔ قدیمی noVNC سرو نشود |
| کاربر | root | کاربر غیرroot با uid 1000 |
| PID 1 | خودِ اسکریپت | `tini` |

پورت‌های `5901` (VNC) و `9222` (CDP) در هر دو مسیر **خصوصی** می‌مانند. در
Hostim اصلاً راهی برای عمومی کردن یک پورت TCP خام وجود ندارد (فقط یک
`httpPort`)، که دقیقاً با خواستهٔ ما هم‌راستاست.

---

## ۴. تست محلی با Docker (بدون Hostim)

این مرحله اختیاری است ولی **قوی‌ترین verification در دسترس شماست**، چون تنها
جایی است که Chrome و Xvfb واقعی اجرا می‌شوند.

```bash
# از ریشهٔ مخزن، روی شاخهٔ hostim-deploy
git switch hostim-deploy
docker compose -f hostim/compose.yaml up --build
```

**خروجی مورد انتظار** (در لاگ compose):

```text
... === Majid Automation System - Hostim entrypoint ===
... secrets: /data/secrets.env (mode 0600, not printed to the log)
...          read them with: hostim exec <app> -- cat /data/secrets.env
... Xvfb on :1 (1366x768x24)
... fluxbox started
... x11vnc on 127.0.0.1:5901 (private)
... Chrome started (CDP on 127.0.0.1:9222, profile /data/chrome-profile)
... automation server starting on 0.0.0.0:6080
... INFO automation.server: listening on 0.0.0.0:6080
... READY: noVNC + sidebar + JSON API + VNC WebSocket on port 6080
... health check path for the platform: /automation/api/info
```

**روش verification:**

```bash
# ۱. صفحهٔ noVNC
open 'http://localhost:6080/vnc.html?autoconnect=true&resize=scale&path=websockify'
#    → دسکتاپ ۱۳۶۶×۷۶۸ با Chrome باید دیده شود؛ رمز VNC را از secrets.env بردارید

# ۲. secretها (هرگز در لاگ چاپ نمی‌شوند)
docker compose -f hostim/compose.yaml exec browser cat /data/secrets.env

# ۳. مسیر health، بدون توکن باید 200 باشد
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:6080/automation/api/info   # → 200

# ۴. بقیهٔ مسیرها بدون توکن باید 401 باشند
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:6080/automation/api/status  # → 401

# ۵. فقط یک پورت منتشر شده؛ 5901 و 9222 باید از بیرون بسته باشند
docker compose -f hostim/compose.yaml ps
nc -z localhost 5901 || echo "5901 بسته است (درست)"
nc -z localhost 9222 || echo "9222 بسته است (درست)"

# ۶. خاموش شدن تمیز با SIGTERM
docker compose -f hostim/compose.yaml down          # volume نگه داشته می‌شود
docker compose -f hostim/compose.yaml down -v       # داده‌ها هم پاک می‌شود
```

---

## ۵. استقرار با CLI

### ۵.۱ نصب و ورود

```bash
curl -fsSL https://raw.githubusercontent.com/hostimdev/cli/main/install.sh | sh
hostim login          # یک کد کوتاه می‌دهد؛ در مرورگر تأیید کنید
hostim projects ls
hostim use <نام پروژه>
```

> `HOSTIM_TOKEN` را هرگز در چت یا در مخزن نگذارید. برای CI از secret خودِ
> GitHub Actions استفاده کنید.

### ۵.۲ از کدام شاخه deploy کنیم؟

Hostim مخزن را از GitHub clone می‌کند و هر شاخه‌ای را می‌پذیرد؛ تنها شرط این
است که **همان شاخه واقعاً `hostim/Dockerfile` را داشته باشد**. مخزن عمومی است،
پس به git token نیازی نیست.

دو حالت:

| حالت | `BRANCH` | کی |
|---|---|---|
| **شاخهٔ کاری فعلی (بدون merge)** | `arena/19c5b0b6-majidautomationsystem-colab-br` | همین حالا؛ سریع‌ترین راه برای اولین تست واقعی |
| شاخهٔ استقرار | `hostim-deploy` | بعد از اینکه PR #1 merge شد؛ این همان شاخه‌ای است که `hostim-template.yaml` به‌صورت پیش‌فرض نشانه رفته |

> اگر از template استفاده می‌کنید و هنوز PR را merge نکرده‌اید، خط
> `branch: hostim-deploy` را در `hostim/hostim-template.yaml` به شاخهٔ کاری
> عوض کنید — وگرنه Hostim شاخه‌ای را build می‌کند که `Dockerfile` در آن وجود
> ندارد و build با خطای «فایل پیدا نشد» شکست می‌خورد.

```bash
# یکی از این دو را انتخاب کنید:
BRANCH=arena/19c5b0b6-majidautomationsystem-colab-br   # بدون merge، همین حالا
# BRANCH=hostim-deploy                                 # بعد از merge شدن PR #1
```

### ۵.۳ ساخت volume و استقرار

```bash
# فهرست پلن‌های واقعی region خودتان
hostim regions pricing eu-center --for volume
hostim regions pricing eu-center --for apps

# volume پایدار
hostim volumes create automation-data --plan vol-1

# استقرار؛ Dockerfile از ریشهٔ build context خوانده می‌شود (mirror ریشه)
hostim deploy browser \
  --git https://github.com/majidshilehsari/MajidAutomationSystem-colab_browser-use \
  --branch "$BRANCH" \
  --dockerfile Dockerfile \
  --plan sa-2-2 \
  --port 6080 \
  --replicas 1 \
  --health-check-path /automation/api/info \
  --volume automation-data:/data \
  --env PORT=6080
```

**خروجی مورد انتظار:** `deploy` منتظر build می‌ماند و اگر build شکست بخورد با
کد غیرصفر خارج می‌شود. در حالت موفق، دامنهٔ app را می‌بینید
(`<نام>-<رشته>.hostim.dev`).

لاگ build را با `hostim logs browser --build -f` دنبال کنید؛ اگر جایی شکست،
همان لاگ را (بدون secret) نگه دارید — برای رفع خطا لازم است.

> ⚠️ **Command Override را خالی بگذارید.** در Hostim این فیلد هم `ENTRYPOINT`
> و هم `CMD` را کامل جایگزین می‌کند؛ اگر چیزی بنویسید، `tini` و کل سوپروایزر
> از کار می‌افتند.

### ۵.۴ به‌روزرسانی

```bash
hostim apps rebuild browser      # build دوباره از همان منبع
hostim apps restart browser      # راه‌اندازی مجدد کانتینرها
hostim status browser -o json
hostim logs browser -f
```

> اگر image را با تگ مشخص (مثل `v1`) deploy می‌کنید، `restart` image جدید را
> نمی‌کشد؛ تگ را عوض کنید. در مسیر Git این مسئله وجود ندارد.

---

## ۶. استقرار با template

کل پشته (اپ + volume) در یک فایل توصیف شده است:

```bash
# بررسی آفلاین، بدون هیچ تماس API و بدون ساخت چیزی
hostim templates validate -f hostim/hostim-template.yaml

# استقرار در پروژهٔ فعلی
hostim templates apply -f hostim/hostim-template.yaml

# یا در یک پروژهٔ کاملاً جدید
hostim templates apply -f hostim/hostim-template.yaml \
  --new-project browser-automation --region eu-center
```

`apply` قبل از ساخت منابع تأییدیه می‌خواهد (`-y` آن را رد می‌کند) و اگر منبعی
از قبل وجود داشته باشد، بدون ساختن چیزی متوقف می‌شود.

مقادیر کلیدی در template (همه با تست قراردادی بررسی می‌شوند):

| فیلد | مقدار | چرا |
|---|---|---|
| `replicas` | `1` | دسکتاپ مرورگری stateful است؛ دو replica یعنی دو Xvfb جدا پشت یک دامنه بدون session affinity |
| `httpPort` | `6080` | همان `PORT` داخل کانتینر |
| `healthCheckPath` | `/automation/api/info` | تنها مسیر API که توکن نمی‌خواهد، پس پروب پلتفرم می‌تواند بدون secret صدا بزند |
| `mountPath` | `/data` | مسیر خنثی؛ هرگز volume را روی باینری یا مسیر سیستمی mount نکنید |
| `dockerfilepath` | `Dockerfile` | همان mirror ریشه؛ در هر دو حالت (چه پلتفرم مسیر را اعمال کند چه نکند) کار می‌کند |
| `branch` | `hostim-deploy` | **هرگز** `colab-stable` |
| `command` | (تنظیم نشده) | تا `tini` و سوپروایزر حفظ شوند |

---

## ۷. استقرار از داشبورد

1. در [console.hostim.dev](https://console.hostim.dev/) یک پروژه بسازید
   (region: `eu-center`). بدون روش پرداخت، پروژه به‌صورت آزمایشی ۵ روزه ساخته
   می‌شود و پس از آن **حذف** می‌گردد.
2. **Create Service → New Volume**: نام `automation-data`، پلن دلخواه.
3. **Create Service → New App**:
   - Deployment Type: **Git**
   - Git URL: `https://github.com/majidshilehsari/MajidAutomationSystem-colab_browser-use`
   - Branch: **شاخه‌ای که `Dockerfile` را دارد** — یا
     `arena/19c5b0b6-majidautomationsystem-colab-br` (بدون merge، همین حالا) یا
     `hostim-deploy` (بعد از merge شدن PR). **هرگز `colab-stable`**
   - Dockerfile path: **`Dockerfile`** (همان پیش‌فرض). اگر فیلد را خالی
     بگذارید هم همین اتفاق می‌افتد. `hostim/Dockerfile` هم محتوای یکسانی دارد،
     ولی تجربهٔ واقعی نشان داد builder پلتفرم از روی context ریشه می‌خواند —
     بخش «mirror ریشه» را ببینید.
   - Plan: `sa-2-2` یا بالاتر، Replicas: **۱**
   - HTTP port: **۶۰۸۰**
   - Health check path: **`/automation/api/info`**
   - Command Override: **خالی**
   - در همان فرم ساخت، **Add New Volume Mount** → `automation-data` روی `/data`
4. **Create App**. اولین build چند دقیقه طول می‌کشد؛ در تب **Logs** از
   dropdown گزینهٔ **Build** را انتخاب کنید (تا پایان build، جریان **StdOut**
   خالی است و این طبیعی است).
5. دامنهٔ app را از صفحهٔ app بردارید و URL زیر را باز کنید:

```text
https://<دامنهٔ اپ>/vnc.html?autoconnect=true&resize=scale&path=websockify
```

---

## ۸. مدیریت secretها

دو secret وجود دارد: `AUTOMATION_TOKEN` (مجوز API اتوماسیون) و `VNC_PASSWORD`
(رمز noVNC). **هیچ‌کدام در لاگ چاپ نمی‌شوند** و هیچ‌کدام در Git نیستند.

ترتیب اولویت در entrypoint:

1. اگر `AUTOMATION_TOKEN` یا `VNC_PASSWORD` به‌عنوان env var اپ تنظیم شده
   باشد، همان استفاده می‌شود.
2. وگرنه اگر `/data/secrets.env` وجود داشته باشد، از روی volume خوانده
   می‌شود (پس بین redeploy پایدار می‌ماند و نشانک‌های noVNC شما نمی‌شکنند).
3. وگرنه در اولین اجرا تولید و با mode `0600` روی volume نوشته می‌شود
   (`openssl rand -hex 8` برای توکن و `openssl rand -hex 4` برای رمز VNC —
   همان طول‌هایی که مسیر Colab تولید می‌کند).

### خواندن secretها

> ⚠️ **در داشبورد، صفحهٔ اپ هیچ تب shell ندارد** (تب‌ها فقط Overview، Envs،
> Domains، Logs، Metrics و Volumes هستند). دسترسی shell در Hostim از راه
> **Bastion** پروژه است، نه از راه مرورگر. سه راه دارید:

**راه ۱ — Bastion SSH (بدون نصب هیچ چیز، حتی volume را مستقیم می‌خواند):**

1. در کنسول: **Project → تب Bastion (SSH)** → کلید عمومی SSH خودتان را اضافه
   کنید (محتوای `~/.ssh/id_ed25519.pub` یا `~/.ssh/id_rsa.pub`).
2. وصل شوید (شناسهٔ پروژه و region خودتان؛ مثال زیر region `eu-center` است):

```bash
ssh <project-id>@ssh.eu-center.hostim.dev
# مثال:  ssh hpr-48a20eb8@ssh.eu-center.hostim.dev
```

3. حالا **دو** گزینه دارید:

```bash
# الف) مستقیم از روی volume — ساده‌ترین راه، وارد کانتینر هم نمی‌شود
cat /volumes/automation-data/secrets.env

# ب) shell تعاملی داخل خودِ اپ
shell <app-name>            # مثال: shell app-masc
cat /data/secrets.env
ss -ltn                     # همان‌جا چک گام ۳ را هم می‌توانید بزنید
/app/hostim/healthcheck.sh
```

> نام volume همان چیزی است که در تب **Volumes** می‌بینید؛ اگر `automation-data`
> نگذاشته‌اید، نام واقعی خودتان را جایگزین کنید. نام اپ هم از فهرست **Apps**
> یا از DNS داخلی (`<app-name>-service`) قابل تشخیص است.
>
> انگشت‌های host key رسمی Bastion در
> `hostim.dev/docs/services/bastion/` منتشر شده‌اند؛ اولین بار که SSH می‌پرسد
> آن را با همان مقایسه کنید.

**راه ۲ — CLI (اگر `hostim` را نصب دارید):**

```bash
hostim exec -y <app-name> -- cat /data/secrets.env
```

**راه ۳ — بدون هیچ shell: رمز را خودتان تعیین کنید.** در تب **Envs** اپ یک
متغیر به نام `VNC_PASSWORD` (و در صورت نیاز `AUTOMATION_TOKEN`) با مقدار
دلخواه خودتان بسازید و اپ را restart/redeploy کنید. طبق بند ۱ اولویت بالا،
همان مقدار استفاده می‌شود و فایل `/data/secrets.env` هم با مقدار واقعی
به‌روز می‌ماند — پس دیگر لازم نیست رمزی را که نمی‌دانید از جایی بخوانید.
رمز VNC حداکثر ۸ بایت مؤثر است (x11vnc بقیه را می‌بُرد).

> ⚠️ این راه ۳ تا commit `79ea138` به‌بعد کار می‌کند. پیش از آن یک باگ در
> entrypoint بود: `source /data/secrets.env` **بعد از** خواندن env اجرا می‌شد
> و مقدار فایل، مقدار env را بازنویسی می‌کرد. یعنی رمز را در Envs
> می‌گذاشتید ولی کانتینر همان رمز تصادفی قبلی را استفاده می‌کرد و شما هیچ
> راهی برای فهمیدنش نداشتید. رفع شد و حالا تست رفتاری
> `TestSecretPrecedenceIsReal` تابع واقعی را در bash اجرا می‌کند تا این
> اولویت دیگر نشکند. اگر اپ شما از commit قدیمی‌تر ساخته شده، یک **Rebuild**
> لازم است.

چرا در لاگ چاپ نمی‌شوند: لاگ کانتینر توسط پلتفرم جمع‌آوری و تا ۷ روز نگه‌داری
می‌شود؛ چاپ secret در لاگ یعنی نشت آن به جایی که کنترلش دست شما نیست. این
یک تفاوت عمدی با مسیر Colab است که هر سه مقدار را در خروجی سلول چاپ می‌کند
(چون در Colab آن خروجی فقط در نوت‌بوک خصوصی خودتان است).

> یک استثنای باقی‌مانده که صادقانه می‌گویم: ساختن فایل رمز VNC با
> `x11vnc -storepasswd <رمز> <فایل>` انجام می‌شود و در همان لحظهٔ کوتاه، رمز
> در `/proc/<pid>/cmdline` آن فرایند دیده می‌شود (دقیقاً مثل مسیر Colab).
> توکن API این مشکل را ندارد، چون از راه محیط به سرور داده می‌شود.

### چرخش secretها

```bash
# ساده‌ترین راه: همان راه ۳ بالا — مقدار تازه در تب Envs و restart اپ

# یا از راه Bastion/CLI، فایل را پاک کنید تا در اجرای بعدی secret تازه ساخته شود
shell <app-name>            # از داخل Bastion
rm -f /data/secrets.env
exit
# سپس اپ را از داشبورد restart کنید؛ secret تازه ساخته می‌شود
```

---

## ۹. verification گام‌به‌گام با خروجی مورد انتظار

### گام ۱ — وضعیت اپ

```bash
hostim status browser
hostim events browser -n 20
```

**مورد انتظار:** وضعیت در حال اجرا/سالم (نه `unhealthy` و نه `stopped`). اگر
`unhealthy` است یعنی پروب `/automation/api/info` جواب ۲۰۰ نمی‌دهد.

### گام ۲ — لاگ راه‌اندازی

```bash
hostim logs browser -n 200
```

**مورد انتظار:** همان بلوکی که در بخش ۴ آمده، شامل
`READY: noVNC + sidebar + JSON API + VNC WebSocket on port 6080` و
`open: https://<دامنه>/vnc.html?...`. **مورد انتظار نیست:** هر خطی شامل
`AUTOMATION_TOKEN=` یا `VNC_PASSWORD=`.

اگر لاگ **کاملاً خالی** است، مشکل از کد ما نیست: یا build تمام نشده (تب
**Build**) یا image برای `linux/amd64` ساخته نشده یا Command Override اشتباه است.

### گام ۳ — گوش‌دادنده‌ها (تأیید خصوصی ماندن VNC و CDP)

```bash
hostim exec browser -- ss -ltn
```

**مورد انتظار:**

```text
0.0.0.0:6080        ← عمومی (تنها ورودی)
127.0.0.1:5901      ← خصوصی (x11vnc)
127.0.0.1:9222      ← خصوصی (Chrome CDP)
```

اگر `5901` یا `9222` روی `0.0.0.0` بودند، استقرار را متوقف کنید و گزارش دهید.

### گام ۴ — health از داخل کانتینر

```bash
hostim exec browser -- /app/hostim/healthcheck.sh && echo HEALTHY
hostim exec browser -- env HEALTHCHECK_DEEP=1 /app/hostim/healthcheck.sh && echo DEEP-OK
hostim exec browser -- curl -s http://127.0.0.1:6080/automation/api/info
```

**مورد انتظار برای مسیر آخر:** یک JSON با `"authRequired": true` و
`"viewport": {"width": 1366, "height": 768}`. حالت `DEEP` علاوه بر HTTP،
نمایش X و CDP کروم را هم بررسی می‌کند و باید `cdpAvailable: true` را در
`/info` ببینید (در محیط تست من چون کروم واقعی نبود `false` بود).

### گام ۵ — دسترسی عمومی HTTPS

```bash
DOM='<دامنهٔ اپ>.hostim.dev'
curl -s -o /dev/null -w 'vnc.html  %{http_code}\n' "https://$DOM/vnc.html"                      # → 200
curl -s -o /dev/null -w 'info      %{http_code}\n' "https://$DOM/automation/api/info"           # → 200
curl -s -o /dev/null -w 'status    %{http_code}\n' "https://$DOM/automation/api/status"         # → 401
curl -s -o /dev/null -w 'http→https %{http_code}\n' "http://$DOM/vnc.html"                     # → 301/308
```

### گام ۶ — WebSocket (مهم‌ترین ریسک تأییدنشده)

در مرورگر `https://<دامنه>/vnc.html?autoconnect=true&resize=scale&path=websockify`
را باز کنید، رمز VNC را وارد کنید.

**مورد انتظار:** دسکتاپ ۱۳۶۶×۷۶۸ با Chrome دیده شود و نشانگر sidebar روی
**Connected** برود.

**اگر تصویر آمد ولی قطع می‌شود:** این همان ریسک timeout در ingress است که
مستند نشده. noVNC خودش reconnect می‌کند؛ اگر فاصلهٔ قطع‌شدن ثابت بود (مثلاً
دقیقاً ۶۰ یا ۱۸۰ ثانیه) یعنی یک idle timeout در مسیر است و باید به پشتیبانی
Hostim گزارش شود. برای کاهش اثر آن، `resize=scale` را نگه دارید و در
`Settings → Clipboard/Scaling` تغییر ندهید؛ حرکت دوره‌ای ماوس هم اتصال را
زنده نگه می‌دارد.

### گام ۷ — API با توکن

```bash
TOK=$(hostim exec browser -- sh -c 'sed -n "s/^AUTOMATION_TOKEN=//p" /data/secrets.env')
curl -s -H "X-Automation-Token: $TOK" "https://$DOM/automation/api/status"
curl -s -X POST -H "X-Automation-Token: $TOK" -H 'Content-Type: application/json' \
     -d '{}' "https://$DOM/automation/api/screenshot"
```

**مورد انتظار:** `{"runId": null, "status": "idle", ...}` و برای اسکرین‌شات یک
`publicUrl` از جنس `/automation/api/public/shot/<نام>`.

> `TOK` را در چت، در issue یا در commit نگذارید.

### گام ۸ — sidebar و شناسایی صفحه

۱. در صفحهٔ noVNC، sidebar سمت راست و تب AI را باز کنید.
۲. توکن را در فیلد توکن sidebar وارد کنید (**نه** در یک گفت‌وگوی AI).
۳. **مورد انتظار:** نشانگر اتصال → **Connected**.
۴. **Pages → Detect page** → **مورد انتظار:** یک رکورد صفحه به‌همراه
   اسکرین‌شات و ارزیابی `challenge`.

### گام ۹ — پایداری داده بین استقرار مجدد

```bash
hostim exec browser -- sh -c 'echo hello > /data/automation/persist-check.txt'
hostim apps restart browser
hostim exec browser -- cat /data/automation/persist-check.txt      # → hello
hostim exec browser -- ls /data/chrome-profile | head              # → پروفایل کروم سر جایش است
```

**مورد انتظار:** هر دو دستور بعد از restart همان داده را برگردانند. یعنی
volume درست mount شده و secretها هم بازتولید نشده‌اند.

### گام ۱۰ — رفتار در برابر خرابی

```bash
hostim exec browser -- sh -c 'pkill -f "google-chrome" || true'
sleep 25
hostim logs browser -n 40 | grep -E 'Chrome is gone|relaunch'
```

**مورد انتظار:** خط `Chrome is gone; relaunching it` و برگشتن مرورگر. اگر یک
سرویس **هسته‌ای** (Xvfb، fluxbox، x11vnc یا سرور Python) بمیرد، entrypoint با
`FATAL: <نام> stopped unexpectedly` کل پشته را خاموش می‌کند و با کد ۱
می‌خروج‌د، تا پلتفرم کانتینر را از نو بالا بیاورد (Hostim مستند کرده که
کانتینرهای متوقف‌شده را خودکار restart می‌کند).

---

## ۱۰. دادهٔ پایدار و بکاپ

روی volume (`/data`) نگه داشته می‌شود:

| مسیر | محتوا |
|---|---|
| `/data/chrome-profile/` | پروفایل کروم: کوکی‌ها، نشست‌های لاگین، تاریخچه |
| `/data/automation/flows/` | flowهای ذخیره‌شده |
| `/data/automation/runs/` | یک پوشه به ازای هر اجرای پایان‌یافته، با لاگ و اسکرین‌شات |
| `/data/automation/pages/` | حافظهٔ صفحه‌های شناسایی‌شده |
| `/data/automation/archive/` | `shots.json` و `texts.json` (فهرست اسکرین‌شات‌ها و متن‌های استخراج‌شده) |
| `/data/logs/` | لاگ Xvfb، fluxbox، x11vnc و chrome |
| `/data/secrets.env`، `/data/vnc.pass` | secretها با mode 0600 |

**در Git نیست و نباید باشد:** همهٔ موارد بالا، به‌علاوهٔ `screen_shots/` و
`node_modules/`. فایل `.gitignore` مخزن از قبل `.runtime/`، `screen_shots/`،
`node_modules/` و `__pycache__/` را پوشش می‌دهد و تست
`tests/test_hostim_deploy.py::TestColabTrackIsUntouched::test_runtime_artifacts_stay_out_of_git`
همین را بررسی می‌کند.

**بکاپ:** هر volume روزی یک‌بار بکاپ می‌شود، ۷ روز نگه داشته می‌شود و به‌صورت
`.tar.zst` قابل دانلود است:

```bash
hostim backups download ...      # از کنسول یا CLI
```

⚠️ **restore از کنسول هنوز فعال نیست** (خودِ Hostim صریح گفته). یعنی برای
بازیابی باید بکاپ را دانلود کنید و فایل‌ها را از راه Bastion در
`/volumes/automation-data` برگردانید. پیش از هر حذف volume این را در نظر
بگیرید: حذف یک service داده را **برای همیشه** پاک می‌کند.

> نکتهٔ تأییدنشده: پروفایل کروم روی یک volume شبکه‌ای یعنی SQLite و
> فایل‌قفل روی block storage. منطقی است و در Colab هم پروفایل روی دیسک شبکه‌ای
> runtime است، ولی کارایی‌اش روی Hostim **تست نشده**. اگر کروم کند بود یا
> `SingletonLock` داد، پروفایل را به `/tmp` منتقل کنید (با
> `EXTRA_CHROME_FLAGS`) و فقط `automation/` را روی volume نگه دارید.

---

## ۱۱. عیب‌یابی

| نشانه | علت احتمالی | کار |
|---|---|---|
| لاگ کاملاً خالی | build تمام نشده / image غیر amd64 / Command Override اشتباه / volume روی باینری | تب **Build** را ببینید؛ Command Override را خالی کنید؛ `mountPath` را فقط `/data` بگذارید |
| `failed to read dockerfile: open Dockerfile: no such file or directory` | builder پلتفرم Dockerfile را در ریشهٔ context می‌خواهد و مسیر زیرپوشه را اعمال نکرده | **رفع شده:** `Dockerfile` ریشه یک mirror از `hostim/Dockerfile` است. اگر باز هم دیدید، شاخهٔ انتخابی واقعاً آن فایل را ندارد (بخش «mirror ریشه») |
| `groupadd: GID '1000' already exists` (کد خروج ۴) | image پایهٔ ubuntu:24.04 یک حساب stock روی uid/gid 1000 دارد | **رفع شده:** شناسه اول با `getent` پرس‌وجو و حساب stock حذف می‌شود، بعد `automation` روی ۱۰۰۰ ساخته می‌شود. اگر باز دیدید، لاگ همان `RUN` را بفرستید |
| `exec format error` | image برای arm64 ساخته شده | `docker buildx build --platform linux/amd64 -f hostim/Dockerfile .` |
| `FATAL: /data is not writable` | volume تازه هنوز writable نشده یا mount نشده | volume را به اپ attach کنید و یک بار start بزنید؛ یا از Bastion: `chmod -R a+rwX /volumes/automation-data` |
| اپ `unhealthy` | پروب جواب ۲۰۰ نمی‌دهد | `hostim exec browser -- /app/hostim/healthcheck.sh` و سپس لاگ سرور |
| build شکست: `websockify module` | ماژول پایتون websockify در image نیست | Dockerfile عمداً اینجا build را می‌شکند؛ چون بدون آن sidebar و API کامل مرده‌اند |
| build شکست: image بزرگ‌تر از ۴ گیگابایت | Chrome + میزکار سنگین است | `Dockerfile.dockerignore` (ریشه و `hostim/`) فعال است؟ stage نهایی فقط `automation/`، `browser_control.sh` و `hostim/` را COPY می‌کند |
| تصویر noVNC نمی‌آید ولی API کار می‌کند | WebSocket در ingress قطع می‌شود | گام ۶؛ به پشتیبانی Hostim گزارش دهید (این مورد در مستندات تأیید نشده) |
| `Port ... is already occupied` | فقط در اجرای محلی | `LOCAL_PORT=7080 docker compose -f hostim/compose.yaml up` |
| کروم مدام restart می‌شود | RAM کم (پلن `sa-1-1`) | پلن را به `sa-2-2` یا بالاتر ببرید |
| اسکرین‌شات خالی است | نمایش X بالا نیامده | `hostim exec browser -- env HEALTHCHECK_DEEP=1 /app/hostim/healthcheck.sh` |
| متن فارسی در کروم به‌صورت جعبهٔ خالی است | فونت پوشش‌دهندهٔ فارسی در image نیست | در لاگ build باید خط `Persian-capable font families:` باشد؛ اگر build با `no Persian-capable font` شکست، همان لاگ را بفرستید |
| فارسی تایپ نمی‌شود | چیدمان `fa` در X فعال نیست | در لاگ راه‌اندازی باید `keyboard layouts: us,fa` باشد؛ با `Alt+Shift` جابه‌جا شوید؛ چیدمان سیستم‌عامل خودتان انگلیسی بماند |
| کلیدهای ماشین‌حساب عدد تایپ نمی‌کنند | NumLock خاموش است | در لاگ باید `NumLock: on` باشد؛ دستی: `numlockx on` |

---

## ۱۲. امنیت

- **یک ورودی عمومی:** فقط `https://<دامنه>/...`. پورت‌های ۵۹۰۱ و ۹۲۲۲ روی
  loopback می‌مانند و در Dockerfile هم `EXPOSE` نشده‌اند.
- **هر دو لایهٔ احراز هویت لازم‌اند:** رمز VNC برای دیدن دسکتاپ، و
  `AUTOMATION_TOKEN` برای API. مسیر `/automation/api/public/shot/<نام>` عمداً
  بدون توکن است (تا یک AI بتواند تصویر را ببیند) — یعنی نام فایل تنها محافظ
  آن است؛ این رفتار از مسیر Colab به ارث رسیده و تغییر نکرده است.
- **مسیر WebSocket محدود به path نیست:** websockify هر upgrade WebSocket روی
  آن پورت را به ۵۹۰۱ پروکسی می‌کند (در تست من حتی مسیر `/nope` هم پروکسی
  شد). این رفتار خودِ websockify است و در مسیر Colab هم همین‌طور بوده؛ پس
  **رمز VNC تنها دروازهٔ واقعی است** و نباید خالی بماند.
- **Chrome با `--no-sandbox` و `--enable-unsafe-swiftshader` اجرا می‌شود**
  (دقیقاً مثل Colab). یعنی ایزولاسیون مرورگر کاهش یافته است. برای حساب‌های
  حساس بانکی، پزشکی یا تولیدی استفاده نکنید.
- **کانتینر non-root است** (uid 1000)، که سطح حمله را نسبت به حالت root در
  Colab کم می‌کند.
- **کاربر root لازم نیست** و هیچ دسترسی privileged، `--cap-add` یا تغییر
  seccomp درخواست نشده — چون Hostim هیچ‌کدام را مستند نکرده و فرض ما بر
  در دسترس نبودنشان است.
- **`AUTOMATION_TOKEN` را در چت نگذارید.** یک secret مخصوص هر اجراست.
- CAPTCHA و رمز عبور را فقط **انسان** وارد کند؛ قبل از خرید، ارسال پیام،
  ثبت، حذف یا تغییر دسترسی، تأیید انسانی لازم است.
- **ایجنت همکار همین قاعده را می‌شکند، پس دروازه دارد:** اسکریپتی که مدل
  می‌نویسد تا تأیید شما اجرا نمی‌شود، ویرایش کد تأیید را باطل می‌کند،
  محیط اجرا پاک‌سازی شده است (`AUTOMATION_TOKEN` و `VNC_PASSWORD` به فرزند
  نمی‌رسند) و کلید قطع اضطراری همهٔ مسیرهای «اقدام» را با 409 می‌بندد.
  اما **sandbox نیست**: اسکریپت تأییدشده با uid کانتینر و دسترسی کامل به
  volume اجرا می‌شود. کدی را که نمی‌فهمید تأیید نکنید.
- **کنسول SQL ایجنت فقط‌خواندنی است:** اتصال با URI read-only باز می‌شود،
  فقط یک دستور، و فقط `SELECT`/`WITH`. `DELETE`، `UPDATE`، `PRAGMA` و چند
  دستور پشت سر هم رد می‌شوند (با تست). نوشتن فقط از راه CRUD تایپ‌شده.
- **secretهای ایجنت در DB نیستند** و در پاسخ API ماسک می‌شوند؛ فایل نشست
  Telethon (`/data/ai-profile` و session تلگرام) اعتبارنامهٔ کامل‌دسترسی
  هستند و با بکاپ volume جابه‌جا می‌شوند — مثل رمز عبور مراقبشان باشید.
- **پورت 9223 (کروم ایجنت) هم خصوصی است:** روی loopback می‌ماند و `EXPOSE`
  نشده، دقیقاً مثل 9222 و 5901.

---

## ۱۳. چه چیزهایی تست نشده‌اند (صریحاً)

**تست‌شده در محیط من:**

- ✅ کل تست‌های مخزن: `./run_tests.sh` → `ALL CHECKS PASSED`
  (**۳۷۱ تست پایتون**، ۲۰ تست `test_core.mjs`، **۳۷ تست jsdom**، بررسی نحو
  همهٔ اسکریپت‌ها). ۱۲۸ تست پایتون و ۷ تست jsdom از این تعداد مربوط به
  ایجنت همکار است (`tests/test_agent.py`) — جزئیات در بخش ۱۵.۱۰.
- ✅ `tests/test_hostim_deploy.py` (تست قراردادهای Hostim + تست واقعی مسیر
  health روی `AutomationApi` واقعی).
- ✅ `shellcheck` روی `hostim/docker-entrypoint.sh` و `hostim/healthcheck.sh`
  → **بدون هیچ warning**.
- ✅ **اجرای واقعی entrypoint با سرویس‌های stub** (Xvfb/fluxbox/x11vnc/Chrome
  جعلی + **سرور پایتون واقعی**): راه‌اندازی کامل، `0.0.0.0` bind شدن،
  `200` برای `/automation/api/info` بدون توکن، `401` برای `/status` بدون توکن
  و `200` با توکن، سرو شدن `/vnc.html` با تزریق sidebar، **رفت و برگشت واقعی
  WebSocket روی `/websockify` تا پورت ۵۹۰۱**، تولید secret با mode 0600 و
  عدم چاپ آن در لاگ، عدم حضور توکن در `/proc/<pid>/cmdline`، بازاستفاده از
  secret در اجرای دوم، راه‌اندازی مجدد Chrome پس از کشته‌شدن، خاموش‌سازی
  کامل پشته هنگام مرگ یک سرویس هسته‌ای (کد خروج ۱) و خاموش شدن تمیز با
  SIGTERM، بدون هیچ zombie.

**تأییدشده توسط build واقعی روی خودِ Hostim (نه در محیط من):**

دو build واقعی روی Hostim انجام شد. لاگ build دوم این موارد را از «تأییدنشده»
به «تأییدشده» تبدیل کرد:

- ✅ **builder پلتفرم واقعاً BuildKit است** و منبع git را به شکل
  `<repo>.git#<commit>` می‌دهد؛ پس context **ریشهٔ مخزن** است و Dockerfile هم
  از ریشه خوانده می‌شود (دلیل افزودن mirror ریشه).
- ✅ **`apt-get install` کامل موفق بود** — یعنی هر ۱۹ بسته روی Ubuntu 24.04
  (noble) وجود دارد و نصب می‌شود: `novnc`، `websockify`، `x11vnc`، `xvfb`،
  `fluxbox`، `dbus-x11`، `wmctrl`، `xdotool`، `xclip`، `scrot`، `x11-utils`،
  `fonts-liberation`، `iproute2`، `procps`، `openssl`، `curl`،
  `ca-certificates`، `python3` و **`tini`**.
- ✅ **بررسی سختِ ماژول websockify پاس شد** — یعنی `import
  websockify.websocketproxy` داخل image کار می‌کند و fallback به pip لازم
  نشد. بدون این ماژول، sidebar و API کامل مرده بودند.
- ✅ **نصب `.deb` گوگل کروم موفق بود**: `Google Chrome 155.0.8059.39` و
  `update-alternatives` برای `google-chrome`. یعنی `dl.google.com` از محیط
  buildِ Hostim قابل دسترسی است و وابستگی‌های کروم (libnss3، libgtk-3، …) هم
  حل شدند.
- ✅ **شبکهٔ build و registry کار می‌کنند** و لایه‌ها بین تلاش‌ها cache
  می‌شوند (build دوم از مرحلهٔ apt جلو نیفتاد).

**تست‌نشده (و ادعایی درباره‌شان ندارم):**

**تأییدشده توسط استقرار واقعی (build سوم به بعد):**

سه مشکل build که گزارش شده بود رفع شد و image ساخته و deploy شد؛ اپ روی
`https://af9833d9.eu-center.hostim.dev` با volume `automation-data` در حال
اجراست و شما دسکتاپ و سرعتش را تأیید کردید:

- ✅ build تا انتها موفق است. `open Dockerfile: no such file or directory`
  (context ریشهٔ مخزن است، برای همین mirror ریشه لازم بود) و
  `groupadd: GID '1000' already exists` (حساب stock با `getent` پیدا و حذف
  می‌شود؛ تست `TestRuntimeUserIsRobust`) هر دو رفع شدند.
- ✅ Xvfb، fluxbox، x11vnc و Chrome **واقعی** داخل Kata کار می‌کنند: دسکتاپ
  noVNC دیده شد و سرعتش تأیید شد — پس رندر swiftshader هم کار می‌کند.
- ✅ **عبور WebSocket از Traefik v3 واقعی** (مهم‌ترین ریسک تأییدنشده) بدون
  هیچ تنظیم اضافه‌ای کار کرد.
- ✅ HTTPS عمومی، health check و پایداری volume بین restart.

**هنوز تأییدنشده:**

- ❌ **اندازهٔ دقیق image اندازه‌گیری نشد** (باید زیر ۴ گیگابایت باشد؛
  موفق بودن build نشانهٔ خوبی است ولی اندازهٔ صریح دیده نشده).
- ❌ در محیط کاری خودم نه `docker` هست و نه `podman` و دسترسی شبکه به Ubuntu
  archives و Docker Hub بسته است، پس هیچ‌وقت نتوانستم build را محلی جایگزین
  کنم. همهٔ موارد «تأییدشده توسط build واقعی» از لاگی است که شما از داشبورد
  Hostim فرستادید. من وارد حساب شما نشدم و credential ندارم.
- ❌ اندازهٔ `/dev/shm` و رفتار `--no-sandbox` در نشست‌های طولانی.
- ❌ idle timeout Traefik برای نشست‌های طولانی noVNC (اتصال کوتاه تأیید شد).
- ❌ کارایی پروفایل کروم روی volume در بلندمدت.
- ❌ کفایت منابع `sa-2-2` — آنچه تأیید شد `sa-4-8` است. با اضافه شدن
  مرورگر ایجنت (Xvfb دوم + Chrome دوم) مصرف حافظه بالاتر رفته، پس قبل از
  پایین آوردن پلن، اول با `AI_BROWSER=0` آزمایش کنید.
- ❌ **کل لایهٔ ایجنت همکار در کانتینر واقعی** — فهرست دقیقش در بخش ۱۵.۱۰.
- ❌ `hostim templates validate` روی فایل template اجرا نشد (CLI نصب نیست).
- ❌ اینکه `Dockerfile.dockerignore` توسط builder پلتفرم خوانده می‌شود تأیید
  نشد (بی‌ضرر است؛ Dockerfile فقط مسیرهای صریح را COPY می‌کند).

**پیشنهاد من برای اولین آزمایش واقعی:** یک پروژهٔ آزمایشی ۵ روزه بسازید،
با `sa-2-2` و volume رایگان deploy کنید، و فقط گام‌های ۱ تا ۶ بخش ۹ را
اجرا کنید. اگر گام ۶ (WebSocket) پاس شد، بقیهٔ ریسک‌ها عملاً از بین می‌روند.

---

## ۱۴. فارسی، صفحه‌کلید و NumLock

سه مشکلی که در اولین استقرار واقعی گزارش شد و رفع شد:

| مشکل | علت | رفع |
|---|---|---|
| فارسی در کروم تایپ نمی‌شد | noVNC کلیدها را خام به X می‌فرستد؛ **چیدمان X** تعیین می‌کند چه نویسه‌ای تولید شود، نه چیدمان سیستم‌عامل خودتان. چیدمان `fa` اصلاً وجود نداشت | `setxkbmap -model pc104 -layout us,fa -option grp:alt_shift_toggle` هنگام راه‌اندازی |
| فارسی به‌صورت جعبهٔ خالی نمایش داده می‌شد | هیچ فونت عربی/فارسی در image نبود (`fonts-liberation` فقط لاتین دارد) | یک لایهٔ اختصاصی فونت + **بررسی سخت** `fc-list ':lang=fa'` در زمان build |
| NumLock خاموش بود و کلیدهای ماشین‌حساب عدد تایپ نمی‌کردند | یک نشست تازهٔ Xvfb با NumLock خاموش بالا می‌آید، پس keypad جهت‌نما می‌فرستد | `numlockx on` هنگام راه‌اندازی |

### نحوهٔ فارسی تایپ کردن

با **`Alt + Shift`** بین انگلیسی و فارسی جابه‌جا شوید (دقیقاً مثل ویندوز).
دو نکتهٔ عملی:

- چیدمان کیبورد **سیستم‌عامل خودتان را انگلیسی نگه دارید**. اگر هم ویندوز/مک
  شما فارسی باشد و هم X، دو لایهٔ تبدیل روی هم می‌افتند و نویسه‌ها قاطی
  می‌شوند. بگذارید تبدیل فقط داخل X اتفاق بیفتد.
- برچسب کلیدها در noVNC عوض نمی‌شود؛ یعنی وقتی روی `fa` هستید، جای حرف‌ها
  همان چیدمان استاندارد فارسی است (`ش` روی کلید A، `ه` روی کلید I و …). اگر
  جای حرفی را پیدا نکردید، یک نقشهٔ چیدمان استاندارد فارسی را کنار دستتان
  داشته باشید.

### متغیرهای محیطی (بدون نیاز به build دوباره)

| متغیر | پیش‌فرض | کاربرد |
|---|---|---|
| `XKB_LAYOUTS` | `us,fa` | چیدمان‌ها؛ مثلاً `fa` تنها، یا `us,fa,ar` |
| `XKB_OPTIONS` | `grp:alt_shift_toggle` | کلید جابه‌جایی؛ مثلاً `grp:ctrl_shift_toggle` یا `grp:win_switch` |
| `XKB_MODEL` | `pc104` | مدل صفحه‌کلید |
| `NUMLOCK` | `on` | هر مقدار دیگری = دست نزن |

این‌ها را در تب **Envs** اپ بگذارید و restart کنید.

### نکته دربارهٔ `type` در `browser_control.sh`

`xdotool type` کلیدها را با XTest پخش می‌کند و فقط می‌تواند نویسه‌هایی را
تولید کند که چیدمان فعال X می‌شناسد؛ یعنی متن فارسی را **اصلاً** تایپ
نمی‌کرد. حالا `type` به‌صورت خودکار متنی که نویسهٔ غیرASCII دارد (فارسی،
عربی، اموجی، متن ترکیبی) را از راه clipboard با `Ctrl+V` می‌چسباند — همان
کاری که دستورهای `paste` و `url` از قبل می‌کردند. متن ASCII **دقیقاً** همان
مسیر قبلی `xdotool type` را می‌رود، پس رفتار موجود تغییر نکرده است.

> این تنها تغییری است که به یک فایل **مشترک** با مسیر Colab داده شد، چون
> یک اصلاح bug است نه یک رفتار جدید: پیش از این فارسی تایپ نمی‌شد. با ۶ تست
> در `tests/test_browser_control.py` پوشش داده شده، از جمله اینکه متن فارسی
> عیناً به clipboard می‌رسد و دستورهای shell داخلش اجرا نمی‌شوند.

### آنچه تأیید شده و آنچه نشده

- ✅ وجود بسته‌ها در Ubuntu 24.04 بررسی شد: `x11-xkb-utils` (۷.۷+۸build۲)،
  `numlockx` (۱.۲-۹build۱، universe)، `fonts-noto-core` (۲۰۲۰۱۲۲۵-۲).
  بستهٔ اختصاصی فارسی/عربی با نام `*arabic*` یا `*naskh*` در noble
  **وجود ندارد**، برای همین لایهٔ فونت به یک نام اتکا نمی‌کند.
- ✅ لایهٔ فونت **نتیجه** را بررسی می‌کند نه نام بسته را: اگر هیچ فونت
  پوشش‌دهندهٔ فارسی نصب نشود، build با پیام
  `ERROR: no Persian-capable font could be installed` **می‌شکند** و image
  خراب بیرون نمی‌آید. در لاگ build موفق باید خط
  `Persian-capable font families:` را با نام چند فونت ببینید.
- ❌ **نمایش و تایپ واقعی فارسی در کرومِ روی Hostim هنوز توسط من تأیید
  نشده** — من نه Docker دارم و نه مرورگر گرافیکی. بعد از Rebuild باید خودتان
  ببینید: در لاگ راه‌اندازی باید `keyboard layouts: us,fa` و `NumLock: on`
  باشد، و در صفحهٔ noVNC یک سایت فارسی باز کنید و چند حرف تایپ کنید.

---

## ۱۵. ایجنت همکار (Coworker Agent)

یک بخش جدید در sidebar که قبلاً «هوش مصنوعی» بود و فقط یک پرامپت متنی می‌ساخت.
حالا یک **همکار** است: کلید اختصاصی خودش را دارد، به داده‌های برنامه دسترسی
کامل دارد، می‌تواند کد بنویسد و در جریان‌های اتوماسیون دخالت کند، و
برنامه‌های خودش را زمان‌بندی کند.

**مرزهایی که عمداً گذاشته شده‌اند:**

- **به GitHub و سورس مخزن دسترسی ندارد.** اسکریپت‌ها فقط داخل `/data` اجرا
  می‌شوند، هیچ route ای محتوای فایل‌های مخزن را برنمی‌گرداند، و تنها
  «دیتابیس» در دسترس، SQLite خودِ ایجنت و store جریان‌هاست.
- **هیچ کاری بدون تأیید شما «اجرا» نمی‌شود** مگر اینکه خودتان صریحاً
  اجازه‌اش را داده باشید (کلید قطع اضطراری، دروازهٔ تأیید اسکریپت، و
  `autoClick` خاموش برای کپچا).
- **مسیر Colab دست‌نخورد است.** اگر ایجنت attach نشود (`--no-agent`) هیچ
  route جدیدی هم وجود ندارد؛ این با تست
  `test_the_agent_routes_are_only_present_when_an_agent_is_attached` پوشش
  داده شده.

### ۱۵.۱ داده‌ها کجا می‌روند

| مسیر | محتوا | نکته |
|---|---|---|
| `/data/automation/agent.db` | تنظیمات، وظایف، اسکریپت‌ها، یادداشت‌ها، audit | SQLite؛ **secret داخلش نیست** |
| `/data/automation/agent-secrets.json` | کلیدها (API key، توکن ربات، apiHash) | mode **0600**، هرگز در DB/لاگ/پاسخ API نمی‌آید |
| `/data/public/` | تصویرهای منتشرشده برای کپچا | مثل مسیر Colab بدون توکن قابل خواندن است |
| `/data/ai-profile/` | پروفایل کرومِ ایجنت (نشست چت) | mode 0700؛ یک **اعتبارنامه** است |

همهٔ این‌ها روی volume `automation-data` هستند، پس بین استقرارها می‌مانند و
در بکاپ روزانهٔ Hostim هم هستند.

### ۱۵.۲ زیرتب‌ها

> زیرتب «دستیار» به تب **چت** منتقل شد (بخش ۱۶.۱)؛ بقیه سر جایشان هستند و دو
> زیرتب تازه — «کلید ایجنت» و «نشانگر» — اضافه شد.

| زیرتب | کارش | چه چیزی ذخیره می‌کند |
|---|---|---|
| **کلید ایجنت** | کلید API تولیدشده برای ایجنت: نمایش/کپی/ساخت کلید تازه، فعال یا قطع کردن، و فهرست همهٔ مسیرهایی که ایجنت می‌تواند صدا بزند (بخش ۱۶.۳) | `agentKey.*` + خود کلید در فایل سری‌ها |
| **تلگرام** | هر دو کانال (ربات و اکانت شخصی)، مسیریابی هر نوع پیام، گیرنده‌ها، پیام آزمایشی **با گیرنده**، و پیام دلخواه (بخش ۱۶.۴) | `telegram.*` + secretها |
| **نشانگر** | اندازه و رنگ نشانگر ماوس و موج کلیک (بخش ۱۶.۵) | `cursor.*` |
| **وظایف** | زمان‌بندی: `at` (یک بار)، `every` (بازه)، `cron` (پنج فیلد). هدف می‌تواند یک flow، یک اسکریپت تأییدشده، یا یک پرسش از مدل باشد | ردیف وظیفه + `next_run_at` + آخرین نتیجه |
| **کپچا** | چیدن زنجیرهٔ حل و روشن/خاموش کردن اجرای خودکار | `captcha.*` |
| **اسکریپت** | کدی که ایجنت نوشته: `pending` → تأیید/رد → اجرا؛ خروجی و exit code و مدت | ردیف اسکریپت + آخرین خروجی |
| **داده‌ها** | یادداشت‌ها، لاگ audit، و یک کنسول SQL **فقط‌خواندنی** | - |
| **کلیدها** | کلید API (اگر مسیر HTTP را انتخاب کردید)، selectorهای مرورگر ایجنت، «باز کردن سایت چت» و «تصویر نمایش ایجنت» برای لاگین دستی | `ai.*` |

### ۱۵.۳ کلید قطع اضطراری

دکمهٔ بالای بخش ایجنت. وقتی روشن است:

- هیچ اسکریپتی اجرا نمی‌شود، هیچ وظیفه‌ای شلیک نمی‌شود (scheduler آن را
  `skipped` ثبت می‌کند)، هیچ اقدام کپچایی انجام نمی‌شود، و `/agent/chat` هم
  جواب **409** می‌دهد.
- وضعیت در `settings` ذخیره می‌شود، پس بعد از restart هم روشن می‌ماند.

### ۱۵.۴ زمان‌بندی

- `cron` استاندارد پنج فیلدی است و وقتی **هم** روز ماه و **هم** روز هفته
  محدود شده باشند از قاعدهٔ OR پیروی می‌کند (مثل cron واقعی) —
  `0 0 1 * 1` یعنی «اول ماه **یا** هر دوشنبه». این با تست پوشش داده شده چون
  اشتباه گرفتنش باعث می‌شود بیشتر اجراها بی‌صدا حذف شوند.
- **سریال است:** یک mouse بیشتر نداریم، پس دو وظیفه هم‌زمان اجرا نمی‌شوند.
  اگر دسکتاپ مشغول باشد، وظیفه `deferred` می‌شود و ۳۰ ثانیه بعد دوباره
  تلاش می‌کند (نه اینکه fail شود).
- `at` بعد از اجرا **خودش غیرفعال می‌شود** وگرنه تا ابد شلیک می‌شد.
- «همین حالا» در پس‌زمینه اجرا می‌شود و فوراً `queued` برمی‌گرداند، چون یک
  flow می‌تواند دقیقه‌ها طول بکشد و نگه‌داشتن درخواست HTTP پشت ingress
  (که idle timeoutش را کسی اندازه نگرفته) یعنی حدس زدن. نتیجه در ستون
  «آخرین نتیجه» همان جدول ظاهر می‌شود.

### ۱۵.۵ اسکریپت‌ها و دروازهٔ تأیید

ایجنت می‌تواند `python3` یا `bash` بنویسد. اجرای آن **فقط** بعد از تأیید شما:

1. اسکریپت تازه همیشه `pending` است (حتی اگر ایجنت خودش آن را ساخته باشد).
2. اگر `code` یک اسکریپت تأییدشده ویرایش شود، وضعیت **به `pending` برمی‌گردد**
   — وگرنه کد تازه سوار تأیید قدیمی می‌شد.
3. اجرا قبل از تأیید → **409** با پیام «approve it before running it».
4. timeout پیش‌فرض ۶۰ ثانیه (۱ تا ۳۶۰۰ قابل تنظیم)؛ در پایان timeout
   exit code **124** ثبت می‌شود.
5. هم‌زمان فقط یک اسکریپت اجرا می‌شود؛ دومی 409 می‌گیرد.
6. محیط اجرا **پاک‌سازی شده** است: `AUTOMATION_TOKEN`، `VNC_PASSWORD` و بقیهٔ
   متغیرهای حساس به فرزند نمی‌رسند. آنچه می‌رسد: `AUTOMATION_API_BASE`،
   `AUTOMATION_DATA_DIR`، `AUTOMATION_SCRIPT_ID`، `DISPLAY`، `PATH`.
7. اگر اسکریپت واقعاً به توکن نیاز دارد، باید عمداً `scripts.passToken` را
   روشن کنید (پیش‌فرض خاموش).

> **صراحتاً: این یک sandbox نیست.** اسکریپت با همان uid کانتینر اجرا می‌شود و
> به volume دسترسی دارد. دروازهٔ تأیید یک محافظ *انسانی* است، نه ایزولاسیون
> فنی. کدی که نمی‌فهمید را تأیید نکنید.

### ۱۵.۶ کپچا — فقط راه‌حل‌های رایگان

سه لایه، به همین ترتیب (قابل تغییر در زیرتب «کپچا»):

1. **vision** — تصویر به مدل داده می‌شود و مدل باید JSON برگرداند:
   `kind`, `confidence`, `summary`, `actions`, `solvableByVision`, `reason`.
   مختصات **اعتبارسنجی می‌شوند**: بیرون از ابعاد تصویر = رد؛ نوع ناشناخته =
   رد؛ بیشتر از ۹ اقدام = بریده می‌شود. تصویر از `/data/public/` با URL عمومی
   به مدل داده می‌شود.
2. **human** — پیام تلگرام با تصویر + لینک دسکتاپ noVNC، و اجرا در حالت
   `waiting` می‌ماند تا شما تأیید کنید (همان مکانیزم قبلی مخزن).
3. **extension** — جای افزونهٔ آماده. **با صداقت:**

| افزونه | رایگان | چه چیزی را حل می‌کند | وضعیت |
|---|---|---|---|
| **Buster** | بله، GPL-3.0 | **فقط چالش صوتی reCAPTCHA** | در image نصب نیست؛ تشخیص گفتار در کرومِ بدون صدا **تأییدنشده**؛ به گفتهٔ خود سازنده استفادهٔ پرتکرار در یک روز ریسک بلاک موقت دارد |
| **NopeCHA** | لایهٔ رایگان ۱۰۰ تشخیص در ۲۴ ساعت | reCAPTCHA، hCaptcha، FunCAPTCHA، AWS WAF، متنی | از ۲۰۲۳ بسته‌متن؛ بیش از سهمیه نیاز به اکانت دارد |

سرویس‌های پولی (2Captcha، Anti-Captcha، CapMonster Cloud، CapSolver و …)
**عمداً هیچ جایشان نیست** — طبق خواستهٔ شما.

**مهم‌ترین نکتهٔ صادقانه:** بینایی ماشین کپچای **رفتاری** را حل نمی‌کند.
reCAPTCHA v2 و hCaptcha بر اساس رفتار مرورگر امتیاز می‌دهند نه پیکسل، پس در
prompt از مدل خواسته شده در این حالت `solvableByVision:false` برگرداند تا
مستقیم به لایهٔ human برود. وانمود نمی‌کنیم که «کپچا حل شد».

`captcha.autoClick` **پیش‌فرض خاموش** است: نتیجه فقط یک **پیشنهاد** است و
شما باید دکمهٔ اجرا را بزنید. روشن کردنش یعنی کلیک‌های مدل بدون تأیید روی
صفحه اجرا می‌شوند.

> عبور خودکار از کپچا می‌تواند شرایط استفادهٔ سایت مقصد را نقض کند. فلسفهٔ
> خودِ این مخزن «تحویل امن به انسان» است؛ این لایه‌ها را برای سایت‌هایی
> استفاده کنید که اجازه‌اش را دارید.

### ۱۵.۷ تلگرام — هم ربات، هم حساب خودتان

| | ربات (پیش‌فرض پیشنهادی) | حساب کاربری (userbot) |
|---|---|---|
| چه چیزی لازم دارد | توکن از **@BotFather** | `apiId` و `apiHash` از **my.telegram.org** + بستهٔ Telethon |
| محدودیت | ربات فقط به کسی می‌تواند پیام بدهد که **قبلاً به آن پیام داده باشد** | مثل یک کاربر عادی؛ ریسک محدودیت حساب در استفادهٔ سنگین |
| نصب | چیزی لازم نیست (فقط `urllib`) | در Dockerfile به‌صورت **best-effort** نصب می‌شود؛ اگر PyPI نرسد، build نمی‌شکند و حالت account با پیام روشن خطا می‌دهد |

**پیدا کردن `chat_id`:** در زیرتب تلگرام دکمهٔ «کشف گفتگوها» هست
(`/agent/telegram/targets`). اول **یک پیام به ربات خودتان بدهید** (یا ربات را
به گروه اضافه کنید و یک پیام در گروه بفرستید)، بعد دکمه را بزنید؛ فهرست
گفتگوها با عنوان و id نشان داده می‌شود و می‌توانید انتخاب کنید. «تست ارسال»
هم هست.

حالت پیش‌فرض **off** است: تا وقتی خودتان انتخاب نکنید هیچ پیامی به هیچ‌کس
نمی‌رود. همهٔ ارسال‌ها fire-and-forget هستند؛ خرابی تلگرام **هرگز** اجرای
اتوماسیون را متوقف نمی‌کند (با تست پوشش داده شده) و در لاگ اجرا ثبت می‌شود.

فایل نشست Telethon روی volume است و یک **اعتبارنامهٔ کامل‌دسترسی** به حساب
شماست: مثل رمز عبور با آن رفتار کنید و در بکاپ/اشتراک‌گذاری volume دقت کنید.

### ۱۵.۸ مرورگر اختصاصی ایجنت — مسیر بدون API key

خواستهٔ شما این بود که ایجنت **بدون کلید API** هم کار کند: خودش سایت
DeepSeek را باز کند، پرامپت را تایپ کند، جواب را بخواند و بعد برنامه‌ریزی
کند. این مسیر پیاده‌سازی شده و **پیش‌فرض است** (`ai.provider = ai-browser`):

- یک Xvfb **دوم** روی `:2` و یک Chrome **دوم** با CDP روی پورت **9223**
  (پورت 9222 و نمایش `:1` مال اتوماسیون اصلی است؛ پنجرهٔ دوم روی `:1` باعث
  می‌شد `browser_control.sh url` پنجرهٔ اشتباه را انتخاب کند).
- پروفایل جدا در `/data/ai-profile` → **یک بار دستی لاگین می‌کنید و نشست
  می‌ماند**.
- این Chrome توسط entrypoint با `AI_BROWSER=1` بالا می‌آید و **غیرهسته‌ای**
  supervise می‌شود: اگر بمیرد دوباره راه می‌افتد، ولی مرگش کل پشته را خاموش
  نمی‌کند و اگر بالا نیاید فقط یک `WARN` است (اتوماسیون اصلی کار می‌کند).

**لاگین یک‌باره:** زیرتب «کلیدها» → «باز کردن سایت چت» (سایت را در مرورگر
ایجنت باز می‌کند) → «تصویر نمایش ایجنت» (اسکرین‌شات `:2` را در sidebar نشان
می‌دهد). از راه noVNC هم می‌توانید مستقیم وارد `:2` شوید و دستی لاگین کنید.

| متغیر | پیش‌فرض | کاربرد |
|---|---|---|
| `AI_BROWSER` | `1` | `0` = مرورگر ایجنت اصلاً بالا نیاید |
| `AI_DISPLAY` | `:2` | نمایش Xvfb اختصاصی ایجنت |
| `AI_SCREEN_SIZE` / `AI_SCREEN` | `1366x768x24` | اندازهٔ نمایش ایجنت |
| `AI_CDP_PORT` | `9223` | پورت DevTools کروم ایجنت (خصوصی می‌ماند) |
| `AI_START_URL` | `https://chat.deepseek.com/` | صفحهٔ شروع |
| `PUBLIC_BASE` | خالی | دامنهٔ عمومی؛ برای لینک دسکتاپ و تصویر کپچا در پیام تلگرام. در Hostim می‌توانید `https://<دامنه>` را بگذارید |

**چطور جواب را می‌خواند:** بدون هیچ سیگنال اختصاصیِ سایت — آخرین پاسخ را
polling می‌کند تا متنش **دو بار پشت سر هم تغییر نکند**. این برای هر رابط چت
جریان‌یافتنی کار می‌کند و به markup یک سایت خاص وابسته نیست.

**selectorها قابل ویرایش‌اند:** فهرست انتخابگرهای ورودی/دکمهٔ ارسال/پاسخ‌ها
در تنظیمات (`ai.providers`) ذخیره می‌شود و بدون restart اثر می‌کند. اگر سایت
markup را عوض کرد، از زیرتب «کلیدها» اصلاحش می‌کنید. وقتی هیچ ورودی‌ای پیدا
نشود، خطا **عنوان صفحه، URL و ۴۰۰ نویسهٔ اول متن صفحه** را برمی‌گرداند تا
بفهمید مثلاً به دیوار لاگین خورده‌اید.

**ارسال تصویر:** DevTools نمی‌تواند بدون `DOM.setFileInputFiles` یک input فایل
را پر کند، پس تصویر با `xclip` روی clipboard نمایش `:2` گذاشته می‌شود و با
`xdotool key ctrl+v` چسبانده می‌شود؛ به‌علاوه URL عمومی تصویر هم در متن
پرامپت می‌آید.

**صداقت دربارهٔ این مسیر:**

- ❌ خودکارسازی رابط وب یک سرویس چت می‌تواند **شرایط استفادهٔ همان سرویس** را
  نقض کند. این تصمیم شماست، نه چیزی که من تأییدش کنم.
- ❌ selectorهای رابط وب شکننده‌اند؛ با هر به‌روزرسانی سایت ممکن است از کار
  بیفتند (به همین دلیل قابل ویرایش‌اند و خطا صفحه را توصیف می‌کند).
- ❌ این مسیر **در کانتینر واقعی تأیید نشده** (بخش ۱۵.۱۰).

### ۱۵.۹ مسیر جایگزین: HTTP API

اگر مرورگر ایجنت جواب نداد، `ai.provider` را روی `http-api` بگذارید و
`ai.baseUrl` (پیش‌فرض `https://api.deepseek.com/v1`)، `ai.model` و کلید را در
زیرتب «کلیدها» وارد کنید. هر سرویس سازگار با OpenAI کار می‌کند. کلید فقط در
`agent-secrets.json` با mode 0600 می‌ماند، در پاسخ API به شکل
`{"secret": true, "set": true}` ماسک می‌شود، و در لاگ audit **فقط نام کلید**
ثبت می‌شود نه مقدارش (هر سه با تست پوشش داده شده‌اند).

### ۱۵.۱۰ آنچه تست شده و آنچه تأییدنشده

**تست‌شده در محیط من (۱۲۸ تست در `tests/test_agent.py`، بدون شبکه و بدون X):**

- ✅ secret هرگز وارد `agent.db` نمی‌شود؛ فایل secret mode 0600 است؛ فهرست
  تنظیمات مقدارها را ماسک می‌کند؛ audit فقط نام کلید را ثبت می‌کند.
- ✅ ریاضی زمان‌بندی: cron (شامل قاعدهٔ OR)، `*/15`، بازهٔ ساعت، عبور به روز
  بعد، `at` گذشته‌نگر؛ و رد شدن cron نامعتبر.
- ✅ scheduler: شلیک و زمان‌بندی مجدد، غیرفعال شدن `at`، `deferred` وقتی
  دسکتاپ مشغول است، ثبت خطا بدون متوقف کردن بقیه، `skipped` زیر کلید قطع
  اضطراری، **و اینکه دو وظیفه هرگز هم‌زمان اجرا نمی‌شوند**.
- ✅ دروازهٔ اسکریپت با **اجرای واقعی** `python3` و `bash`: رد شدن `pending`،
  اجرای موفق بعد از تأیید، exit code و stderr، timeout → 124، پاک‌سازی محیط
  (توکن و رمز VNC به فرزند نمی‌رسند)، opt-in بودن `scripts.passToken`، و
  بازگشت به `pending` بعد از ویرایش کد.
- ✅ تلگرام: ساخت پیام/عکس multipart، کشف گفتگوها از `getUpdates` (بدون
  تکراری)، قالب فارسی handoff/result با لینک دسکتاپ، گزارش شکست هر گیرنده،
  و اینکه notify_handoff **هرگز** استثنا را به اجرا برنمی‌گرداند.
- ✅ کپچا: استخراج JSON از پاسخ مدل (فارسی هم سالم می‌ماند)، رد مختصات بیرون
  تصویر، رد نوع ناشناخته، سقف ۹ اقدام، `solvableByVision:false` → human،
  خاموش بودن `autoClick` به‌صورت پیش‌فرض (بدون هیچ کلیک)، و مسدود شدن اجرا زیر
  کلید قطع اضطراری.
- ✅ مرورگر ایجنت: گزارش «در دسترس نیست» با نام بردن `AI_BROWSER=1`، تشخیص
  پایان پاسخ با پایداری متن، timeout وقتی پاسخی نیاید، **و اینکه پرامپت به
  عنوان داده به JavaScript تزریق می‌شود نه به عنوان کد** (پرامپتی که رشته را
  می‌بندد فرار نمی‌کند).
- ✅ LLM: ساخت درخواست سازگار با OpenAI، base64 کردن تصویر، خطای روشن وقتی
  کلید نیست، و اینکه مقدار کلید در هیچ خروجی‌ای نمی‌آید.
- ✅ قلاب اعلان در engine: قبل از wait به notifier خبر می‌دهد، notifier خراب
  اجرا را نمی‌شکند و در لاگ ثبت می‌شود، و بدون notifier هیچ چیز عوض نمی‌شود.
- ✅ سطح HTTP: همهٔ routeهای ایجنت بدون توکن **401**، تنظیم ناشناخته **400**،
  اسکریپت تأییدنشده **409**، کلید قطع اضطراری **409**، SQL نوشتنی **400**،
  چند دستور پشت سر هم **400**، مسیر ناشناخته **404**، و به‌روزرسانی جزئی
  وظیفه (فقط `enabled`) بدون پاک شدن بقیهٔ تعریف.
- ✅ **تطبیق دو نیمه:** هر مسیری که sidebar صدا می‌زند در جدول route وجود
  دارد، و هر route ایجنت توسط یک زیرتب صدا زده می‌شود (تست
  `RouteCoverageTest` — چون این UI یک بار از روی حافظه نوشته شد).

**تأییدنشده (ادعایی درباره‌شان ندارم):**

- ❌ **هیچ‌کدام از این‌ها در کانتینر واقعی اجرا نشده‌اند.** من Docker ندارم.
  بعد از Rebuild باید خودتان ببینید.
- ❌ لایهٔ نصب **Telethon** در Dockerfile (best-effort است؛ اگر نرسد build
  نمی‌شکند و در لاگ `WARN` می‌آید). حالت userbot بدون آن کار نمی‌کند.
- ❌ بالا آمدن Xvfb دوم روی `:2` و Chrome دوم روی 9223 داخل Kata.
- ❌ **ارسال واقعی پیام تلگرام** (من هیچ توکنی ندارم و هیچ پیامی ارسال نشد).
- ❌ **چت واقعی با DeepSeek از راه مرورگر**: لاگین دستی، پیدا شدن ورودی،
  خواندن پاسخ، و چسباندن تصویر با clipboard.
- ❌ thread زمان‌بند داخل کانتینر در بازهٔ طولانی (و اینکه بعد از restart
  اپ، `next_run_at`ها درست بازیابی شوند).
- ❌ UI ایجنت در یک مرورگر واقعی (تست‌های من jsdom هستند، نه رندر واقعی).
- ❌ سهم منابع: مرورگر دوم + Xvfb دوم یعنی حافظهٔ بیشتر. روی `sa-2-2`
  آزمایش نشده؛ فعلاً `sa-4-8` تأییدشده است. اگر خواستید پلن را پایین
  بیاورید، اول `AI_BROWSER=0` را امتحان کنید.

**ترتیب پیشنهادی برای اولین آزمایش واقعی:**

1. Rebuild و بررسی لاگ راه‌اندازی برای خط‌های `AI browser:` و `ai-chrome`.
2. زیرتب «کلیدها» → «تصویر نمایش ایجنت»: باید دسکتاپ `:2` با صفحهٔ چت دیده
   شود. همان‌جا **دستی لاگین** کنید.
3. زیرتب «دستیار» یک درخواست ساده بدهید و ببینید جواب مدل برمی‌گردد یا خطا
   صفحه را توصیف می‌کند.
4. تلگرام: یک پیام به ربات بدهید → «کشف گفتگوها» → «تست ارسال».
5. یک وظیفهٔ `every` با بازهٔ `2m` روی یک flow ساده بسازید و دو دقیقه صبر
   کنید.
6. فقط در آخر `captcha.autoClick` را روشن کنید — و پیشنهاد من این است که
   **خاموش بماند**.

---

## ۱۶. تازه‌های این نسخه

هشت تغییر که به درخواست خودت اضافه شد. وضعیت تأیید هر کدام در
[`hostim/CHECKLIST.fa.md`](CHECKLIST.fa.md) آمده؛ اینجا «چطور کار می‌کند» و
«چطور امتحانش کنم» نوشته شده.

ترتیب تب‌های پنل عوض شده است:

```
چت | کتابخانه | مراحل | ضبط | صفحه‌ها | اسکرین‌شات‌ها | متن‌ها | ایجنت | لاگ
```

پنل روی **چت** باز می‌شود و آخرین تبی که انتخاب کرده‌ای را به خاطر می‌سپارد.

### ۱۶.۱ تب چت

یک صفحهٔ گفت‌وگوی معمولی، اما **بدون کلید API پولی**: جواب از مرورگر اختصاصی
ایجنت می‌آید (به‌طور پیش‌فرض `chat.deepseek.com` روی نمایش `:2`، پورت CDP ۹۲۲۳).

چون یک مرورگر واقعی دارد تایپ می‌کند و منتظر جواب است، یک پرسش می‌تواند چند
دقیقه طول بکشد. پس چت **ناهمزمان** است:

1. `POST /agent/chat/send` پیام تو را ذخیره می‌کند و یک ردیف «دستیار» با وضعیت
   `thinking` می‌سازد؛
2. یک thread جداگانه پرامپت را به مرورگر ایجنت می‌دهد؛
3. پنل هر ۲ ثانیه `GET /agent/chat/messages` را می‌خواند تا جواب برسد.

نتیجه: اگر پنل را ببندی یا صفحه را refresh کنی، جواب گم نمی‌شود.

| کنترل | کار |
|---|---|
| **مدل / پروفایل** | از پروفایل‌های مرورگر ایجنت انتخاب می‌کند (`deepseek`, `generic`, …). افزودن مدل جدید = افزودن یک پروفایل، بدون تغییر UI |
| **زمینهٔ اتوماسیون** | اگر روشن باشد، وضعیت اجرا، نام جریان‌ها، عملیات کتابخانه و متن صفحهٔ فعال **فقط‌خواندنی** به پرامپت اضافه می‌شود |
| **⤓ اعمال به‌عنوان جریان** | وقتی مدل در جوابش JSON جریان بدهد، این دکمه ظاهر می‌شود؛ با یک کلیک در «جریان‌های ذخیره‌شده» ذخیره و در تب مراحل باز می‌شود |
| **🧹 پاک کردن گفت‌وگو** | تاریخچه را خالی می‌کند (در audit ثبت می‌شود) |
| **ابزارهای دستیار** | همان گردش کار قبلی: «🧩 کپی پرامپت» (پرامپت کامل با لینک عمومی اسکرین‌شات) و «🤖 پرسش مستقیم» و «وارد کردن JSON» |

```bash
curl -s -X POST -H "x-agent-key: $KEY" -H 'Content-Type: application/json' \
  -d '{"text":"سلام، چه کاری می‌توانی بکنی؟","provider":"deepseek"}' \
  https://af9833d9.eu-center.hostim.dev/automation/api/agent/chat/send
curl -s -H "x-agent-key: $KEY" \
  https://af9833d9.eu-center.hostim.dev/automation/api/agent/chat/messages
```

> اگر مرورگر ایجنت بالا نیامده باشد، پاسخ با خطای صریح ذخیره می‌شود
> (`status: failed` + متن خطا) و پنل همان را نشان می‌دهد — نه یک ۵۰۰ خالی.

### ۱۶.۲ تب کتابخانه — عملیات

«عملیات» یعنی **چند گام با یک نام** که یک بار می‌سازی و هر بار استفاده می‌کنی.
پنج عملیات آماده دارد (با ری‌استارت برمی‌گردند، اما اگر ویرایششان کنی دست‌نخورده
می‌مانند و با «بازنشانی» به نسخهٔ کارخانه برمی‌گردند):

| عملیات | چه می‌کند |
|---|---|
| اتصال به ایجنت lmarena | سایت را در مرورگر ایجنت باز می‌کند و ایجنت را می‌آورد |
| شناسایی یا حل کپچا | اسکرین‌شات + زنجیرهٔ حل رایگان؛ خروجی **پیشنهاد** است |
| اتصال به دیپ‌سیک | `chat.deepseek.com` را باز می‌کند |
| پرامپت دادن به دیپ‌سیک و دریافت پیامش | تایپ پرامپت، صبر تا پایان پاسخ، و برگرداندن متن |
| عکس و متن صفحهٔ فعال | اسکرین‌شات + خواندن متن صفحه در گزارش اجرا |

سه دکمهٔ اصلی هر کارت:

- **▶ اجرا** — گام‌ها را مستقیم به موتور می‌دهد. اگر اجرای دیگری مالک ماوس باشد
  جواب **۴۰۹** است (دو راننده روی یک نشانگر = خراب شدن اجرا).
- **📂 باز کردن در مراحل** — همان چیزی که خواستی: گام‌ها داخل تب «مراحل»
  می‌روند تا ویرایششان کنی و بعد اجرا کنی. نام عملیات، نام جریان می‌شود.
- **✎ ویرایش** — نام، توضیح، برچسب‌ها و گام‌ها. مقدارهای هر گام را به‌صورت JSON
  می‌نویسی؛ فیلدهای لازمِ هر نوع گام در placeholder همان کادر آمده و فهرست کامل
  با توضیح فارسی از `GET /agent/operations` (کلید `stepTypes`) می‌آید.

```bash
curl -s -H "x-agent-key: $KEY" \
  https://af9833d9.eu-center.hostim.dev/automation/api/agent/operations | python3 -m json.tool | head -40
```

### ۱۶.۳ کلید API ایجنت

یک کلید، با تاریخ ساخت. زیرتب **ایجنت ← کلید ایجنت**:

| دکمه | کار |
|---|---|
| 👁 نمایش کلید | خود کلید را نشان می‌دهد. **این کار در audit ثبت می‌شود** (`agentKey.revealed`) |
| 📋 کپی کلید | در clipboard می‌گذارد |
| 🔄 ساخت کلید تازه | کلید قبلی **همان لحظه** از کار می‌افتد |
| رادیو فعال/غیرفعال | دسترسی ایجنت را وصل یا قطع می‌کند |
| ⏸ قطع دسترسی | همان غیرفعال کردن، اما صریح و با یک کلیک |

قطع دسترسی: **آنی** است (بدون deploy مجدد) و **در دیتابیس می‌ماند** (بعد از
ری‌استارت هم قطع است). توکن اصلی `AUTOMATION_TOKEN` همیشه کار می‌کند، یعنی هیچ‌وقت
خودت را بیرون نمی‌اندازی.

سه راه فرستادن کلید — هر سه یک کلید را می‌رسانند:

```
Authorization: Bearer <کلید>
x-agent-key: <کلید>
?k=<کلید>          ← برای ابزارهایی که هدر سفارشی نمی‌فرستند (مثل نسخهٔ وب ChatGPT)
```

**اگر دسترسی قطع باشد، همهٔ این مسیرها با کد ۴۰۳ رد می‌شوند.** کلید اشتباه ۴۰۱
می‌دهد (یعنی «نمی‌شناسمت») و کلید قطع‌شده ۴۰۳ (یعنی «می‌شناسمت ولی اجازه نداری») —
عمداً از هم جدا هستند تا بفهمی کدام اتفاق افتاده.

فهرست مسیرها: `GET /agent/api-index` همان چیزی را می‌دهد که در پنل می‌بینی، با
**آدرس مطلق** (دامنهٔ عمومی + `/automation/api`) و یک توضیح یک‌خطی فارسی و
انگلیسی برای هر مسیر. دکمهٔ «📋 کپی همه» کل فهرست را یک‌جا کپی می‌کند تا به یک
AI دیگر بدهی.

```bash
KEY='<کلید ایجنت>'
BASE=https://af9833d9.eu-center.hostim.dev/automation/api
curl -s -H "x-agent-key: $KEY" $BASE/agent/api-index | python3 -m json.tool | head -30
curl -s -H "Authorization: Bearer $KEY" $BASE/status
curl -s "$BASE/status?k=$KEY"
```

> یک تست خودکار هست که نمی‌گذارد این فهرست از جدول مسیرهای واقعی سرور جدا شود:
> اگر مسیری اضافه یا کم شود و فهرست به‌روز نشود، تست قرمز می‌شود.

### ۱۶.۴ تلگرام — هر دو کانال، با مسیریابی

زیرتب **تلگرام** هفت بخش شماره‌دار دارد:

1. **کانال‌ها** — حالت ارسال: `خاموش` / `فقط ربات` / `فقط اکانت شخصی` / **`هر دو`**،
   به‌علاوهٔ جدول وضعیت هر کانال (چه چیزی کم دارد، فایل نشست کجاست) و دکمهٔ probe.
2. **مسیریابی پیام‌ها** — برای هر «منظور» جداگانه انتخاب کن از کدام کانال برود:
   `تحویل به انسان`، `کپچا`، `وظایف زمان‌بندی‌شده`، `پیام دستی/ایجنت`.
   «خودکار» یعنی از همان حالت کلی بالا پیروی کند.
3. **ربات** — توکن از `@BotFather`. مقدار ذخیره‌شده هرگز نمایش داده نمی‌شود؛
   کادر خالی یعنی «تغییر نده».
4. **اکانت شخصی** — `api_id` و `api_hash` از `my.telegram.org`، بعد «ارسال کد» و
   «پایان لاگین». فایل نشست روی `/data` می‌ماند و بعد از ری‌استارت هم اعتبار دارد.
   (اگر `telethon` در ایمیج نصب نشده باشد، خطا صریح می‌گوید از حالت ربات استفاده کن.)
5. **گیرنده‌ها** — «پیدا کردن چت‌ها» از `getUpdates`، افزودن با ＋ و حذف با 🗑.
6. **پیام آزمایشی** — **حتماً یک گیرنده می‌خواهد**: بدون گیرنده سرور ۴۰۰ می‌دهد
   (`a test message needs a target`). کانال و متن هم قابل انتخاب است.
7. **پیام دلخواه** — دقیقاً همان کاری که ایجنت می‌کند: متن دلخواه، یک گیرنده یا
   همهٔ گیرنده‌ها، کانال دلخواه، و **منظور** پیام. با عوض کردن منظور می‌توانی
   مسیریابی بخش ۲ را واقعاً امتحان کنی.

```bash
curl -s -X POST -H "x-agent-key: $KEY" -H 'Content-Type: application/json' \
  -d '{"target":-100123456,"channel":"bot","purpose":"jobs","text":"سلام از ایجنت"}' \
  $BASE/agent/telegram/send
```

پاسخ می‌گوید از کدام کانال‌ها رفت و چند تا موفق/ناموفق بود:
`{"sent":1,"failed":0,"channels":["bot"]}`.

### ۱۶.۵ نشانگر ماوس

نشانگر **زرد و بزرگ‌تر از معمول** روی دسکتاپ مجازی، و با هر کلیک یک **موج** در
نقطهٔ برخورد باز می‌شود تا معلوم شود کلیک کجا نشست.

- پیش‌فرض: ۴۴ پیکسل، زرد `#ffd400`، لبهٔ تیره `#1b1b1b`، موج روشن.
- زیرتب **نشانگر**: روشن/خاموش، اندازه، رنگ، رنگ لبه، موج، «💾 ذخیره و اعمال»
  (همان لحظه روی مرورگر اعمال می‌شود) و «🖱 حرکت نشانگر برای دیدن».
- روش کار: تزریق یک `<style>` از راه CDP که `cursor` همهٔ عناصر را به یک PNG
  داده‌ای (دیتا-یوآرال) پیکان زرد تغییر می‌دهد + یک لایهٔ DOM برای موج. بعد از هر
  `goto_url` دوباره نصب می‌شود، چون ناوبرکی CSS تزریق‌شده را می‌برد.
- **مهم:** این یک اثر ظاهری است و هر خطایش **بلعیده می‌شود** — اگر CDP در دسترس
  نباشد، اجرا به همان شکل قبلی ادامه پیدا می‌کند.

> ⚠️ تأییدنشده: رندر نشانگر داخل Xvfb/noVNC هنوز **دیده نشده**. اگر بعد از
> Rebuild نشانگر عوض نشد، بگو تا مسیر Xcursor (تم نشانگر در سطح X) را امتحان کنیم.

### ۱۶.۶ تب صفحه‌ها — اول تصویر، بعد کلیک واقعی

- بعد از «🔍 شناسایی صفحه»، **اول** بلوک اسکرین‌شات می‌آید: خود تصویر، عنوان،
  URL، زمان، و **لینک مطلق عمومی** تصویر با دکمهٔ «📋 کپی لینک» (این لینک بدون
  توکن باز می‌شود، پس می‌توانی در تلگرام یا به یک مدل دیگر بدهی).
- این بلوک **بالای** فهرست صفحه‌های قبلی است، نه زیرش.
- هر عنصر دو دکمه دارد:
  - **کلیک واقعی** — همان لحظه نشانگر را روی آن مختصات می‌برد و کلیک می‌کند
    (`POST /control` با `action: click`). اگر اجرای دیگری مالک ماوس باشد ۴۰۹.
  - **کلیک** — مثل قبل فقط یک گام به «مراحل» اضافه می‌کند.

### ۱۶.۷ نوار اجرا = فوتر ثابت

در صفحهٔ اختصاصی پنل (`/automation/panel.html`) نوار پایین
(«قطع شد · 2m 4s · 11/36 · 📋 کپی گزارش اجرا · ▶ اجرا ⏸ ⏹») دیگر با اسکرول
نمی‌رود: `position: fixed` به کف صفحه چسبیده و بدنه `padding-bottom` گرفته تا
آخرین خط زیرش پنهان نشود. داخل overlay noVNC همان ستون ثابت قبلی است (چون خودِ
پنل `fixed` است، یک فوتر `fixed` دوم روی noVNC شناور می‌شد).

### ۱۶.۸ مسیرهای دستی: یک عمل، بدون ساختن جریان

`POST /control` حالا علاوه بر `pause/resume/stop/confirm` این‌ها را هم قبول می‌کند:

| action | بدنه | کار |
|---|---|---|
| `click` | `x`, `y`, `button?`, `clicks?` | یک کلیک واقعی |
| `double_click` | `x`, `y` | دوبار کلیک |
| `move` | `x`, `y` | بردن نشانگر بدون کلیک |
| `type` | `text` | تایپ (فارسی از راه clipboard) |
| `paste` | `text` | چسباندن با Ctrl+V |
| `key` | `keys: ["ctrl","a"]` | کلیدها |
| `goto_url` | `url` | باز کردن آدرس |
| `screenshot` | `name?` | اسکرین‌شات دستی |

قاعده‌ها: اگر اجرایی در جریان باشد **۴۰۹** (نشانگر صاحب دارد)، ورودی بد **۴۰۰**،
و اگر خود دستور روی دسکتاپ شکست بخورد **۵۰۲** با دنبالهٔ stderr.

### ۱۶.۹ چه چیزهایی در این نسخه تأییدنشده‌اند

همهٔ موارد زیر با تست خودکار (۴۲۳ پایتون + ۲۳ منطق + ۵۴ DOM) سنجیده شده‌اند،
اما **هرگز داخل کانتینر Hostim اجرا نشده‌اند**:

- چت با DeepSeek در مرورگر ایجنت (نیاز به یک بار لاگین دستی)،
- ارسال واقعی تلگرام (ربات و اکانت شخصی)،
- اجرای یک عملیات روی دسکتاپ واقعی،
- رندر نشانگر زرد و موج کلیک،
- کلیک واقعی از تب صفحه‌ها،
- کار کردن کلید ایجنت از بیرون (مثلاً ChatGPT).

ترتیب امتحان کردنشان در بخش ۷ همان [`CHECKLIST.fa.md`](CHECKLIST.fa.md) آمده.

---

## ۱۷. دور هفتم: فارسی‌سازی، زیبایی، دیتابیس، مرورگر و تلگرام انسان‌پسند

### ۱۷.۱ نام تب‌ها فارسی و مشکل «کش قدیمی»
نام همهٔ تب‌ها از جدول برچسب‌ها می‌آید (چت، کتابخانه، مراحل، …، **دیتابیس**، ایجنت همکار).
آن «tabchat» انگلیسی که دیدی حاصل ترکیبِ `automation.js` جدید با `core.mjs` قدیمیِ
مانده در کش مرورگر بود. حالا دو لایهٔ محافظ وجود دارد:
- هر import ماژولی داخل فایل کپی‌شده یک `?v=<hash>` می‌گیرد (`version_module_imports`)،
  پس مرورگر دیگر هرگز ترکیب قدیمی/جدید نمی‌سازد؛
- تست `LabelCoverageTest` در هر اجرا بررسی می‌کند که **هر** برچسب استفاده‌شده در
  `automation.js` هم در جدول فارسی باشد هم انگلیسی؛ برچسب جاافتاده = تست قرمز.
اگر بعد از Rebuild هنوز چیزی انگلیسی دیدی، یک‌بار **Ctrl+Shift+R** بزن؛ از آن به
بعد کش دیگر نمی‌تواند خراب کند.

### ۱۷.۲ نمای مرورگر همیشه تمام‌صفحه
سرور هنگام شروع، یک بلوک کوچک داخل `<head>` صفحهٔ `vnc.html` تزریق می‌کند که:
`noVNC_setting_resize = 'scale'` را **قبل از بالا آمدن خود noVNC** ست می‌کند و
اسکرول صفحه را می‌بندد (`overflow:hidden`). نتیجه: دسکتاپ همیشه کامل دیده می‌شود و
با اسکرول یا تغییر اندازهٔ پنجره چیزی گم نمی‌شود. این بلوک فقط روی مسیر Hostim/Colab
ِ همین سرور اعمال می‌شود و به خودِ آبجکت‌های noVNC دست نمی‌زند.

### ۱۷.۳ تلگرام انسان‌پسند + تست قابل‌اتکا
زیربخش «تلگرام» حالا با یک **کارت وضعیت** شروع می‌شود: مسیر ایجنت (ربات/حساب/هر دو)،
آمادگی هر مسیر، و شمارش‌ها درست مثل منوی نمونهٔ تو: «همه / در صف / رفته / ناموفق /
لغوشده». زیر آن:
- **تنظیمات** با توضیح‌های فارسیِ جمع‌شونده (📖): توکن ربات را از کجا بیاورم،
  ورود با حساب خودم یعنی چه، مقصد چیست و چطور پیدایش کنم، مسیردهی چه می‌کند؛
- **تست**: یک پیام واقعی به مقصد انتخابی می‌فرستد؛ دکمهٔ « تست کامل مرحله‌به‌مرحله»
  مسیر را قدم‌به‌قدم بررسی می‌کند (حالت → مسیرها → getMe ربات → نشست حساب → مقصد →
  ارسال واقعی) و هر قدم را با ✅/❌ و توضیح فارسی نشان می‌دهد (`POST /agent/telegram/diagnose`)؛
- **لیست پیام‌ها**: صندوق پستی هر تلاش ارسال با وضعیت، مقصد، متن و **زمان به ساعت تهران**
  (جدول `telegram_log` در دیتابیس؛ صف/رفته/ناموفق/جزئی)؛
- **برنامه‌ها**: پیام زمان‌بندی‌شده با عنوان، متن، تکرار (هر روز/هر ساعت/هر هفته)،
  «تا تاریخ» و «سقف دفعات»؛ وقتی سقف یا تاریخ تمام شود، برنامه خودش خاموش می‌شود
  (`payload.action = "telegram"` در زمان‌بند).

### ۱۷.۴ تب «دیتابیس»: پشتیبان‌گیری پیوسته
- «📦 گرفتن پشتیبان» یک `tar.gz` در `<data>/backups/` می‌سازد شامل: snapshot سازگارِ
  `agent.db` (با API رسمی backup خودِ SQLite، نه کپی خام)، همهٔ جریان‌های JSON و یک
  `backup.json` توضیحی. دکمهٔ قرمز «پشتیبان + secrets» فقط با تأیید صریح، فایل secretها
  را هم داخل آرشیو می‌گذارد (هشدارش را جدی بگیر).
- جدول پشتیبان‌ها: نام، اندازه، زمان (تهران)، و کارها: 📥 دانلود، 📤 فرستادن به تلگرام،
  🗑 حذف. دانلود از مرورگر با توکن انجام می‌شود و فایل gzip واقعی برمی‌گردد.
- «⏰ پشتیبان‌گیری خودکار» یک job زمان‌بندی‌شده می‌سازد (cron/every/at) که سر وقت
  پشتیبان می‌گیرد، به‌اندازهٔ «تعداد نگه‌داشته» نگه می‌دارد و اختیاری فایل را به مقصد
  تلگرامِ دلخواه تو آپلود می‌کند (`POST /agent/backup-config`، job با `action=backup`).
- کارت آمار: اندازهٔ دیتابیس، تعداد سطر هر جدول، شمار جریان‌ها و آخرین پشتیبان.

### ۱۷.۵ زیربخش «مرورگر»: چشم‌های ایجنت روی کروم
- تب‌های باز همین لحظه (عنوان + نشانی) با دکمه‌های «فعال کردن» و «بستن» و یک جعبهٔ
  «باز کردن تب» جدید؛
- نسخهٔ کروم و پروفایل فعال؛
- **سابقهٔ کامل** از فایل History خودِ کروم (کپی موقت، هرگز فایل زنده نه): بازدیدها با
  زمان و نحوهٔ ورود، جست‌وجوهای نوار 주소، و دانلودها؛ با فیلتر متن و بازهٔ زمانی؛
- «تب‌های بسته‌شدهٔ اخیر» = سابقهٔ ۲۴ ساعت منهای تب‌های باز. **صريح:** فایل باینری
  session کروم (SNSS) پارس نمی‌شود، پس این فهرست تقریب خوبی است نه عینِ «recently closed».
- مسیرها: `GET /agent/browser`، `/agent/browser/tabs`، `/agent/browser/history`،
  `POST /agent/browser/tab`.

### ۱۷.۶ دسکتاپ شبیه ویندوز ۱۰ (فقط Hostim)
- `hostim/fluxbox-style`: نوار پایین تیره با گرادیان آبی-فیروزه‌ای، ساعت و نام فضای
  کاری وسط، آیکن‌بار با هایلایت گرادیانی؛ پنجره‌ها با نوار عنوان باریک و گوشهٔ نرم؛
  منوها تیره با هایلایت فیروزه‌ای؛ و **پس‌زمینه گرادیان** بدون نیاز به هیچ فایل تصویر
  (در ایمیج feh/ImageMagick نیست).
- `hostim/fluxbox-menu`: راست‌کلیک روی دسکتاپ = منوی فارسی: پنل اتوماسیون، نمای مرورگر،
  پنجرهٔ جدید کروم، اسکرین‌شات، پنجره‌های باز، فضاهای کاری، بستن پنجرهٔ فعال، تنظیمات.
- `prepare_desktop` در entrypoint قبل از شروع fluxbox اجرا می‌شود: استایل/منو را کپی می‌کند،
  کلیدهای `~/.fluxbox/init` را می‌نویسد (نام فضاهای کاری فارسی، چیدمان نوار) و اگر متغیر
  محیطی `DESKTOP_BACKGROUND` به یک فایل تصویر روی volume اشاره کند، همان را پس‌زمینه
  می‌کند (`background.pixmap`).
- ⚠️ **تأییدنشده:** زیباییfluxbox از داخل سندباکس قابل دیدن نیست؛ بعد از Rebuild اگر
  چیزی بزشت بود بگو تا رنگ/ارتفاع را تنظیم کنم. هیچ‌کدام از این‌ها روی مسیر Colab اثر ندارد.

### ۱۷.۷ آنچه در این دور تأییدنشده ماند
- ظاهر دسکتاپ/نوار وظیفه در کانتینر واقعی (فقط تست فایل و wire شدن بررسی شده)؛
- ارسال واقعی فایل پشتیبان به تلگرام (نیازمند توکن واقعی تو)؛
- خواندن History کرومِ در حال اجرا داخل کانتینر (منطق با دیتابیس مصنوعی تست شده)؛
- رفتار noVNC با بلوک تمام‌صفحه در مرورگرهای مختلف (منطق تزریق تست شده).

## ۱۸. دور هشتم: دامنهٔ لخت، زیرتب‌ها، تم روشن و دسکتاپ فارسی

این دور دقیقاً هفت گزارشی که بعد از Rebuild فرستادی را جواب می‌دهد.

### ۱۸.۱ دامنهٔ لخت حالا خودِ برنامه را باز می‌کند
`https://af9833d9.eu-center.hostim.dev/` قبلاً ۴۰۴ می‌داد چون سرور فقط
مسیرهای فایل را می‌شناخت. حالا `GET /` و `HEAD /` با **302** به
`/vnc.html` می‌روند؛ یعنی همان آدرس را که در مرورگر تایپ می‌کنی، مستقیم
دسکتاپ + پنل را می‌بینی. مسیر سلامت (`/info`) و API دست‌نخورده‌اند.

### ۱۸.۲ فوتر دیگر روی محتوا نمی‌افتد
اشتباه دور قبل: فوتر `position: fixed` بود، پس آخرین ردیف‌های هر بخش
(از جمله «هفت») زیرش پنهان می‌شدند. حالا صفحهٔ پنل یک **ستون فلکس** است:
سربرگ ثابت، بدنه با اسکرول مستقل، و فوتر `position: sticky` در ردیف خودش.
نتیجه: فوتر همیشه پایینِ دید است و هیچ محتوایی هرگز زیرش نمی‌رود.

### ۱۸.۳ تم روشن و یک زبان بصری واحد
- کل پنل از تم تیره به **تم روشن** درآمد: پس‌زمینهٔ `#eef2f6`، کارت‌های
  سفید، خطوط روشن، رنگ تأکید **سبز-آبی (teal)**، وضعیت‌ها سبز/کهربایی/قرمز.
- فونت فارسی **وزیرمتن** از Google Fonts بارگذاری می‌شود (در `panel.html`
  و در بلوکِ تزریقی `vnc.html`)؛ اگر روزی آفلاین شد، بی‌صدا به فونت سیستم
  برمی‌گردد و چیزی نمی‌شکند.
- **زیرتب‌ها دقیقاً هم‌شکل تب‌های بالا هستند** (همان کلاس `mas-tab`)، در هر
  عمقی که لازم شود — مثل زیرتب‌های تلگرام داخل تب ایجنت.
- نوار تب‌ها حالا `flex-wrap: wrap` است؛ یعنی تب‌ها به‌جای قایم شدن در
  اسکرول افقی، در چند ردیف **همه دیده می‌شوند**. (دلیل اینکه «دیتابیس» را
  پیدا نمی‌کردی همین بود: تب وجود داشت ولی بیرونِ کادرِ اسکرول پنهان بود.)

### ۱۸.۴ تلگرام: زیرتب‌ها و «مقصدها»ی ویرایش‌پذیر
تب تلگرام در ایجنت حالا = کارت وضعیت (همیشه بالا، با selectِ حالت) و شش
زیرتب: **وضعیت و تست / تنظیمات / مقصدها / لیست پیام‌ها / برنامه‌ها /
نوشتن و فرستادن**. زیرتب «مقصدها» کاملاً قابل ویرایش است: شناسه، عنوان و
نوع هر مقصد را عوض کن، مقصد دستی اضافه کن، «🔎 کشف گفتگوها» را بزن و با
«＋» اضافه کن، بعد «💾 ذخیرهٔ مقصدها» تا در `telegram.targets` روی سرور
بنشیند.

### ۱۸.۵ پرامپت: اولین زیرتبِ ایجنت همکار
«کپی پرامپت / 🧩 / وارد کردن JSON» از جعبه‌ابزار چت بیرون آمد و شد
**زیرتب «پرامپت» — اولین زیرتب ایجنت**. جعبه‌ابزار تب چت فقط یک دکمه دارد:
«رفتن به زیرتب پرامپت». دیگر دو خانهٔ جدا برای پرامپت وجود ندارد.

### ۱۸.۶ تب دیتابیس
همان تبِ دور هفتم است (پشتیبان دستی + خودکار + ارسال به تلگرام) و با
`flex-wrap` بالاخره دیده می‌شود. هیچ چیز دیگری تغییر نکرد.

### ۱۸.۷ دسکتاپ مجازی و فارسی
- **فونت منو/نوار fluxbox**: استایل با جای‌نگهدار `MasFont` بسته‌بندی می‌شود
  و `prepare_desktop` موقع استارت، با `fc-list ':lang=fa'` اولین خانوادهٔ
  دارای گلیف فارسی را جایش می‌گذارد (پیش‌فرض احتیاطی: DejaVu Sans). پس
  منوی راست‌کلیک و نوار وظیفه دیگر مربع‌مربع نمی‌شوند.
- **کیبورد**: کلید `PERSIAN_KEYBOARD=off` را که بگذاری، دسکتاپ فقط `us`
  می‌شود (همان «انگلیسی کنیم تا راحت باشیم»). پیش‌فرض `on` است: `us,fa`
  با **Alt+Shift** برای جابه‌جایی. `setxkbmap` حالا روی نمایشگر دوم
  (`AI_DISPLAY=:2` — کرومِ ایجنت) هم اجرا می‌شود.
- اگر فارسیِ داخلِ دسکتاپ هنوز اذیت کرد، کافی است در تنظیمات App روی
  Hostim یک متغیر محیطی بگذاری: `PERSIAN_KEYBOARD=off`.

### ۱۸.۸ تست‌های تازه
- `test_server.py`: ریشه ۳۰۲ به `/vnc.html` (GET و HEAD) و اینکه دنبال
  کردنِ bounce به فهرست دایرکتوری نمی‌رسد.
- `test_hostim_deploy.py`: کلید `PERSIAN_KEYBOARD`، کیبورد روی `AI_DISPLAY`،
  و تعویض `MasFont` با فونتِ دارای پوشش fa.
- `test_agent.py`: لینک فونت در بلوکِ تزریقی `vnc.html` + عدم تکرار در
  اجرای دوم.
- `test_ui.mjs`: پرامپت اولین زیرتب، اشاره‌گر تب چت، زیرتب‌های تو در توی
  تلگرام (مقصدهای ویرایش‌پذیر/تنظیمات/نوشتن)، فوتر sticky و اسکرول مستقل
  بدنه، تم روشن (`color-scheme: light`).

## ۱۹. دور نهم: دسکتاپِ درست، رمز واحد، تم دارک/لایت، صفحهٔ اختصاصی و پیشنهادات ایجنت

### ۱۹.۱ چرا لینک باز می‌شد ولی دسکتاپ نمی‌آمد (و حل شد)
دو علت داشت: ① ریدایرکت `302` روی `/` با health check هاستیم که **HTTP 200**
می‌خواهد دعوا می‌کرد و اپ ناسالم علامت می‌خورد؛ ② لینک فونت Google در
`<head>` رندر را **بلاک** می‌کرد و اگر شبکه به گوگل نمی‌رسید، صفحه سفید
می‌ماند. حالا `/` یک صفحهٔ ۲۰۰ کوچکی است که فوراً به `/vnc.html` می‌پرد
(meta-refresh + JS) و فونت **وزیرمتن خودمیزبان** است (woff2 کنار خودِ CSS،
بدون هیچ درخواست خارجی — حتی کاملاً آفلاین هم کار می‌کند).

### ۱۹.۲ یک رمز برای همهٔ درها، با هر طولی
اسطورهٔ «رمز باید هشت کاراکتر باشد» هیچ‌جا در کد وجود نداشت ولی از این
پس صریحاً هم نیست: هر رشته‌ای در `AUTOMATION_TOKEN` بگذاری (کوتاه، بلند،
فارسی، با علامت) همان **رمز ورود** است: درِ noVNC، درِ پنل و هدر API همه
با همان یک رمز باز می‌شوند (`VNC_PASSWORD` به‌صورت پیش‌فرض = خودِ
`AUTOMATION_TOKEN`؛ اگر صراحتاً `VNC_PASSWORD` بگذاری فقط درِ VNC با آن
باز می‌شود). توجه: پروتکل VNC فقط ۸ بایت اول رمز را carries — کلاینت و
سرور هر دو یک‌جا truncate می‌کنند، پس رمز بلند هم کار می‌کند.

### ۱۹.۳ دسکتاپ: تسک‌بار آیکونی، ساعت با تاریخ و منطقه، رفع تداخل پنجره‌ها
- **تسک‌بار**: فقط آیکون هر برنامه (بدون متن) — `iconbar.usePixmap=true`،
  `iconWidth=36` و رنگِ متن هم‌رنگِ پس‌زمینهٔ دکمه، پس عنوان دیده نمی‌شود.
  کلیک = فوکوس/بالا آوردن (مثل ویندوز).
- **ساعت**: `%H:%M  %Y/%m/%d  (%Z)` — وقت، تاریخ و نام منطقهٔ زمانی.
  منطقه از `DESKTOP_TZ` می‌آید (پیش‌فرض `Asia/Tehran`).
- **تداخل «Open File»**: culprit اصلی `autoRaise:true` بود — هر پنجره‌ای که
  ماوس رویش می‌رفت بی‌درخواست می‌پرید بالا. حالا `ClickToFocus` +
  `autoRaise:false` (رفتار ویندوزی: کلیک = فوکوس + بالا آمدن، همان یکی).
  اگر باز هم یک محاورهٔ «Open File» لجباز ماند: راست‌کلیک روی دسکتاپ →
  «🧹 جمع‌کردن پنجرهٔ مزاحم Open File» همهٔ محاوره‌های فایل را می‌بندد.

### ۱۹.۴ کلیپ‌بورد دوطرفه (کامپیوتر تو ↔ دسکتاپ)
noVNC فقط کلیدها را می‌برد، نه کلیپ‌بورد را. راه‌حل: **تب تنظیمات → پل
کلیپ‌بورد**. متن را بگذار، «📤 بفرست به دسکتاپ» → داخل دسکتاپ `Ctrl+V`؛
یا «📥 از دسکتاپ بخوان» → متنِ کپی‌شده در دسکتاپ را بگیر. دو دکمهٔ «محلی»
هم با اجازهٔ مرورگر مستقیم با کلیپ‌بورد کامپیوتر خودت کار می‌کنند
(نیاز به HTTPS و مجوز clipboard — روی دامنهٔ hostim.dev هر دو هست).
API: `GET/POST /automation/api/clipboard` (پشت همان رمز ورود؛ server با
`xclip` روی `DISPLAY=:1` جابه‌جا می‌کند).

### ۱۹.۵ تم دارک/لایت — در هدر، با حافظهٔ دیتابیسی
دکمهٔ 🌙/☀️ هم در هدر صفحهٔ پنل است هم در نوار بالای پنلِ داخل noVNC.
انتخاب در `localStorage` **و** در دیتابیس (کلید `ui.theme` از طریق
`POST /agent/settings`) ذخیره می‌شود؛ موقع بالا آمدن، مقدارِ دیتابیس
اعمال می‌شود، پس از هر مرورگر/کامپیوتری که بیایی همان تمِ قبلی هست. کل
CSS با توکن‌های معنایی (`--bg-solid`, `--text`, `--accent`, …) بازنویسی شد
و تم تاریک فقط همان توکن‌ها را بازنویسی می‌کند — طبق همان الگویی که
خودت فرستادی (کارت ۱۶px، تب کپسولی، زیرتب سگمنتی با نشانگر تعداد، چیپ
نرم، جدول در wrapper گرد، ستون ۱۰۸۰px وسط‌چین، breakpointهای
۹۶۰/۹۰۰/۸۲۰/۶۲۰، حرکت ظریف `fadeUp` که با `prefers-reduced-motion` خاموش
می‌شود، فونت ۱۵px/۱٫۷۵ در صفحهٔ مستقل).

### ۱۹.۶ تب «تنظیمات»
تب جدید در نوار اصلی: تم، پل کلیپ‌بورد، **لاگ پیشرفت توسعه** و اطلاعات
سیستم (نسخه، viewport، زمان سرور به تهران) + میان‌برها به تنظیمات تلگرام،
پشتیبان‌گیری و کلید ایجنت.

### ۱۹.۷ لاگ پیشرفت توسعه + نامه به ایجنت همکار
جدول `devlog` در دیتابیس ساخته شد (`GET/POST /agent/devlog`). اولین
یادداشت، **نامهٔ خودِ ایجنت** است: خلاصهٔ دورهای ۱ تا ۹ و قواعد بازی
(خواندن آزاد، تغییر با تأیید انسان، کپچا همیشه انسانی، کلیدها هرگز در
چت/URL). هر پیشرفت بعدی هم همین‌جا ثبت می‌شود تا ایجنت همیشه بداند کجای
کاریم. در تب تنظیمات قابل خواندن و افزودن است.

### ۱۹.۸ زیرتب «صفحه اختصاصی» (ایجنت همکار)
ایجنت (یا خودت) هر HTML می‌سازد: **ایجنت → صفحه اختصاصی**. ویرایشگر +
پیش‌نمایش زنده در iframe جدا (sandbox: اسکریپت اجرا می‌شود ولی به پنل و
کوکی‌هایش دسترسی ندارد) + فهرست صفحه‌های ذخیره‌شده. دکمهٔ «📥 برداشتن
HTML از پاسخ ایجنت» بلوک ```html را از پاسخ چت بیرون می‌کشد. همه در جدول
`agent_pages` **در دیتابیس می‌ماند** (`GET/POST /agent/pages`، حذف فقط با
رمز اپراتور).

### ۱۹.۹ زیرتب «پیشنهادات ایجنت» + پنجرهٔ فقط‌خواندنی دیتابیس
طبق نامهٔ ایجنت همکار، کامل پیاده شد:
- جدول `suggestions` با همهٔ فیلدهای خواسته‌شده (عنوان، مشکل، بخش هدف، نوع
  از ۸ دسته، شواهد، پیشنهاد، before/after برای diff، دلیل، ریسک، اثر،
  needs_human، وضعیت از draft تا applied/failed، نتیجهٔ اعمال، یادداشت
  rollback).
- API: `GET/POST /agent/suggestions` + `suggestion-update/decision/apply/
  rollback/delete` (حذف = **بایگانی نرم**، هرگز حذف واقعی).
- **دروازهٔ انسانی**: ایجنت فقط draft/pending ثبت می‌کند (خودتأییدی
  نادیده گرفته می‌شود)؛ decision/apply/rollback/delete با کلید ایجنت
  **۴۰۳** است؛ apply بدون «تأیید» قبلی **۴۰۹**؛ نوع `settings` واقعاً
  اعمال و rollback می‌شود، بقیهٔ انواع صادقانه «اعمال خودکار امن ندارد»
  می‌گیرند. هر قدم در audit ثبت می‌شود.
- `GET /agent/db/schema` و `POST /agent/db/query`: اتصال `mode=ro` (حتی
  اگر از چک‌های متنی چیزی رد شود، SQLite نوشتن را رد می‌کند)، فقط
  SELECT/WITH/PRAGMA/EXPLAIN، تک‌دستور، بدون ATTACH، سقف ۵۰۰ ردیف.
  **ماسک‌سازی**: ستون‌های حساس (token/secret/password/hash/cookie/session/
  otp/card/…) و در جدول settings هر ردیفی که کلیدش بوی اعتبارنامه بدهد یا
  flag secret داشته باشد → `{masked, configured, length, last4}`. UI آن در
  زیرتب «داده‌ها» است.
- **connector**: هر درخواست با کلید ایجنت یک ردیف audit می‌گیرد
  (`actor=agent-key`, endpoint، هرگز خودِ کلید) + «آخرین استفاده» در زیرتب
  کلید نمایش داده می‌شود و دکمهٔ «🔌 تست اتصال با کلید» همان چیزی است که
  نامه خواسته بود (GET /agent با هدر `x-agent-key`، بدون `?k=`).
  revoke/rotate/kill-switch همان‌های قبلی‌اند و فعال.

### ۱۹.۱۰ تست‌های تازه
۴۵۸ تست پایتون OK (چرخهٔ کامل پیشنهادات با دروازه‌ها، ماسک‌سازی ردیفی
settings، رد کوئری‌های نوشتاری/چنددستوری/ATTACH، صفحات و devlog، رمز با
هر طول، ساعت و فوکوس و آیکون‌های دسکتاپ، ریشهٔ ۲۰۰+meta-refresh، فونت
خودمیزبان) · ۲۳ تست core · ۵۹ تست UI (تمِ دیتابیسی، تب تنظیمات، پل
کلیپ‌بورد، صفحهٔ اختصاصی، دروازهٔ confirm در اعمال پیشنهاد، پنجرهٔ
فقط‌خواندنی با ماسک).

## ۲۰. دور دهم: هدر یک‌خطی، فوتر شناور، زیرتب‌های دیتابیس و فونت فارسی دسکتاپ

بازخورد کاربر با دو عکس: (۱) آدرس لخت باید دقیقاً همان نمای
`vnc.html?autoconnect=true&resize=scale&path=websockify` را بیاورد، (۲) فوتر
تمام‌عرض، یک خط و چسبیده به کف، (۳) هدر یک خط شامل ردیف تب‌ها، (۴) تم و زبان
به تب تنظیمات بروند، (۵) اسکرین‌شات‌ها و متون استخراج‌شده زیرتب دیتابیس شوند،
(۶) جملهٔ زیر عنوان حذف شود، (۷) دکمهٔ «نمای دسکتاپ» در تب جدید باز شود،
(۸) مربع‌های توخالی (tofu) در تسک‌بار/منوی دسکتاپ فارسی درست شود.

| # | تغییر | فایل(ها) | نکتهٔ فنی |
|---|---|---|---|
| ۱ | ریدایرکت آدرس لخت با پارامتر کامل | `automation/server.py` | ROOT_PAGE همچنان ۲۰۰ می‌دهد (health check) ولی مقصد meta-refresh/`location.replace` حالا `?autoconnect=true&resize=scale&path=websockify` دارد؛ هرگز به `/vnc.html` خالی برنگردانید |
| ۲ | هدر یک‌خطی چسبان | `panel.html` + `automation.js` + `automation.css` | `.mas-page-head` خالی ship می‌شود و JS عنوان، ردیف تب‌ها، نقطهٔ وضعیت و دکمهٔ دسکتاپ را در آن می‌گذارد؛ `#mas-root .mas-head` در حالت standalone مخفی است؛ جملهٔ «کنترل همان مرورگر…» حذف شد |
| ۳ | فوتر شناور تمام‌عرض | `automation.css` | `.mas-foot` در standalone شده `position:fixed` یک ردیف `nowrap` با `min-height:44px`؛ پنل گام‌ها به‌جای باز کردن فوتر، کشویی (`fixed bottom:46px`) روی آن باز می‌شود؛ `.mas-body` پدینگ کف ۶۰px گرفت |
| ۴ | زیرتب‌های دیتابیس | `automation.js` | تب «دیتابیس» سه زیرتب دارد: پشتیبان‌گیری / اسکرین‌شات‌ها / متون استخراج‌شده (`state.dbSub` + `[data-dbsub]`)؛ `renderShots/renderTexts` پارامتر host گرفتند و تب‌های مستقل shots/texts از TAB_ORDER حذف شدند |
| ۵ | تم و زبان در تنظیمات | `automation.js` + `core.mjs` | انتخاب FA/EN و دکمهٔ تم از هدر حذف و به کارت‌های تب تنظیمات منتقل شدند (`settingsLangTitle/Hint`) |
| ۶ | دکمهٔ «نمای دسکتاپ» | `automation.js` | در هدر (انتهای چپ در RTL) با `target=_blank` و href `../vnc.html?autoconnect=true&resize=scale&path=websockify`؛ لینک داخل noVNC و تب ضبط هم همین پارامترها را گرفتند |
| ۷ | حذف جعبه‌ابزار چت | `automation.js` | کارت «🧩 ابزارهای دستیار» و دکمهٔ اشاره‌گر از تب چت حذف شد؛ خانهٔ یکتای پرامپت همان زیرتب «ایجنت همکار → پرامپت» است |
| ۸ | فونت فارسی دسکتاپ | `automation/static/fonts-ttf/` + `hostim/Dockerfile` + entrypoint | Vazirmatn TTF (Regular/Bold + لایسنس OFL) مستقیم در image کپی و `fc-cache` قبل از حلقهٔ کاندیدای apt اجرا می‌شود؛ entrypoint اول family دقیق `Vazirmatn` را ترجیح می‌دهد؛ اگر نبود `DESKTOP_LANG=en` منوی انگلیسی (`hostim/fluxbox-menu-en`) و نام Workspace انگلیسی می‌دهد |

### نکته‌های بازگشت‌ناپذیر (رگرسیون ممنوع)
- ریدایرکت لخت **بدون پارامتر** = کارت رمز noVNC ظاهر نمی‌شود.
- mirror ریشه باید دقیقاً hostim/Dockerfile باشد با note بعد از خط `# syntax`؛
  تست `TestRootMirrorOfHostimDockerfile` drift را می‌گیرد.
- فهرست COPYهای mirror حالا `automation/static/fonts-ttf/` هم دارد (تست به‌روز شد).

# بخش ب — مسیر Colab (شاخهٔ پایدار `colab-stable`)

این بخش **نسخهٔ پایدار و مستقل Colab** است. استقرار Hostim هیچ تغییری در آن
نداده: همان `install.sh`، همان `start_colab_browser.sh`، همان پورت‌های
`5901`/`6080`/`9222`، همان مسیر noVNC/VNC و همان Cloudflare Quick Tunnel.

شاخهٔ پایدار: **`colab-stable`**. هر سلول زیر را در یک سلول جداگانهٔ Colab
اجرا کنید.

> ⚠️ یک ایراد مستنداتی که پیدا کردم و **اصلاحش نکردم** (چون در `colab-stable`
> است و من اجازهٔ تغییرش را ندارم): سلول ۱ در `README.md` به شاخهٔ قدیمی
> `arena/a41706bf-...` اشاره می‌کند. راهنمای درست، همین بخش ب با شاخهٔ
> `colab-stable` است.

### سلول ۱ — clone شاخهٔ پایدار

```python
%cd /content
!git clone --single-branch --branch colab-stable https://github.com/majidshilehsari/MajidAutomationSystem-colab_browser-use.git
%cd /content/MajidAutomationSystem-colab_browser-use
!git status --short --branch
```

**خروجی مورد انتظار:** `git status` شاخهٔ `colab-stable` را نشان دهد و هیچ
فایل تغییریافته‌ای نباشد:

```text
## colab-stable...origin/colab-stable
```

**verification:** اگر مخزن از قبل در runtime وجود دارد، clone نکنید؛ از داخل
پوشهٔ آن `!git checkout colab-stable` را بزنید. با
`!git rev-parse --abbrev-ref HEAD` باید `colab-stable` چاپ شود.

### سلول ۲ — نصب وابستگی‌های میزکار و مرورگر

```python
%cd /content/MajidAutomationSystem-colab_browser-use
!chmod +x ./*.sh
!./install.sh
```

**خروجی مورد انتظار:** در خط‌های پایانی `Installed Chrome:`،
`Installed cloudflared:` و `Computer-use dependencies are ready.` دیده شود.
نصب چند دقیقه طول می‌کشد. خط `websockify module: ok` هم مهم است — بدون آن
sidebar از کار می‌افتد و اسکریپت به‌صورت خودکار به حالت noVNC ساده
برمی‌گردد.

**verification:**

```python
!google-chrome --version && cloudflared --version
!python3 -c 'import websockify.websocketproxy; print("websockify ok")'
```

### سلول ۳ — راه‌اندازی پشتهٔ مرورگر سمت سرور

```python
%cd /content/MajidAutomationSystem-colab_browser-use
!./start_colab_browser.sh --wait
```

**این سلول عمداً در حال اجرا می‌ماند.** خروجی مورد انتظار شامل این نام‌هاست:

```text
BROWSER_URL=https://<رشته>.trycloudflare.com/vnc.html?autoconnect=true&resize=scale&path=websockify
VNC_PASSWORD=<رمز همین اجرا>
AUTOMATION_TOKEN=<توکن همین اجرا>
STATE_DIR=/content/MajidAutomationSystem-colab_browser-use/.runtime
Supervisor is running; keep this terminal session open.
```

این مقدارها **secret مخصوص همین اجرا** هستند: هرگز در چت، در issue یا در
commit نگذارید. `BROWSER_URL` را در یک تب یا دستگاه دیگر باز کنید و رمز VNC
را فقط در پنجرهٔ رمز noVNC وارد کنید. تا پایان کارهای سمت سرور، این runtime
و این سلول را زنده نگه دارید.

**verification:** اگر سلول تمام شد (به‌جای اینکه در حال اجرا بماند)، یعنی
supervisor خطا داده؛ `.runtime/logs/` را ببینید. خط
`Port 5901 or 6080 is already occupied` یعنی یک پشتهٔ دیگر از قبل در حال
اجراست: `!./stop_colab_browser.sh` و دوباره سلول ۳.

> مسیر Hostim **در این سلول اجرا نمی‌شود** و نباید بشود: `cloudflared` فقط
> مخصوص مسیر Colab است.

### سلول ۴ — اتصال sidebar و تأیید شناسایی صفحه

۱. در صفحهٔ noVNC، sidebar سمت راست و تب AI را باز کنید.
۲. `AUTOMATION_TOKEN` همین اجرا را در فیلد توکن sidebar وارد کنید — **نه** در
   یک گفت‌وگوی AI.
۳. **مورد انتظار:** نشانگر اتصال → **Connected**.
۴. **Pages → Detect page** → **مورد انتظار:** یک رکورد صفحهٔ ذخیره‌شده به‌همراه
   اسکرین‌شات. ارزیابی `challenge` هم در snapshot هست؛ هشدار فقط وقتی نشان
   داده می‌شود که تشخیص‌دهندهٔ best-effort نشانه‌های چالش پیدا کند.

**verification از راه API (بدون مرورگر):** توکن را از خروجی سلول ۳ در یک
متغیر محیطی همان سلول بگذارید و **هرگز چاپش نکنید**:

```python
import os, urllib.request, json
os.environ['TOKEN'] = ''   # مقدار AUTOMATION_TOKEN سلول ۳ را اینجا paste کنید

req = urllib.request.Request('http://127.0.0.1:6080/automation/api/status',
                             headers={'X-Automation-Token': os.environ['TOKEN']})
print(json.load(urllib.request.urlopen(req, timeout=10)))          # → status: idle

req2 = urllib.request.Request('http://127.0.0.1:6080/automation/api/status')
try:
    urllib.request.urlopen(req2, timeout=10)
except urllib.error.HTTPError as exc:
    print('بدون توکن →', exc.code)                                  # → 401
```

**خروجی مورد انتظار:** `{'runId': None, 'status': 'idle', ...}` و سپس
`بدون توکن → 401`. یعنی سرور روی `127.0.0.1:6080` در همان runtime در دسترس
است و احراز هویت درست کار می‌کند.

### سلول ۵ — تأیید واگذاری به انسان، بدون مراجعه به CAPTCHA

برای یک تست امن و محلی، در تب Flow یک گام `pause_for_human_verification` با یک
prompt کوتاه اضافه کنید و بعد از آن یک گام بی‌خطر `wait`. اجرا کنید.

**خروجی مورد انتظار:** وضعیت به **Waiting** تغییر کند، یک جعبهٔ کوچک واگذاری
پدیدار شود **بدون** اینکه بوم noVNC غیرفعال شود، و گام بعدی هنوز اجرا نشده
باشد. برای این تست روی **Continue after human review** کلیک کنید؛
**مورد انتظار:** گام `wait` اجرا شود و وضعیت **Done** شود.

برای این verification نیازی به فعال‌کردن یک CAPTCHA واقعی نیست.

### سلول اختیاری — اجرای کامل تست‌ها

runtime مرورگر به Node نیازی ندارد. اگر Node.js و npm در دسترس بودند و
خواستید همهٔ بررسی‌های مخزن را اجرا کنید:

```python
%cd /content/MajidAutomationSystem-colab_browser-use
!npm ci
!./run_tests.sh
```

**خروجی مورد انتظار:** خط پایانی `ALL CHECKS PASSED`. اسکریپت نتیجهٔ جداگانهٔ
تست‌های پایتون، جاوااسکریپت، shell و بررسی نحو را چاپ می‌کند. اگر Node/npm
نیست، همین بررسی‌ها را در یک محیط توسعه اجرا کنید؛ به‌خاطر تست، ابزار Node
بی‌ربط نصب نکنید.

> روی شاخهٔ `hostim-deploy` این اسکریپت `tests/test_hostim_deploy.py` را هم
> اجرا می‌کند (۴۷ تست قراردادی مسیر Hostim). روی شاخهٔ `colab-stable` آن فایل
> وجود ندارد و تعداد تست‌ها همان ۱۵۵ تست پایتون قبلی است — یعنی **رفتار تست
> مسیر Colab هم تغییر نکرده است**.

### توقف محیط Colab

`Ctrl+C` در همان سلول ۳، یا:

```python
%cd /content/MajidAutomationSystem-colab_browser-use
!./stop_colab_browser.sh
```

**خروجی مورد انتظار:** `Colab browser services stopped.` — این اسکریپت فقط
فرایندهایی را می‌بندد که در `.runtime/` همین پروژه ثبت شده‌اند.

---

## جمع‌بندی تفاوت دو مسیر

| | Colab (شاخهٔ `colab-stable`) | Hostim (شاخهٔ `hostim-deploy`) |
|---|---|---|
| نقطهٔ ورود | `install.sh` سپس `start_colab_browser.sh --wait` | `hostim/Dockerfile` + `hostim/docker-entrypoint.sh` |
| دسترسی عمومی | Cloudflare Quick Tunnel (موقتی، بدون تضمین uptime) | دامنهٔ HTTPS پایدار از ingress خودِ Hostim |
| دادهٔ ماندگار | `.runtime/` — با حذف runtime از بین می‌رود | volume روی `/data` + بکاپ روزانه |
| secretها | در خروجی سلول چاپ می‌شوند | در `/data/secrets.env` با mode 0600، بدون چاپ در لاگ |
| عمر اجرا | تا وقتی سلول و runtime زنده‌اند | ۲۴/۷، با restart خودکار پس از crash |
| تغییر در رفتار دیگری | — | هیچ: فایل‌های Colab دست‌نخورده‌اند |

این دو نسخه عمداً **merge، rebase یا cherry-pick نمی‌شوند**. مسیر Hostim یک
افزودنی مستقل است که فقط روی شاخهٔ `hostim-deploy` زندگی می‌کند.
