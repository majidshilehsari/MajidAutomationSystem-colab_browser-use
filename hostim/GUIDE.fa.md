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
- [بخش ب — مسیر Colab (شاخهٔ پایدار `colab-stable`)](#بخش-ب--مسیر-colab-شاخهٔ-پایدار-colab-stable)

---

# بخش الف — استقرار روی Hostim

## ۱. نتیجهٔ امکان‌سنجی: چه چیزی تأیید شده و چه چیزی تأییدنشده است

منبع: مستندات عمومی [hostim.dev/docs](https://hostim.dev/docs/intro/)،
[CLI manual](https://hostim.dev/cli-manual.md)،
[changelog](https://hostim.dev/docs/changelog/) و
[pricing](https://hostim.dev/docs/billing/pricing-model/) — بدون ورود به حساب
کاربری.

| # | موضوع | وضعیت | نکتهٔ عملی |
|---|---|---|---|
| ۱ | Dockerfile / image سفارشی | ✅ تأییدشده | build از Git با مسیر دلخواه Dockerfile؛ image باید `linux/amd64` و زیر ۴ گیگابایت باشد |
| ۲ | چند فرایند ماندگار در یک کانتینر | ⚠️ **تأییدنشده** | مستندات نه منع کرده نه تأیید؛ Command Override الگوی `sh -c "... && exec ..."` را پیشنهاد می‌دهد یعنی PID 1 می‌تواند سوپروایزر باشد |
| ۳ | یک پورت HTTP عمومی برای noVNC/API | ✅ تأییدشده | هر app دقیقاً یک `httpPort` دارد؛ دامنهٔ `*.hostim.dev` با HTTPS خودکار |
| ۴ | WebSocket از مسیر HTTPS/proxy | ⚠️ **تأییدنشده** | ingress پلتفرم Traefik v3 است که WebSocket را بومی پشتیبانی می‌کند، ولی Hostim هیچ چیزی دربارهٔ WebSocket یا idle timeout ننوشته |
| ۵ | volume پایدار | ✅ تأییدشده | mount روی `/data`؛ پایدار بین deploy؛ بکاپ روزانه با ۷ روز نگه‌داری |
| ۶ | منابع / root / seccomp / privileged | 🔶 پلن‌ها تأییدشده، بقیه **تأییدنشده** | سقف ۳ vCPU / ۸GB؛ هیچ فلگی برای privileged وجود ندارد؛ هر app داخل یک microVM از Kata Containers است |

**جمع‌بندی:** استقرار از نظر فنی **ممکن به‌نظر می‌رسد** و طراحی فعلی پروژه
(همه‌چیز روی یک پورت، VNC و CDP فقط روی loopback، بدون نیاز به privileged)
با محدودیت‌های Hostim خوب جور درمی‌آید. اما سه ریسک **تأییدنشده** باقی است که
فقط با build واقعی image و یک استقرار آزمایشی روی Hostim قابل تأیید هستند:

1. اجرای واقعی Chrome روی Xvfb داخل یک microVM از Kata (اندازهٔ `/dev/shm`،
   seccomp و capabilities مستند نشده‌اند).
2. عبور WebSocket بلندمدت noVNC از ingress بدون timeout.
3. کاربری که کانتینر با آن اجرا می‌شود (این image عمداً non-root ساخته شده تا
   در هر دو حالت کار کند).

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

---

## ۱۳. چه چیزهایی تست نشده‌اند (صریحاً)

**تست‌شده در محیط من:**

- ✅ کل تست‌های مخزن: `./run_tests.sh` → `ALL CHECKS PASSED`
  (۲۱۲ تست پایتون، ۲۰ تست `test_core.mjs`، ۳۰ تست jsdom، بررسی نحو همهٔ
  اسکریپت‌ها) — جزئیات دقیق در گزارش نهایی.
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

- ⚠️ **build هنوز تا انتها نرسیده.** در تلاش دوم، مرحلهٔ ساخت کاربر غیرroot
  با `groupadd: GID '1000' already exists` (کد خروج ۴) شکست خورد، چون image
  پایهٔ ubuntu:24.04 یک حساب پیش‌فرض روی uid/gid 1000 دارد. **رفع شد** (بخش
  «mirror ریشه» و تست `TestRuntimeUserIsRobust`): حالا شناسه اول با `getent`
  پرس‌وجو می‌شود، حساب stock به‌صورت عمومی (بدون فرض نام `ubuntu`) حذف
  می‌شود و بعد `automation` روی همان ۱۰۰۰ ساخته و با `id automation`
  راستی‌آزمایی می‌شود.
- ❌ **اندازهٔ نهایی image تأیید نشد** (باید زیر ۴ گیگابایت باشد). لایه‌های
  apt و کروم موفق بودند، ولی جمع نهایی هنوز اندازه‌گیری نشده.
- ❌ **هیچ کانتینری تا به حال روی Hostim اجرا نشده** — پس entrypoint، Xvfb،
  fluxbox، x11vnc و کرومِ **واقعی** در آن محیط آزموده نشده‌اند. رفتار
  entrypoint فقط با سرویس‌های stub در محیط من آزموده شده (بالا).

- ❌ **هنوز هیچ image ای از این مخزن با موفقیت ساخته نشده.** build اول با
  `open Dockerfile: no such file or directory` و build دوم با
  `groupadd: GID '1000' already exists` شکست خورد؛ هر دو رفع شدند، ولی build
  سومی که تا انتها برود هنوز انجام نشده. وضعیت اپ فعلاً
  `Never built / Never deployed` است.
- ❌ در محیط کاری خودم نه `docker` هست و نه `podman` و دسترسی شبکه به Ubuntu
  archives و Docker Hub بسته است، پس هیچ‌وقت نتوانستم build را محلی جایگزین
  کنم. همهٔ موارد «تأییدشده توسط build واقعی» از لاگی است که شما از داشبورد
  Hostim فرستادید. من وارد حساب شما نشدم و credential ندارم.
- ❌ Chrome و Xvfb **واقعی** اجرا نشدند (فقط stub). رندر swiftshader، رفتار
  `--no-sandbox` داخل Kata، و اندازهٔ `/dev/shm` تأیید نشده‌اند.
- ❌ عبور WebSocket از Traefik واقعی Hostim و هرگونه idle timeout تأیید نشد.
- ❌ کارایی پروفایل کروم روی volume تأیید نشد.
- ❌ کفایت منابع پلن‌ها (اینکه `sa-2-2` واقعاً کافی است) تأیید نشد.
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
