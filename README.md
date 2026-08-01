# الأرشيف السوري — بيانات Airwars

واجهة عربية مستقلة وثابتة لحوادث الضرر المدني المتعلقة بسوريا. الموقع غير تابع لـAirwars، وتبقى نسبة المواد والمعلومات للناشرين والمصادر الأصلية. قد تقود بعض الروابط إلى محتوى مؤلم.

الموقع المنشور: <https://carsspacee88-crypto.github.io/syrian-archive-airwars/>

English documentation: [README_EN.md](README_EN.md)

## الحالة الحالية

- 8,114 صفحة حادثة و8,114 ملف بيانات تاريخي.
- 82 صفحة فهرس، وبحث محلي اختياري، وخريطة تعمل من بيانات ثابتة.
- خط جمع مباشر وقابل للاستئناف من Airwars ثم Wayback عند الحاجة.
- 11 سجلًا في دفعة الاختبار المباشر الأولى؛ تظهر الأعداد الحالية الدقيقة في `data/reports/collection-summary.json`.
- 6,679 نقطة معروضة حاليًا. استُبعدت الإحداثيات الناقصة وغير الصالحة والمشبوهة من دون تصحيح صامت؛ التفاصيل في `data/reports/map-coverage.json`.
- لا توجد قاعدة بيانات أو خادم خلفي، ولا تُنزّل صور أو فيديوهات.

## مصدر الحقيقة وهندسة المشروع

المصدر النشط هو JSON الموحّد في `data/incidents/`. ترتيب المصادر هو:

1. نقطة Airwars العامة المنظمة، عندما تكون متاحة وموثوقة.
2. صفحة حادثة Airwars الحية.
3. نسخة صفحة Airwars في Wayback عند تعذر المصدر الحي أو نقصه.
4. روابط الأرشفة الخارجية المدرجة مع الحادثة.
5. لقطة Excel التاريخية، بوصفها مرجع هجرة واحتياطًا فقط.

القيم المهاجرة موسومة `legacy_import` حتى تُراجع من مصدر مباشر أو مؤرشف. لا تُحذف القيمة القديمة عند التعارض؛ تُحفظ القيمتان والمنشأ ويُرفع علم للمراجعة. المعرّف الثابت مشتق من معرّف Airwars الداخلي، وليس من الرمز العام وحده، ولذلك تبقى الرموز العامة المكررة سجلات منفصلة.

الحزمة القديمة في `site-package/` محفوظة لتجنب تدمير الموقع العامل ولتوفير المعرّفات الأولية. لا تُستخدم كخط جمع مستمر.

## البنية

```text
archive_pipeline/       الجامع والمحلل والتطبيع والبناء والتحقق
scripts/                أوامر قابلة للتنفيذ
data/incidents/         JSON موحّد لكل حادثة جُمعت مباشرة
data/raw/airwars/       بيانات طلبات ولقطات JSON/نصية مسموحة
data/raw/archive/       بيانات Wayback ولقطات نصية، بلا وسائط ثنائية
data/reports/           تقارير الجمع والخريطة والتحقق
data/state/             نقطة الاستئناف والدفعات
data/schema/            مخطط JSON
site-package/           لقطة الموقع التاريخية المجزأة
.github/workflows/      الفحص والجمع والنشر
```

## تعريف الحالات

- `complete`: جرى تحليل مصدر مباشر أو مؤرشف وتوفرت الحقول والأقسام المطلوبة وفق إصدار المحلل الحالي.
- `partial`: توجد بيانات وصفحة صالحة، لكن حقلًا أو قسمًا مطلوبًا لم يُستخرج. لا تعني أن HTML معطل.
- `blocked`: حجب المصدر الطلب وقت الجمع ولم تتوفر نسخة قابلة للاستخدام في تلك المحاولة.
- `unavailable`: أعادت المصادر المتاحة عدم وجود صريحًا.
- `failed`: خطأ شبكة أو تحليل غير حاسم.
- `pending_review` و`conflicting_sources`: يحتاج السجل إلى قرار بشري.

نجاح HTTP وحده لا يساوي الاكتمال. صفحة المنهجية المنشورة هي `methodology.html`.

## الإعداد محليًا

