# ترقية مركز جمع الأرشيف السوري إلى V4

> **مراجعة المثبّت `r1`:** لا تغيّر محرك الجمع `4.0.0`. تعالج سباق إقلاع
> Celery أثناء التفعيل: تنتظر web والعامل وCaddy بمهل صريحة، توجّه ping إلى
> العامل الجديد نفسه، وتحتفظ بسجلات الفشل داخل النسخة الاحتياطية قبل الرجوع.

## ما الذي تعالجه V4؟

تعالج V4 انهيار السرعة الذي كان يظهر بعد بداية سريعة في V3. كان طلب X ينتظر قفل بداية واحدًا بينما يمسك تصاريح المضيف والعامل العام، ثم كان التأخير يتضاعف بعد نتائج فردية مثل 403 حتى ثماني ثوانٍ. النتيجة كانت طابورًا متزايدًا رغم أن زمن الشبكة نفسه يقارب أجزاء من الثانية.

تضيف V4:

- حجز معدل الطلب قبل أخذ أي تصريح شبكة؛ لا توجد تصاريح نائمة.
- توزيعًا عادلًا بين Facebook وX والمواقع العامة.
- حدود معدل مستقلة لكل مضيف مع احترام 429 و`Retry-After`.
- حدًا حقيقيًا لمدة الطلب كاملة، بما فيها التحويلات وجسم الاستجابة.
- طابور حفظ منفصلًا عن حلقة الشبكة.
- قياسات منفصلة لانتظار المجدول، التمهيد، الشبكة، الإعادة، والحفظ.
- إلغاءً آمنًا سريع الاستجابة من دون تسريب تصاريح.

لا تدّعي V4 أن كل رابط خارجي قابل للاستخراج. الرابط المحذوف أو الخاص أو المحجوب يبقى حالة موثقة قابلة للاسترداد، ولا يُعرض كنجاح زائف ولا يوقف المهمة.

## أوضاع المثبّت

| الأمر | السلوك |
|---|---|
| بلا خيار | يجهّز الحزمة ثم يفعّلها فورًا فقط إذا لم توجد مهمة نشطة أو متوقفة |
| `--stage-only` | يتحقق من الحزمة ويبني صورة معزولة؛ لا يغير أي ملف production ولا يوقف أي مهمة أو حاوية |
| `--activate-when-idle` | يجهّز الآن، ويراقب PostgreSQL، ثم يفعّل بعد انتهاء/إلغاء كل المهام فقط |

المثبّت يرفض downgrade، ويتحقق من `SHA256SUMS` ومن عدم وجود ملفات زائدة، والمساحة الحرة، والأسرار، وحالة PostgreSQL وRedis وweb وworker وCaddy. وقبل التفعيل ينشئ نسخة للكود والإعدادات ونسخة `pg_dump`. بعد تشغيل V4 ينتظر جاهزية كل خدمة بدل الاعتماد على حالة `Started` وحدها. إذا فشل البناء أو فحص الصحة يحفظ سجلات V4 أولًا، ثم يعيد الكود السابق ويعيد بناء الخدمات القديمة تلقائيًا.

## التحقق والرفع من Termux

```bash
cd /storage/emulated/0/Download
sha256sum -c syrian-archive-airwars-v4.0.0-r1.zip.sha256
scp syrian-archive-airwars-v4.0.0-r1.zip syrian-archive-airwars-v4.0.0-r1.zip.sha256 \
  root@153.92.222.234:/root/
ssh root@153.92.222.234
```

يجب أن يطبع أمر التحقق `OK` قبل رفع الملف أو فكّه.

## التجهيز فقط من دون لمس المهمة الحالية

```bash
set -euo pipefail
release_root=$(mktemp -d /root/syrian-v4-release-XXXXXXXX)
release_dir=$release_root/syrian-archive-airwars-v4.0.0-r1
python3 -m zipfile -e /root/syrian-archive-airwars-v4.0.0-r1.zip "$release_root"
bash "$release_dir/deploy/install-v4.sh" --stage-only
printf 'RELEASE_DIR=%s\n' "$release_dir"
```

النهاية الصحيحة:

```text
V4_STAGE_OK
No production file or container was changed.
```

احتفظ بالقيمة المطبوعة بعد `RELEASE_DIR=`. إذا أغلقت جلسة SSH، أعد تعيين `release_dir` إلى تلك القيمة قبل أمر التفعيل.

حتى لو كانت هناك مهمة `running` أو `paused`، لا يوقف هذا الوضع المهمة ولا يعيد تشغيل الخدمات. بناء صورة staging قد يستخدم جزءًا من CPU والقرص، لكنه لا يبدل صورة الحاويات العاملة.
كما يشغّل اختبارات V4 داخل صورة staging نفسها؛ فلا يسمح بالتفعيل إذا لم تنجح على بيئة Docker الخاصة بالخادم.

