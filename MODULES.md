# MODULES — Modül Kütüphanesi

> Kural (CONVENTIONS.md, Kural 11 — MUST): Oluşturulan veya değiştirilen her python modülü, **aynı oturumda** bu dosyaya işlenir: amaç, girdi/çıktı, parametreler, örnek komut, test durumu. Yeni oturum DEVLOG.md ile birlikte bu dosyayı okuyarak başlar; modül detayları için kimsenin sohbette hatırlatma yapması gerekmez. Bir modülün girdisi değişirse ilgili bölüm güncellenir (DEVLOG'dan farklı olarak burada güncelleme serbesttir, geçmiş DEVLOG'da tutulur).

Tüm modüller `python_scripts/` altındadır ve CONVENTIONS.md K1–K8 kurallarına uyar (dosya girdili/çıktılı, streaming, pipeline uyumlu, `--debug`, parametrik, `report.json` canlı, çökmez). Hepsi yalnızca **standart kütüphane** kullanır.

---

## sanitize.py — log temizleyici

**Amaç:** Herhangi bir log dosyasını AI/dilimleme için güvenli düz metne çevirir.

| | |
|---|---|
| **Girdi** | Herhangi bir log dosyası (Türkçe, bozuk bayt, her format — çökmez) |
| **Çıktı** | Tek satırlık dosya: her orijinal satır `_SOL_ <içerik> _EOL_` biçiminde art arda |
| **Dönüşümler** | 1) Türkçe → ASCII (ö→o, ı→i, ÖĞÜÇŞİ→OGUCSI; açık harita + `unicodedata` NFKD — stdlib). 2) a-zA-Z0-9 ve boşluk dışındaki her karakteri **BOŞLUĞA ÇEVİRİR** (noktalama, sembol, alt çizgi dahil; `org.apache.hadoop` → `org apache hadoop`, `Bar.java:42` → `Bar java 42`, `2015-10-17` → `2015 10 17`). 3) Çoklu boşluk → tek boşluk. 4) Satırları `_SOL_`/`_EOL_` ile işaretleyip tek satıra stitch |

**Parametreler:** `--input` (zorunlu) · `--output` (zorunlu) · `--report` (varsayılan `report.json`) · `--report-every` (varsayılan 50000 satır) · `--debug`

**Örnek:**
```bash
python3 python_scripts/sanitize.py \
  --input example_logs/original/hadoop.log \
  --output data/hadoop.clean.log \
  --report data/report_sanitize_hadoop.json
```

**Performans:** ~50–68k satır/sn (zookeeper 74k satır: 0,8 sn; hadoop 394k satır: 6,4 sn).
**Test durumu:** ✅ Türkçe+bozuk bayt+boş satır seti, zookeeper gerçek log (74.380/74.380 satır korundu), hadoop gerçek log (394.310 `_SOL_` = 394.310 `_EOL_` = lines_read, 51.418.983 bayt, 2026-09-06 noktalama-boşluk formatıyla), olmayan dosya (temiz hata rc=1), boş dosya (rc=0). **Marker belirleyiciliği:** alt çizgi boşluğa çevrildiğinden içerikte `_` geçemez → `_SOL_`/`_EOL_` kelimelerini yalnızca bu script yazar; girdiye elle `_SOL_` yazılsa bile çıktıda `SOL` olur (testle kanıtlandı), 51 MB çıktıda underscore'lu tek kelimeler yalnızca marker'lardır.

---

## chunk.py — token bütçeli dilim kesici