يتطلب البناء Python 3.11 أو أحدث و`unzip`.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cat site-package/part-* > /tmp/syrian-archive-site.zip
echo "9f35a90d92f9fb0334cb61ffd8434aac03fd0ee61622908a11923dac40da35f3  /tmp/syrian-archive-site.zip" | sha256sum --check
```

## الجمع المباشر والاستئناف

### محرك V4 على GitHub Actions

سير العمل **«جمع بيانات Airwars»** يشغّل المحرك `4.0.0` مباشرة داخل
GitHub Actions، ثم يحفظ JSON والتقدم وطابور الاسترداد في `main`، ويبني
الموقع ويتحقق منه. بعد نجاح سير الجمع، يشغّل حدث `workflow_run` نشر GitHub
Pages من أحدث `main`، وبذلك تظهر البيانات الجديدة في مشروع الأرشيف نفسه
من دون قاعدة بيانات أو خادم دائم.

يمكن التشغيل بلا GitHub CLI وبلا جلسة OAuth محلية: أي التزام يغيّر
`.github/v4-request.json` على `main` يشغّل الطلب الموجود في الملف. يحتوي
`request_id` على معرّف فريد لكل طلب؛ تغييره مع النطاق يمنع الخلط بين
التشغيلات ويترك سجلًا واضحًا في Git.

للتشغيل من تبويب Actions:

1. اختر `جمع بيانات Airwars` ثم `Run workflow`.
2. اختر `collect` لجمع نطاق جديد، أو `recover` لاسترداد المتعذر في النطاق.
3. أدخل أول وآخر تسلسل. الإعداد الافتراضي التالي بعد البيانات الحالية هو
   `151` إلى `250`.

أما التشغيل من ملف الطلب، فيتم بتحديث العملية والنطاق والإعدادات في
`.github/v4-request.json` مع `request_id` جديد ثم دفع التعديل إلى `main`.
تتحقق Action من القيم قبل بدء الجمع، وتقرأ كل طلب من الالتزام الذي أطلقه
حتى لو وُجدت طلبات أخرى في الطابور.

يستخدم V4 افتراضيًا 64 عامل شبكة عالميًا، مع عامل واحد وفاصل `0.75` ثانية
لكل مضيف. لا يحتجز مضيف بطيء بقية الطابور؛ تُجدول الروابط بالتناوب بين
المضيفين. يمر الجمع الأساسي مرة واحدة، وتُنقل العناصر الخارجية المتعذرة
إلى `data/recovery/v4-XXXX-YYYY.json` بدل عدّها فشلًا نهائيًا. يعيد وضع
`recover` المحاولة الحية ثم Wayback ويحفظ النتيجة بعد كل عنصر.

التنفيذ المحلي المطابق:

```bash
python scripts/run_v4_action.py \
  --legacy-zip /tmp/syrian-archive-site.zip \
  --operation collect \
  --first-sequence 151 \
  --last-sequence 250 \
  --workers 64
```

التفاصيل التشغيلية ومسارات الحالة موثقة في
[دليل GitHub Actions لمحرك V4](docs/GITHUB_ACTIONS_V4_AR.md).

دفعة الاختبار الممثلة:

```bash
python scripts/collect_incidents.py \
  --legacy-zip /tmp/syrian-archive-site.zip \
  --batch-file data/state/initial-test-batch.json \
  --limit 20 \
  --delay 1.5
```

دفعة محددة:

```bash
python scripts/collect_incidents.py \
  --legacy-zip /tmp/syrian-archive-site.zip \
  --sequences 4771,480-481 \
  --limit 20 \
  --delay 1.5
```

متابعة الدفعة التالية من السجلات غير المكتملة:

```bash
python scripts/collect_incidents.py \
  --legacy-zip /tmp/syrian-archive-site.zip \
  --limit 25 \
  --delay 1.5
```

يكتب الجامع تقدمَه بعد كل حادثة في `data/state/collector-state.json`. لا يعيد السجلات المكتملة إلا مع `--force`. السجلات الجزئية والفاشلة تبقى مؤهلة لمحاولة لاحقة. التأخير افتراضيًا 1.25 ثانية، مع إعادة محاولة وتراجع أُسّي للأخطاء المؤقتة. يمكن تشغيل سير العمل «جمع بيانات Airwars» يدويًا من Actions من دون مفاتيح مدفوعة.

## التقارير والبناء والتحقق

```bash
python scripts/generate_reports.py \
  --legacy-zip /tmp/syrian-archive-site.zip \
  --output-root .