## التفعيل تلقائيًا عند الخمول

```bash
bash "$release_dir/deploy/install-v4.sh" --activate-when-idle
```

أثناء وجود مهمة تظهر رسائل مثل:

```text
V4_WAITING_FOR_IDLE active_jobs=1 elapsed_seconds=120
```

لا يطلب المثبّت إيقافها. عند وصول العدد إلى صفر يعيد الفحص، يغلق web/Caddy أولًا لمنع إنشاء مهمة جديدة، ثم يعيد إثبات الخمول قبل إيقاف worker والبدء بالتفعيل. إذا سبقت مهمة جديدة هذا الحد يعيد الخدمات وينسحب من دون قطعها. مهلة الانتظار الافتراضية 24 ساعة، ويمكن تغييرها قبل الأمر:

```bash
export V4_IDLE_TIMEOUT_SECONDS=43200
export V4_IDLE_POLL_SECONDS=5
```

## التفعيل الفوري عندما لا توجد مهمة

```bash
bash "$release_dir/deploy/install-v4.sh"
```

إذا ظهرت مهمة نشطة بين الفحص وحدّ التفعيل يعيد المثبّت الخدمات القديمة وينسحب قبل تغيير الكود.

النهاية الصحيحة للتفعيل:

```text
V4_INSTALL_OK
Admin: http://153.92.222.234/admin
```

## ما الذي يبقى محفوظًا؟

- مجلد `data` وكل سجلات الحوادث والمصادر ونقاط الاستئناف.
- أسرار الإدارة والجلسة وقاعدة البيانات.
- وحدات PostgreSQL وRedis وذاكرة legacy cache.
- المهمة المكتملة أو الملغاة ونتائجها السابقة.
- نسخة احتياطية مؤرخة داخل `/root/projects/_backups`.
- نسخة staging قابلة للتحقق داخل `/root/projects/_staged`.

لا ينفّذ المثبّت `down -v`، ولا يحذف volumes، ولا يعيد تهيئة PostgreSQL.

## إعدادات V4 المتوازنة عند التفعيل

```text
ARCHIVE_COLLECTOR_WORKERS=64
ARCHIVE_COLLECTOR_PER_HOST_WORKERS=4
ARCHIVE_COLLECTOR_SOCIAL_WORKERS=12
ARCHIVE_COLLECTOR_ARCHIVE_WORKERS=12
ARCHIVE_COLLECTOR_DELAY=0.05
ARCHIVE_COLLECTOR_TIMEOUT=6
ARCHIVE_COLLECTOR_FAST_TIMEOUT=3
ARCHIVE_COLLECTOR_RETRIES=1
ARCHIVE_COLLECTOR_CHECKPOINT_EVERY=5000
ARCHIVE_SOURCE_CHUNK_SIZE=5000
ARCHIVE_INCIDENT_CHUNK_SIZE=250
ARCHIVE_LIVE_UPDATE_INTERVAL=0.5
ARCHIVE_INCIDENT_MODE=network_refresh
ARCHIVE_INLINE_WAYBACK=false
```

هذه قيم بداية متوازنة. حدود Facebook وX والأرشيف الفعلية يطبقها مجدول V4 بسياسات معدل مستقلة، وليس بتأخير عالمي متضاعف.

## فحص ما بعد التثبيت

```bash
cd /root/projects/syrian-archive-airwars
docker compose --env-file .env.vps -f compose.vps.yaml ps -a
docker compose --env-file .env.vps -f compose.vps.yaml exec -T web \
  python -c 'from archive_pipeline.speed_pilot import ENGINE_VERSION; print(ENGINE_VERSION)'
curl -fsS -H 'Host: 153.92.222.234' http://127.0.0.1/health
```

المتوقع هو `4.0.0` ثم `{"status":"ok"}`، مع PostgreSQL وRedis وweb بحالة healthy والعامل بحالة running.

## القياس قبل نطاق كبير

نتيجة القياس الحتمي موثقة في `docs/V4_BENCHMARK_RESULT_AR.md`. بعد التثبيت شغّل نطاقًا جديدًا من 20–50 حادثة أولًا وقارن:

- wall clock الحقيقي.
- العناصر/الدقيقة بعد أول خمس دقائق، لا سرعة الدقيقة الأولى فقط.
- P95 لانتظار المجدول والشبكة كلٌ على حدة.
- نسبة النص المحفوظ، والمؤجل الخارجي، والخطأ التشغيلي.
- عدالة المواقع العامة أثناء وجود كتلة Facebook/X.

لا تبدأ نطاق 850 حادثة اعتمادًا على Benchmark منطقي وحده؛ اجعل اختبار VPS القصير بوابة الانتقال.