**Amaç:** Dev tek satırlık (`_SOL_`/`_EOL_`'lu) metinden, AI'a gönderilecek token bütçeli dilimi keser. Makas satırın ortasında kapanmaz: **son kelime daima _EOL_** — AI'a yarım satır gitmez. ("10k token bedelinde texti çeker, havada kalan bir şey olmaz.")

| | |
|---|---|
| **Girdi** | sanitize.py çıktısı **ZORUNLU** — ilk kelime `_SOL_` olmalı; değilse "önce sanitize.py çalıştırın" hatası + rc=1 (`not_sanitized`). Boş dosya geçerlidir (boş logun sanitize çıktısı da boştur) |
| **Çıktı** | Tek satırlık dilim dosyası, sonu EOL |

**Kesim kuralları:**
- Token maliyeti = (kelime uzunluğu + 1) / `--chars-per-token` (varsayılan 4) — kelime sayısı dosyanın kelime uzunluklarına göre değişir, sabit değildir.
- Hedef ± `--tolerance` (varsayılan %10) bandında **`_EOL_` sınırına** kaydırılır (`at_target_eol` / `extended_to_next_eol` / `trimmed_to_prev_eol`).
- Dosya bütçeden küçükse → tüm dosya gönderilir (`eof_whole_file`; küçük dosyada çıktı girdinin **bayt-bayt aynısıdır**).
- Bütçe tek satırdan küçükse → yine de en az bir tam satır verilir (asla boş çıktı yok).
- `_EOL_` hiç yoksa (bozuk girdi: `_SOL_` var ama satır sonları yok) → bellek koruması `--max-words-no-eol` (varsayılan 5M kelime); gerçek sanitize çıktısında görülmez, savunmadır.
- Son satırı `_EOL_`'süz kalan (bozuk) girdide → kuyruk atılır, temiz `_EOL_`'de durulur.
- Okuma bütçe dolunca durur: 2,9 GB'lık dosyada bile saniyeler içinde biter.

**Parametreler:** `--input` (zorunlu) · `--output` (zorunlu) · `--tokens` (zorunlu, pozitif tam sayı) · `--chars-per-token` (4.0) · `--tolerance` (0.10) · `--max-words-no-eol` (5000000) · `--report` · `--report-every` · `--debug`

**Örnek:**
```bash
python3 python_scripts/chunk.py \
  --input data/hadoop.clean.log \
  --output data/hadoop_dilim_10k.txt \
  --tokens 10000
```

**Referans sonuçlar** (hadoop.clean.log, deterministik — her koşuda aynı; 2026-09-06 noktalama-boşluk formatıyla):
| tokens | kelime | satır | gerçek token | kesim |
|---|---|---|---|---|
| 1000 | 645 | 21 | 1.015,8 | at_target_eol |
| 5000 | 3.356 | 113 | 5.037,5 | at_target_eol |
| 10000 | 6.482 | 206 | 10.056,8 | at_target_eol |

Dilimler iç içedir: 1k ⊂ 5k ⊂ 10k (hepsi aynı yerden başlar; sondaki newline hariç birebir önek). Noktalama-boşluk dönüşümü satır başına kelime sayısını ~2x artırdığından aynı token bütçesi daha az satır alır (10k'da 224→206 satır).
**Test durumu:** ✅ 20/20 Rambo bataryası (eski formatla) + 2026-09-06 `_EOL_` sonrası mini batarya: E2E 1k/5k/10k (rc=0, son kelime `_EOL_`, tolerans OK, deterministik), iç içe önek kontrolü, `--tokens 10` (ilk satır her zaman yazılır), olmayan dosya (rc=1). **Girdi sözleşmesi (aynı gün, kullanıcı talimatıyla):** sanitize edilmemiş düz metin → rc=1 + "önce sanitize.py çalıştırın" (`not_sanitized`); ham hadoop.log → aynı şekilde reddedildi (ilk kelime `2015-10-17`); boş dosya → rc=0 (`eof_empty_input`). "Tolerans aşıldı" uyarısı hata değil, bilgilendirmedir (rc=0).

---

## call_llm_for_pattern_recon.py — LLM kalıp-keşif çağrısı

**Amaç:** chunk.py dilimini, config klasöründeki prompt dosyalarıyla LLM'e (z.ai, OpenAI-uyumlu endpoint) gönderir; kalıp (pattern) kuralları içeren cevabın metnini dosyaya yazar. Her istek/yanıt `--llm-dir` altına JSON olarak düşer (K6 denetim izi).

| | |
|---|---|
| **Girdi** | chunk.py çıktısı **ZORUNLU** — ilk kelime `_SOL_` değilse "önce chunk.py çalıştırın" hatası + rc=1 |
| **Çıktı** | LLM cevabının metin kısmı (`--output`) + wire log çiftleri (`llm/<ad>_NNNN_request/response.json`) + report.json (usage tokenları dahil) |

**Prompt dosyaları DIŞARIDA** — `python_scripts/call_llm_for_pattern_recon_config/` (kod değişmeden düzenlenebilir, son halini orada tut):
- `system_prompt.txt` — sözleşme: girdi formatı (`_SOL_`/`_EOL_`, noktalama boşluk), **çoklu-segment olay kalıpları** (regex'te literal ` _EOL_ _SOL_ ` ayracı; parçalı olaylar VE hep-birlikte-gelen satır dizileri olay sayılır — kullanıcı kararı), **`readable`** (mühendis gözünden şablon satırı: literal kelimeler aynen + değişkenler `_BUYUK_HARF_`/numaralı `_IP1_`), etiketler **`healthy | error`** (olgusal; known/benign yargısı İNSAN'a ait — prompt'ta yasak), izinli regex sözlüğü, anti-yutma kuralları, en fazla 20 kalıp, format ipucu YOK (örnekler soyut)
- `user_prompt.txt` — `$hadoop` placeholder'lı şablon

**Deney sandbox'ı:** `experiment_multiline_llm_recon.py` + kendi config'i yaşar — yeni prompt fikirleri önce orada A/B test edilir, kanıtlanınca ana config'e promote edilir (2026-09-06'da bu akışla çoklu-segment promote edildi).

**Parametreler:** `--input` · `--output` · `--config-dir` (script yanı `<adı>_config`) · `--system-prompt-file` · `--user-prompt-file` · `--placeholder` ($hadoop) · `--model` (çözüm: param > ZAI_MODEL dosya/ortam > glm-5.3-flash) · `--base-url` (… > **z.ai Coding Plan: https://api.z.ai/api/coding/paas/v4** — genel API URL'i DEĞİL, coding anahtarıyla 401 verir) · `--api-key` / `--api-key-file` (varsayılan `/secrets/zai.env`) · `--thinking` (enabled) · `--response-format` (json → API düzeyinde `response_format=json_object`) · `--format-retries` (1) · `--temperature` (0.1) · `--max-output-tokens` · `--max-input-chars` (2M koruma) · `--timeout` (120 sn) · `--retries` (2 → toplam 3 deneme) · `--retry-backoff` (2 sn, üstel) · `--retry-backoff-max` (30 sn) · `--llm-dir` (./llm) · `--report` · `--debug`

**Yanıt sözleşmesi garantisi (üç katman):**
1. **API düzeyi:** `response_format=json_object` (z.ai JSON modu) — sunucu cevabı JSON olmaya zorlar.
2. **Prompt düzeyi:** system prompt dil kuralı (yalnız İngilizce, CJK/Cince kesinlikle yasak) + sıkı JSON şeması.
3. **Lokal doğrulama + düzeltme turu:** cevap geldiğinde `validate_patterns_json` denetler — JSON parse (markdown çitleri ayıklanarak), `patterns[]` şeması (tag/label/regex zorunlu), CJK karakter taraması. İhlal varsa `--format-retries` kadar, önceki cevaba düzeltme mesajı eklenerek yeniden istenir (her tur yeni wire çifti). Bütün turlar bittiğinde hâlâ uymuyorsa: ham cevap yine de `--output`'a OLDUĞU GİBİ yazılır ama rc=1 döner + report'a `json_valid: false` + `contract_violation` işlenir — pipeline durur, sessiz kabul yok. Report alanları: `json_valid`, `pattern_count`, `format_retries_used`.

**Canlı akış (SSE, varsayılan AÇIK):** istek `stream:true` ile atılır; düşünme (`reasoning_content`) ve cevap delta'ları geldikçe stderr'e **2 saniyede bir** `akan: düşünme N karakter, cevap M karakter` satırı düşer — kör bekleme yok. Wire response'a `stream`, `sse_chunks`, `reasoning_chars` + birleştirilmiş `reasoning_content` yazılır. `--no-stream` ile kapatılır. **İlerleme koruması (2026-09-06):** delta'lar aktıkça (sayaçlar arttıkça) koşu istediği kadar sürer, kesilmez; iptal yalnızca veri `--stream-idle-timeout` (varsayılan 300 sn) boyunca TAMAMEN susarsa gelir. İlerleme görülmüşken akış düşerse **otomatik retry YAPILMAZ** (düşünme sıfırlanmasın) — kısmi içerik wire loga `partial: true` ile korunur, rc=1 + insan-okur mesaj. `--timeout` yalnızca stream kapalıyken geçerlidir.

**Davranış kuralları:**
- Anahtar çözümü: `--api-key` > `--api-key-file` > `ZAI_API_KEY` ortam değişkeni; wire loglara anahtar YAZILMAZ (yalnızca `***son4` parmak izi).
- Her deneme bir wire çifti yazar; numara mevcut en büyükten devam eder (restart'a dayanıklı), hata yanıtı da yazılır — kayıp çağrı olamaz (K6).
- Retry yalnızca: ağ hatası, 429/500/502/503/504, bozuk yanıt biçimi. 401/403 gibi kalıcı hatalarda ilk denemede düşer (fail-fast).
- report.json deneme başına güncellenir (K8, atomik yazım).

**Örnek:**
```bash
python3 python_scripts/call_llm_for_pattern_recon.py \
  --input data/hadoop_e2e_10000.txt \
  --output data/hadoop_patterns.json \
  --debug
```

**Test durumu:** ✅ (anahtarsız) sözleşme reddi, olmayan girdi, placeholder eksikliği, anahtar hiçbir yerde yok → hepsi rc=1 + temiz mesaj; gerçek z.ai endpoint'i placeholder anahtarla → HTTP 401 fail-fast (tek deneme, tek wire çifti) — endpoint/URL doğrulandı; yerel mock sunucuda 500→retry(1sn backoff)→200 tam yolu: rc=0, cevap metni birebir çıktıya, wire çiftleri 0001/0002, usage (prompt/completion/total token) rapora düştü. ⏳ Gerçek anahtarla canlı kalıp-çıkarım testi bekliyor.

---

## clean_patterns_sum_and_list_new.py — öğrenilenleri ele, özetle, yenileri listele

**Amaç:** Öğrenme döngüsünün ikinci yarısı. LLM'in döndürdüğü kalıp kurallarıyla sanitize çıktısındaki eşleşen segmentleri/olayları **atomik** eler; readable sıklık özetini JSON'a yazar; eşleşmeyenleri **aynı sanitize formatında** sonraki tura bırakır (chunk.py'a direkt beslenir → yeni LLM turu → döngü kapanır).

| | |
|---|---|
| **Girdi** | `--input`: sanitize.py çıktısı (ilk segment `_SOL_` değilse red + rc=1) · `--rules`: LLM kalıp JSON'u, **tekrarlanabilir** (birikimli baseline) |
| **Çıktı** | `--output`: eşleşmeyenler (sanitize formatı, tek satır) · `--summary`: readable sıklık özeti JSON (overview_lines dahil) · `--matched-output` (opsiyonel denetim) · report.json (K8) |

**Kural doğrulama kapıları** (geçmeyen skip + özete işlenir, koşu düşmez; geçerli kural kalmazsa rc=1): compile · **self-match** (regex kendi `example`'ına fullmatch — v3'te 2 LLM hatası bu kapıda yakalandı: `\w+` sayı hatası) · yinelenen regex. Skip edilen kuralın segmentleri survivor'a düşer → sonraki LLM turu düzgününü öğrenir (kendi kendine onarım).

**Motor (streaming, sabit bellek):** segmentler `_EOL_`'den bölünür (underscore invariant: yanlış bölme imkânsız; blok kenarında yarım marker yapıştırılır); kurallar **uzun→kısa**, ilk eşleşme kazanır (attribution); **literal-anchor prefilter** (regex'in en uzun saf-literal koşusu `str.find` ile — regex koşmadan önce); kuralın erişimi regex'teki `_EOL_` sayısından hesaplanır, `(...)+ ` sınırsızları `--max-event-lines` (500) ile sınırlı; bellek = en uzun kuralın erişimi.

**Koruma denklemi (her koşuda kanıt):** `segments_in == removed + kept` — bozulursa SERT hata rc=1, sessiz kayıp imkânsız. (Geliştirme sırasında denklem iki gerçek bug yakaladı: `_EOL_`'siz segment üretimi + sayacsız survivor.)

**Parametreler:** `--input` · `--rules` (append) · `--output` · `--summary` · `--matched-output` · `--only-rules` / `--exclude-rules` (virgüllü tag) · `--max-rule-coverage` (0.40 — aşırı-geniş kural flag'i) · `--max-event-lines` (500) · `--report` · `--report-every` (50000) · `--debug`

**Performans:** 51 MB / 394.310 segment / 18 kural → **7,3 sn** (~54k segment/sn).

**Referans sonuçlar** (v3 kurallarıyla, 2026-09-06):

| girdi | segment | elenen | kalan | kapsam |
|---|---|---|---|---|
| hadoop_ex_chunk_10k (öğrenildiği chunk) | 279 | 153 | 126 | %54,8 |
| **hadoop.clean.log (tam log)** | 394.310 | 79.292 | 315.018 | **%20,1** |

En çok eşleşenler: task progress raporu ×35.157 · spill dizisi ×4.248 · metrics ×978. Kapsam döngü turlarıyla büyür (round 2: `unmatched_round1.log` → chunk → LLM → kurallar birikimli `--rules` ile).

**Test durumu:** ✅ gerçek v3 kurallarıyla chunk + tam log (denklem tuttu), sanitize-değil girdi rc=1, bozuk kurallar→"geçerli kural kalmadı" rc=1, hiç eşleşmeyen kural → survivor çıktısı girdinin **bayt-bayt aynısı** (round-trip kanıtı), `--only-rules` filtresi + doğrulama zinciri, çoklu-segment olaylar atomik elendi (spill dizisi ×14).

---

## pipeline.py — öğrenme döngüsü orkestratörü

**Amaç:** Bizim elle yaptığımız döngünün otomatiği: orijinal log → sanitize → (chunk → LLM → clean) × N raunt. Her raunt kendi klasörüne yazar; alt uygulamaların canlı çıktıları (akan düşünme, İŞLENİYOR...) aynı ekrana akar; kırmızı bayrakta anında durur.

| | |
|---|---|
| **Girdi** | `--input`: orijinal HAM log (sanitize çıktısı da verilirse sanitize otomatik atlanır — ilk kelime `_SOL_` algılaması) |
| **Çıktı** | `<workdir>/round_000/` (sanitize) + `round_NNN/` (chunk.txt, patterns.json, report_llm.json, `llm/` wire loglar, unmatched.log, summary.json, removed.log, report_clean.json) + `pipeline_report.json` (K8, raunt raunt atomik güncellenir) |

**Mekanik:** Alt modüller K3 gereği import edilir (`parse_args([...])` + `run(args)`) — aynı süreç, canlı loglar akar. Her adımın rc'si denetlenir: beklenmeyen rc≠0 → **kırmızı bayrak**, anında dur + rapora sebep. İyi sonlar: kalan 0 · raunt kazancı < `--min-round-gain-pct` (varsayılan 1.0 puan, "azalan getiri") · "geçerli kual kalmadı" (kalanlar kalıp vermiyor) · `--max-rounds` tavanı.

**Ekran çıktısı:** raunt başlıkları, adım sonuçları (`dilim: N segment`, `kalıp: N | json_valid | token`), raunt kapanışı (`elenen | kalan | kapsam | kazanç`), final tablo + toplam token (düşünme dahil) + süre.

**Parametreler:** `--input` · `--workdir` (varsayılan `data/pipeline_<zaman>`) · `--max-rounds` (3) · `--tokens` (10000) · `--min-round-gain-pct` (1.0) · `--llm-model` / `--llm-base-url` / `--llm-timeout` (120) / `--llm-stream-idle-timeout` (300) / `--no-stream` · `--report-every` (50000) · `--report` · `--debug`

**Örnek:**
```bash
python3 python_scripts/pipeline.py \
  --input example_logs/original/hadoop.log \
  --workdir data/pipeline_hadoop_1 \
  --max-rounds 5
```

**Test durumu:** ✅ mock LLM ile uçtan uca: mutlu yol (110 segmentte %81,8, raunt klasörleri + wire loglar tam) · azalan getiri duruşu (raunt 2 kazanç +0.0 < 1.0 → dur) · "geçerli kual kalmadı" iyi sonu · kırmızı bayrak (mock ölünce llm rc=1 → anında dur, sebep raporda) · prev_coverage güncellemesi eksikliği testte yakalandı ve düzeltildi. Gerçek z.ai koşusu kullanıcıya teslim.

---

## gui_server.py + dashboard.html — canlı mission-control GUI

**Amaç:** Pipeline'ın ne yaptığını insan gözüyle anında anlatan tek sayfa dashboard; koşuyu GUI'den başlatma/durdurma, /workspace içinde dosya gezintisi, dışarıdan log upload. Sunucu stdlib'dir (FastAPI gibi bağımlılık YOK); görsellik tek dosya dashboard.html'de (CDN'siz, offline).

| | |
|---|---|
| **Çalıştırma** | `docker compose --profile gui up -d gui` → Windows Chrome: `http://localhost:8080` (opt-in: profil dışıysa hiç başlamaz; `docker compose stop gui` ile kapatılır). Port yalnız 127.0.0.1'de (LAN kapalı). |
| **API** | `GET /api/state` (pipeline_report + LLM kalp atışı + aşama tahmini) · `/api/patterns` (rauntlar birleşik kalıp tablosu) · `/api/log` (canlı konsol kuyruğu) · `/api/fs` (/workspace'e hapsedilmiş gezintide) · `/api/tree` (aktif koşu klasörünün canlı ağacı, 4000 giriş tavanı) · `/api/file?path=` (workdir içi dosyanın O ANKİ içeriği — `_safe_ws` hapisli, >64 KB ise son 64 KB kuyruk) · `/api/runs` (arşiv) · `POST /api/start` (pipeline subprocess — sunucu bloklanmaz, çift koşuya red) · `/api/stop` (SIGINT → kısmi çıktılar korunur) · `/api/upload` (ham gövde akıtma → data/uploads/) |
| **Dashboard** | koyu mission-control: nabız atan aşama akışı (sanitize→chunk→LLM(düşünme krk canlı)→clean→raunt), animasyonlu KPI'lar, SVG kapsam grafiği, raunt kartları, `×N [label] readable` kalıp tablosu (yeni kalıp borsa-tarzı yeşil giriş animasyonuyla), "Oluşan Dosyalar" paneli: mtime azalan düz liste (yeşil saat + amber klasör yolu + ad + boyut her satırda; en güncel dosya en üstte, yeni dosya yeşil parlar; 600 satır tavanı) ve dosyaya tıkla → İÇERİK POPUP'ı (o anki içerik; >64 KB'da son 64 KB; modal açıkken 1,5 sn'de canlı tazelenir, kuyruk modunda otomatik alta kayar; ↻ anında yeniler), ALTA SABİTLENMİŞ canlı konsol, dosya gezinme + upload + parametreli BAŞLAT. Yerleşim docked'dır: içerik `#main`'de kendi kayar, konsol/alt bar gerçek paneldir — hiçbir paneli örtmez (fixed-overlay denemesi kalıp tablosunu kapatıyordu, ölçümle yakalanıp değiştirildi). TÜM paneller tıkla-aç/tıkla-kapa (başlıktaki ▸/▾ döner; durum localStorage'da kalıcı); her başlıkta o panelin verisini ANINDA çeken ↻ butonu (state/patterns/tree/log); GET'lere `_=ts` cache-busting — tarayıcı önbelleği veriyi bayatlatamaz |
| **Hiç çökmez** | her uç try/except (eksik/bozuk dosya → null), raporlar zaten atomik (K8); koşu tespiti: GUI subprocess'u ∨ taze kalp atışı (15 sn) ∨ running-rapor (600 sn bayatlık payı — eski kalp atışsız koşular için) |

**Kalp atışı (llm):** `http_post_json_stream` stream sırasında 2 sn'de bir `llm/live.json` yazar (düşünme/cevap karakter, SSE parça, sn) — GUI'de "düşünme N karakter" canlı akar. Ana + deney scriptlerinde parite.

**Test durumu:** ✅ salt-okuma uçları (/, state, patterns, fs, runs, /etc kaçış denemesi → /workspace'e sabitlendi); GERÇEK koşu canlı izlendi (pipeline_hadoop_1, running tespiti, 28 kalıp birleşik); Windows Chrome'dan HTTP 200; gui konteyneri entrypoint override ile (SSH entrypoint'i komutu ezmişti — düzeltildi); `sre-dev`/koşan pipeline'a hiç dokunulmadan. ⏳ start/stop/upload uçları GUI'den kullanıcı tarafından ilk kullanımda değerlenecek (mock ile pipeline yolu zaten kanıtlı).

## chunk.py — token bütçeli dilim kesici (GÜNCELLENDİ: dairesel rastgele pencere)

**Değişiklik (kullanıcı tasarımı):** pencere artık varsayılan olarak dosyanın RASTGELE bir segmentinden başlar; dosya sonuna denk gelirse BAŞA SARILIR ve bant (hedef ±%10) dolana dek tamamlanır — asla cılız dilim dönülmez. Amaç: dilimin kütleye oranlı temsili (dev kalıpların bölgeleri de sıraya girer; her raunt güncel dosyadan taze örnek → öz-dengeli döngü).

| | |
|---|---|
| **Modlar** | `--mode random` (VARSAYILAN) · `--mode head` (eski davranış) · `--start-offset N` (konumu bayt cinsinden zorla — test/tekrarlanabilirlik) |
| **Tohum** | `--seed` — verilirse koşu tekrarlanabilir; seçilen konum/tohum/sarma bilgisi report.json'a düşer (denetim izi) |
| **Güvenlik** | rastgele nokta segment ortasına düşebilir → akış ilk `_SOL_'e yaslanır (underscore invariant: yaslanma tek anlamlı); sarma EN FAZLA bir tur (küçük dosyada içerik çoğaltılmaz — tur bitince `circle_completed`); girdi sözleşmesi DOSYANIN ilk kelimesinden kontrol edilir (pencere konumundan bağımsız) |
| **Testler** | ✅ sözleşme reddi rc=1 · wrap (s039→s000→s022, 24 segment, bant içi, çevrimsel-bitişik) · küçük dosya (40/40 benzersiz, çoğaltma yok) · aynı seed → bayt-bayt aynı · rastgele iki koş farklı + format geçerli (`_SOL_`…`_EOL_`, 10.024 token) · head modu eski davranış. (İlk testteki 'uyuşmazlık' test hatasıydı: karşılaştırma `_EOL_` yutuyordu — düzeltilince tam eşleşme) |
| **Pipeline** | çağrı argümanları değişmedi → pipeline OTOMATİK rastgele pencereye geçer; raunt raporlarında (`report_chunk.json`) pencere bilgisi görünür |

**GUI güncelleme:** DURDUR butonu hep görünür (koşu yokken disabled); `/api/stop` GUI süreci yoksa koşu-tespitli ise "terminalden başlatılmış" + pkill tarifi döner. LLM prompt'u tamamen nötr (hadoop örneği yok — çoklu-dosya testine hazır).