mkdir -p _site
unzip -q /tmp/syrian-archive-site.zip -d _site

python scripts/build_site.py --site-root _site --project-root .

python scripts/validate_site.py \
  --site-root _site \
  --project-root . \
  --legacy-zip /tmp/syrian-archive-site.zip \
  --report-root _site/data/reports

python scripts/write_checksums.py --site-root _site
python -m http.server 8000 --directory _site
```

يفشل التحقق عند غياب صفحة أو JSON، أو كسر رابط داخلي، أو وجود مسار يبدأ من جذر النطاق، أو تكرار معرّف داخلي، أو تسرب إحداثي غير صالح إلى الخريطة، أو نقص المنشأ، أو وسم سجل مكتمل مع نقص متطلباته، أو وجود ملف وسائط ثنائي. الرموز العامة المكررة تحذير متوقع وليست خطأ دمج.

## GitHub Pages وحل 404

يجب أن يكون Source في **Settings → Pages** مضبوطًا على **GitHub Actions**. سير العمل يعيد تكوين ZIP، ويتحقق من بصمته، ويفك `index.html` في جذر artifact، وينشئ `.nojekyll`، ويبني الطبقة الجديدة، ويفحص المسارات النسبية تحت `/syrian-archive-airwars/` قبل النشر. سبب 404 التاريخي كان عدم تفعيل مصدر Pages، لا غياب `index.html` من الحزمة.

## طيار المحتوى الكامل لأول 100 حادثة

فرع الطيار يعالج `cases/0001` إلى `cases/0100` فقط. يفرض
`archive_pipeline.pilot` هذا الحد برمجيًا ويوقف التنفيذ إذا طُلب التسلسل
`0101` أو أي تسلسل بعده. يبدأ من قيم لقطة الهجرة الموسومة
`legacy_import`، ثم يضيف تحقق Airwars الحي أو المؤرشف، وسجلات المصادر،
ويحفظ النصوص المسترجعة بلغتها الأصلية من دون ترجمة أو تلخيص، ويضيف مواضع الوسائط الوصفية من دون تنزيل ملفات
الصور أو الفيديو أو الصوت.

```bash
python scripts/run_first_100_pilot.py \
  --legacy-zip /tmp/syrian-archive-site.zip \
  --output-root .
```

التنفيذ قابل للاستئناف من `data/pilot/first-100-progress.json`، ويكتب نقطة
تحقق بعد كل حادثة ومصدر. الترجمة الآلية معطلة بطلب المستخدم ولا يحتاج
الطيار إلى مفتاح OpenAI أو DeepL. توجد القياسات
في `data/reports/first-100-*`، وتُنشأ صفحات المصادر في artifact تحت
`sources/{source_id}/index.html`.

## الوسائط والقيود المعروفة

لا يُنزّل المشروع الصور أو الفيديوهات ولا يحولها إلى Base64 ولا يستخدم Git LFS. تُحفظ الروابط والعناوين والنسب وإشارات الحساسية فقط، ولا تُحمّل الصور الحساسة إلا باختيار المستخدم. خطة الحفظ المستقبلية موثقة في [docs/MEDIA_PRESERVATION_PLAN_AR.md](docs/MEDIA_PRESERVATION_PLAN_AR.md).

قد يعيد Airwars الرمز 403، وقد تكون لقطة Wayback ناقصة أو مؤقتًا غير متاحة. لا توجد ترجمة آلية لأي نص، ولا يُستنتج تاريخ أو موقع مفقود. تفاصيل المخاطر والمراجعات في تقارير `data/reports/`.

## لوحة إدارة VPS

توجد حزمة تشغيل مستقلة في `compose.vps.yaml` تضيف لوحة عربية محمية، قاعدة PostgreSQL، وطابور Celery/Redis قابلًا للاستئناف. تسمح باختيار نطاق الحوادث ومتابعة كل مرحلة وإيقاف المهمة أو استئنافها أو إعادة العناصر الفاشلة فقط، ولا تبدأ أي جمع تلقائيًا عند تشغيل الخادم. راجع [دليل VPS العربي](docs/VPS_ADMIN_AR.md) للإعداد والنسخ الاحتياطي والحدود التشغيلية.
